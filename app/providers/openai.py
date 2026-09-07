"""
Production OpenAI provider adapter, implementing the existing
ProviderAdapter contract (app.providers.base) using the official
AsyncOpenAI client and the current Responses API.

API choice (Responses vs. Chat Completions): the Responses API
(client.responses.create) is used rather than Chat Completions.
CompletionRequest.prompt is a single string; the Responses API accepts a
plain string as `input` directly, a closer match to this project's
existing interface than Chat Completions' list-of-messages shape, which
would require wrapping the prompt in a synthetic single-message list for
no benefit. The Responses API is also OpenAI's current recommended
default endpoint for new integrations (see "Core concepts: Responses
API" / "guides/migrate-to-responses" in the official docs at
https://developers.openai.com/api/docs/guides/migrate-to-responses).

Model selection: the default model is gpt-5-mini (verified against the
official model page https://developers.openai.com/api/docs/models/gpt-5-mini
on 2026-09-07: $0.25 per 1,000,000 input tokens, $2.00 per 1,000,000
output tokens, 400,000-token context window, 128,000 max output tokens,
supports both the Responses and Chat Completions endpoints). This is a
configured default (app.config.Settings.openai_model), not hard-coded
here -- the caller building the provider registry chooses which model(s)
to register as supported. OpenAI's own docs currently point new
high-volume workloads toward gpt-5.6-terra instead; gpt-5-mini was kept
as the default here because its pricing, context window, and snapshot
pinning are unambiguously documented as of this writing. Pricing changes
over time -- see the note in app.services.pricing where the OpenAI
catalog entry is registered.

SDK retry ownership: the AsyncOpenAI client is constructed with
max_retries=0. app.services.routing already owns retries, exponential
backoff, and fallback decisions (see execute_route/_run_candidate). If
the SDK's own automatic retries were left enabled (the SDK's documented
default is 2), a single routing "attempt" could silently become up to 3
real, separately-billed OpenAI requests before routing ever saw a
failure to classify -- defeating routing's own attempt counting and the
cost-accounting assumptions built on top of it. This adapter therefore
disables SDK-level retries entirely and implements no retry loop of its
own; retrying is exclusively routing's responsibility.

Timeout interaction: this adapter passes NO per-request timeout override
to the SDK call. The existing routing layer already wraps every
adapter.complete() call in asyncio.timeout(...) (see
app.services.routing._run_candidate). If this adapter also set its own
SDK-level request timeout, the two timeouts would race independently --
whichever fired first would determine the outcome, and a request
cancelled by the SDK's own internal timeout machinery would not
necessarily surface as the same exception type asyncio.timeout()
produces, undermining routing's timeout-vs-provider-failure
classification. Relying solely on the outer asyncio.timeout() keeps
routing the single source of truth for "how long is too long": the
AsyncOpenAI client is built on httpx, whose connections cooperate
correctly with asyncio-level cancellation, so the outer timeout cleanly
cancels the in-flight HTTP request rather than leaving it orphaned.

Client lifecycle: exactly one AsyncOpenAI client instance is created,
once, at adapter-construction time (OpenAIProvider.from_settings), and
reused for every subsequent request -- never recreated per call, which
would otherwise open a new httpx connection pool on every single
completion request. Tests inject a fake client satisfying only the
`.responses.create(...)` surface this adapter actually uses, via the
plain OpenAIProvider(client=...) constructor, with no dependency on
constructing real openai-python SDK objects and no real network access.

No database dependency: this module imports nothing from app.db or
app.models, and never creates, holds, or closes a Session.
"""

import time
from collections.abc import Mapping
from inspect import isawaitable
from typing import Protocol

import openai
from openai import AsyncOpenAI

from app.providers.base import CompletionRequest, CompletionResult, ProviderError
from app.services.routing import (
    NonRetryableProviderError,
    ProviderRegistration,
    RetryableProviderError,
    build_provider_registry,
)


