
"""

Production OpenAI provider adapter, implementing the existing

ProviderAdapter contract (app.providers.base) using the official

AsyncOpenAI client and the current Responses API.

API choice: Responses API (client.responses.create) -- see the original

Step 29 rationale, unchanged: CompletionRequest.prompt is a single

string, matching Responses API's plain-string `input` more directly than

Chat Completions' message-list shape.

SDK retry ownership: max_retries=0 -- app.services.routing owns all

retries. Unchanged from Step 29.

Timeout interaction: no adapter-level request timeout is set -- the

outer asyncio.timeout() in app.services.routing._run_candidate is the

single source of truth. Unchanged from Step 29.

Client lifecycle / ownership (Step 30 correction): OpenAIProvider now

exposes `owns_client` publicly and `aclose()` closes the underlying

client ONLY if this instance actually constructed it (via

from_settings). An externally injected client (the common case in

tests, and any future caller that wants to manage its own client

lifecycle) is never closed by this class -- ownership is tracked

explicitly via a constructor flag, not inferred by isinstance checks

anywhere else in the codebase (see app.main's lifespan for the

corresponding fix).

Step 30 addition -- authoritative input-token counting: this adapter

now also exposes count_input_tokens(model, prompt), which calls

OpenAI's official token-counting endpoint

(client.responses.input_tokens.count(...), confirmed against

https://developers.openai.com/api/docs/guides/token-counting and

https://developers.openai.com/api/reference/python/resources/responses/subresources/input_tokens/methods/count).

This returns the EXACT number of input tokens the model will actually

be charged for -- not a character-count guess. app.api.chat_completions

calls this before sizing a budget reservation whenever the injected

adapter supports it (production always does; test fakes generally do

not, and fall back to a clearly-labeled, non-authoritative estimate --

see that module's docstring for the full accounting story, including

why this still is not a complete guarantee under retries).

Note: this makes one additional, lightweight OpenAI API call per

request (before the actual completion call). OpenAI's own documentation

describes this endpoint as returning "the exact count the model will

receive" without describing it as a billable generation; this

implementation does not assume it is free of any charge, and this

assumption should be reconfirmed against OpenAI's current pricing page

before high-volume production use.

Step 34 addition -- circuit breaker wiring: build_production_provider_

registry now optionally accepts a circuit_breaker_config

(app.services.routing.CircuitBreakerConfig). When provided, a fresh

CircuitBreaker is constructed and attached to the single "openai"

ProviderRegistration -- see app.services.routing's own module docstring

for the full circuit-breaker design (per-process, in-memory, not

Redis-shared). When omitted (None), the registry behaves exactly as

before Step 34: no circuit breaking.

"""

import time

from collections.abc import Mapping

from inspect import isawaitable

from typing import Protocol

import openai

from openai import AsyncOpenAI

from app.providers.base import CompletionRequest, CompletionResult, ProviderError

from app.tracing import start_safe_span

from app.services.routing import (

    CircuitBreaker,

    CircuitBreakerConfig,

    NonRetryableProviderError,

    ProviderRegistration,

    RetryableProviderError,

    build_provider_registry,

)


class UnexpectedProviderResponseError(ProviderError):

    """Raised when the OpenAI response is malformed in a way that is not

    a documented, expected API failure. Deliberately a bare

    ProviderError -- see Step 29's original docstring for the full

    rationale (routing treats an unclassified bare ProviderError as

    non-retryable by default).

    """


class _ResponsesClient(Protocol):

    responses: object  # exposes async .create(**kwargs) and .input_tokens.count(**kwargs)