class UnexpectedProviderResponseError(ProviderError):
    """Raised when the OpenAI response is malformed in a way that is not
    a documented, expected API failure -- missing usage data, or a
    response object missing fields this adapter requires in order to
    populate CompletionResult truthfully. Deliberately a bare
    ProviderError, not one of the RetryableProviderError/
    NonRetryableProviderError subclasses: app.services.routing treats an
    unclassified bare ProviderError as non-retryable by default (its
    documented fail-safe), which is correct here -- a malformed response
    shape from OpenAI is not something a same-candidate retry is likely
    to fix.
    """


class _ResponsesClient(Protocol):
    """The minimal async client surface this adapter actually uses --
    lets tests inject a fake object satisfying just this shape, with no
    dependency on the real openai-python SDK's client construction.
    """

    responses: object  # exposes an async .create(**kwargs) method


class OpenAIProvider:
    name = "openai"
    supported_models = frozenset({"gpt-5-mini"})

    def __init__(self, client: _ResponsesClient, *, owns_client: bool = False) -> None:
        self._client = client
        self._owns_client = owns_client

    @classmethod
    def from_settings(
        cls,
        *,
        api_key: str,
        base_url: str | None = None,
        organization: str | None = None,
        project: str | None = None,
    ) -> "OpenAIProvider":
        """Production factory. Constructs one real AsyncOpenAI client
        with SDK-level retries disabled (max_retries=0 -- see module
        docstring) and no client-level request timeout override (the
        outer routing layer owns timeouts). Performs NO network call --
        constructing an AsyncOpenAI client is purely local/lazy; nothing
        is sent until a method like .responses.create(...) is actually
        awaited. Raises ValueError immediately if api_key is blank --
        production must fail clearly at construction time, not silently
        proceed with an unauthenticated client that would only fail
        later, unpredictably, on the first real request.
        """
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError(
                "OpenAI API key is required to construct a production OpenAIProvider "
                "(construction fails now rather than failing unpredictably on first request)"
            )
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            organization=organization,
            project=project,
            max_retries=0,
        )
        return cls(client=client, owns_client=True)

    async def aclose(self) -> None:
        """Close a client created by ``from_settings``.

        Injected clients are caller-owned and are deliberately left open.
        Production application lifespan code should await this method
        during shutdown.
        """
        if not self._owns_client:
            return
        close = getattr(self._client, "close", None)
        if close is None or not callable(close):
            return
        result = close()
        if isawaitable(result):
            await result

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        start = time.perf_counter()
        try:
            response = await self._client.responses.create(
                model=request.model,
                input=request.prompt,
                max_output_tokens=request.max_tokens,
            )
        except openai.APITimeoutError as exc:
            raise RetryableProviderError("OpenAI request timed out") from exc
        except openai.RateLimitError as exc:
            raise RetryableProviderError("OpenAI rate limit exceeded") from exc
        except openai.InternalServerError as exc:
            raise RetryableProviderError("OpenAI reported a server-side (5xx) error") from exc
        except openai.APIConnectionError as exc:
            # Must be caught after APITimeoutError, which is itself a
            # subclass of APIConnectionError -- this branch covers
            # connection failures that are NOT a timeout.
            raise RetryableProviderError("could not connect to OpenAI") from exc
        except openai.AuthenticationError as exc:
            raise NonRetryableProviderError("OpenAI authentication failed") from exc
        except openai.PermissionDeniedError as exc:
            raise NonRetryableProviderError("OpenAI denied permission for this request") from exc
        except openai.NotFoundError as exc:
            raise NonRetryableProviderError(
                f"OpenAI model not found or unsupported: {request.model!r}"
            ) from exc
        except openai.BadRequestError as exc:
            raise NonRetryableProviderError("OpenAI rejected the request as invalid") from exc
        except openai.APIStatusError as exc:
            # Any other documented HTTP-status failure not explicitly
            # matched above (e.g. ConflictError, UnprocessableEntityError):
            # treated as non-retryable by default, matching routing's own
            # fail-safe policy for any provider failure it cannot classify
            # more specifically.
            status_code = exc.status_code
            if status_code in {408, 409} or status_code >= 500:
                raise RetryableProviderError(
                    f"OpenAI returned a transient status error ({status_code})"
                ) from exc
            raise NonRetryableProviderError(
                f"OpenAI returned an unhandled status error ({status_code})"
            ) from exc
        # openai.APIError (the common base of every exception type above)
        # is intentionally NOT caught here as a catch-all: any OpenAI
        # exception type not explicitly matched above is a genuinely
        # unexpected condition and must propagate unclassified rather than
        # being silently mapped to a provider-failure category. Likewise,
        # asyncio.CancelledError, KeyboardInterrupt, SystemExit, and
        # GeneratorExit are BaseException subclasses -- never caught by
        # any `except <SomeException>` clause above -- so external
        # cancellation always propagates unchanged.

        elapsed_ms = int((time.perf_counter() - start) * 1000)

        usage = getattr(response, "usage", None)
        if usage is None:
            raise UnexpectedProviderResponseError(
                "OpenAI response is missing usage data; refusing to invent token counts"
            )
        prompt_tokens = getattr(usage, "input_tokens", None)
        completion_tokens = getattr(usage, "output_tokens", None)
        if isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int):
            raise UnexpectedProviderResponseError(
                "OpenAI usage.input_tokens is missing or has an unexpected type"
            )
        if isinstance(completion_tokens, bool) or not isinstance(completion_tokens, int):
            raise UnexpectedProviderResponseError(
                "OpenAI usage.output_tokens is missing or has an unexpected type"
            )

        completion_text = getattr(response, "output_text", None)
        if completion_text is None:
            # A response can legitimately have output_text == "" -- for
            # example, when all of max_output_tokens was consumed by
            # internal reasoning tokens before any visible text was
            # produced, a documented, real Responses API behavior for
            # reasoning-capable models such as gpt-5-mini. That is NOT a
            # contract violation: usage is still present and still
            # billed, so an empty string is passed through faithfully.
            # output_text being entirely ABSENT (the attribute missing,
            # i.e. None here) means the response object itself is
            # malformed -- that IS treated as a contract failure.
            raise UnexpectedProviderResponseError("OpenAI response is missing output_text entirely")
        if not isinstance(completion_text, str):
            raise UnexpectedProviderResponseError("OpenAI response output_text has an unexpected type")

        return CompletionResult(
            provider=self.name,
            # Always the candidate's own requested model identifier, never
            # response.model. OpenAI's response.model frequently reports a
            # resolved, dated snapshot (e.g. "gpt-5-mini-2025-08-07")
            # rather than the alias that was actually requested
            # (e.g. "gpt-5-mini") -- using response.model here would fail
            # routing's own `result.model == candidate.model` contract
            # check on every real, successful call.
            model=request.model,
            completion_text=completion_text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=elapsed_ms,
        )


def build_production_provider_registry(
    *,
    api_key: str,
    model: str,
    base_url: str | None = None,
    organization: str | None = None,
    project: str | None = None,
) -> Mapping[str, ProviderRegistration]:
    """Build the production provider registry: OpenAI only, never
    MockProvider. Takes raw configuration values rather than an
    app.config.Settings instance, so this module has no dependency on
    app.config and cannot participate in an import cycle with it.
    Raises ValueError immediately (via OpenAIProvider.from_settings) if
    api_key is blank -- production must fail clearly at construction
    time rather than silently falling back to mock or proceeding
    unauthenticated. Performs no network call and no database access.
    """
    if not isinstance(model, str) or not model.strip():
        raise ValueError("OpenAI model must not be blank")
    if model not in OpenAIProvider.supported_models:
        raise ValueError(
            "OpenAI model is not present in this deployment's verified pricing catalog"
        )

    provider = OpenAIProvider.from_settings(
        api_key=api_key, base_url=base_url, organization=organization, project=project
    )
    registration = ProviderRegistration("openai", provider, OpenAIProvider.supported_models)
    return build_provider_registry((registration,))