def _map_openai_exception(exc: Exception, *, model: str) -> Exception:

    """Shared mapping from an openai-python exception to this project's

    routing failure categories, used by BOTH complete() and

    count_input_tokens() so the two call sites cannot silently drift

    apart. Returns (does not raise) the exception to raise, so callers

    keep control of the `raise ... from exc` chaining at their own call

    site.

    """

    if isinstance(exc, openai.APITimeoutError):

        return RetryableProviderError("OpenAI request timed out")

    if isinstance(exc, openai.RateLimitError):

        return RetryableProviderError("OpenAI rate limit exceeded")

    if isinstance(exc, openai.InternalServerError):

        return RetryableProviderError("OpenAI reported a server-side (5xx) error")

    if isinstance(exc, openai.APIConnectionError):

        return RetryableProviderError("could not connect to OpenAI")

    if isinstance(exc, openai.AuthenticationError):

        return NonRetryableProviderError("OpenAI authentication failed")

    if isinstance(exc, openai.PermissionDeniedError):

        return NonRetryableProviderError("OpenAI denied permission for this request")

    if isinstance(exc, openai.NotFoundError):

        return NonRetryableProviderError(f"OpenAI model not found or unsupported: {model!r}")

    if isinstance(exc, openai.BadRequestError):

        return NonRetryableProviderError("OpenAI rejected the request as invalid")

    if isinstance(exc, openai.APIStatusError):

        status_code = exc.status_code

        if status_code in {408, 409} or status_code >= 500:

            return RetryableProviderError(f"OpenAI returned a transient status error ({status_code})")

        return NonRetryableProviderError(f"OpenAI returned an unhandled status error ({status_code})")

    return exc  # not an OpenAI exception at all -- caller re-raises unchanged


class OpenAIProvider:

    name = "openai"

    supported_models = frozenset({"gpt-5-mini"})

    def __init__(self, client: _ResponsesClient, *, owns_client: bool = False) -> None:

        self._client = client

        self.owns_client = owns_client

    @classmethod

    def from_settings(

        cls,

        *,

        api_key: str,

        base_url: str | None = None,

        organization: str | None = None,

        project: str | None = None,

    ) -> "OpenAIProvider":

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

        """Close the underlying client, but ONLY if this instance

        actually constructed it (owns_client=True, set only by

        from_settings). An externally injected client is left entirely

        alone -- this instance never assumes it may close a client it

        did not create.

        """

        if not self.owns_client:

            return

        close = getattr(self._client, "close", None)

        if close is None or not callable(close):

            return

        result = close()

        if isawaitable(result):

            await result

    async def count_input_tokens(self, model: str, prompt: str) -> int:

        """Call OpenAI's official token-counting endpoint to get the

        EXACT input-token count for a given model/prompt, without

        generating a completion. See module docstring for sources and

        the caveat about this making an additional real API call.

        """

        try:

            with start_safe_span("openai.input_tokens.count", **{"openai.operation": "input_tokens.count"}):
                response = await self._client.responses.input_tokens.count(model=model, input=prompt)

        except Exception as exc:

            mapped = _map_openai_exception(exc, model=model)

            if mapped is exc:

                raise

            raise mapped from exc

        count = getattr(response, "input_tokens", None)

        if isinstance(count, bool) or not isinstance(count, int) or count < 0:

            raise UnexpectedProviderResponseError(

                "OpenAI token-count response is missing input_tokens or has an unexpected type"

            )

        return count

    async def complete(self, request: CompletionRequest) -> CompletionResult:

        start = time.perf_counter()

        try:

            with start_safe_span("openai.responses.create", **{"openai.operation": "responses.create"}):
                response = await self._client.responses.create(

                    model=request.model,

                    input=request.prompt,

                    max_output_tokens=request.max_tokens,

                )

        except Exception as exc:

            mapped = _map_openai_exception(exc, model=request.model)

            if mapped is exc:

                raise

            raise mapped from exc

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

            # See Step 29's original docstring: a legitimate, documented

            # empty-string case (reasoning consumed the whole budget) is

            # different from the attribute being entirely absent, which

            # IS a contract failure.

            raise UnexpectedProviderResponseError("OpenAI response is missing output_text entirely")

        if not isinstance(completion_text, str):

            raise UnexpectedProviderResponseError("OpenAI response output_text has an unexpected type")

        return CompletionResult(

            provider=self.name,

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

    circuit_breaker_config: CircuitBreakerConfig | None = None,

) -> Mapping[str, ProviderRegistration]:

    if not isinstance(model, str) or not model.strip():

        raise ValueError("OpenAI model must not be blank")

    if model not in OpenAIProvider.supported_models:

        raise ValueError("OpenAI model is not present in this deployment's verified pricing catalog")

    provider = OpenAIProvider.from_settings(

        api_key=api_key, base_url=base_url, organization=organization, project=project

    )

    circuit_breaker = CircuitBreaker(circuit_breaker_config) if circuit_breaker_config is not None else None

    registration = ProviderRegistration(

        "openai", provider, OpenAIProvider.supported_models, circuit_breaker=circuit_breaker

    )

    return build_provider_registry((registration,))
