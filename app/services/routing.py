"""
Provider routing: candidate registry, ordered fallback routing, per-attempt
timeouts, and a retry/backoff policy.

Transaction boundary: this module has NO database dependency. It never
imports app.db, app.models, or any DB-touching service, and never creates,
holds, commits, or closes a Session. Nothing here can open a database
connection.

Failure classification: adapters should raise RetryableProviderError or
NonRetryableProviderError (both subclass the existing app.providers.base
.ProviderError -- no change to that file was needed). A bare ProviderError
(the pre-existing base class) is treated as NON-retryable by default, as a
fail-safe for adapters that haven't been updated to signal retryability
explicitly. A ProviderContractError (an adapter returning a malformed
CompletionResult) is treated as a programming/implementation bug, not a
provider failure: it propagates immediately, unretried, with no fallback,
exactly like an entirely unexpected exception type would. asyncio.
CancelledError, KeyboardInterrupt, SystemExit, and GeneratorExit are all
BaseException subclasses (not Exception subclasses), so no except clause
here ever catches them -- they always propagate.

Timeout mechanism: asyncio.timeout() (3.11+) is used rather than
asyncio.wait_for(). If the timeout itself causes cancellation, it's
converted to a plain TimeoutError by asyncio.timeout()'s own __aexit__;
if cancellation instead originates from outside this coroutine (e.g. the
whole routing task is cancelled), asyncio.timeout() recognizes it did not
cause that cancellation and lets the original asyncio.CancelledError
propagate unchanged -- giving correct timeout-vs-external-cancellation
classification without hand-rolled detection.

Operational cost note: a timeout does NOT prove the provider performed no
work -- a request may have been received, processed, and even billed by
the provider before the client-side timeout fired. Retrying after a
timeout can therefore incur additional provider-side cost beyond what a
single reservation covers. The Step 25 max-of-candidates worst-case
estimate covers the cost of ONE successful attempt among the candidates --
it does NOT account for cumulative cost across multiple retried/timed-out
attempts against the same or fallback candidates. This is an explicitly
flagged, currently-unaddressed gap between the reservation ceiling and
true potential billed cost under retries; a later orchestration/cost-
accounting step MUST resolve this before the project is considered
production-ready. It is not solved here.

Pricing decoupling: this module never imports app.services.pricing, to
avoid any import cycle. Future orchestration is responsible for checking
that every route candidate has BOTH a registered adapter (checked here)
AND a pricing catalog entry (checked separately by the caller against
Step 25's catalog) before dispatching a request -- these two checks are
deliberately independent and composed elsewhere, not coupled here.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from app.providers.base import CompletionRequest, CompletionResult, ProviderAdapter, ProviderError

_MAX_ATTEMPTS_PER_CANDIDATE_CAP = 10
_MAX_CANDIDATES_CAP = 10
_MAX_BACKOFF_MS_CAP = 60_000  # 60 seconds


class RoutingError(Exception):
    """Base class for all routing-specific errors."""


class RoutingValidationError(RoutingError, ValueError):
    """Raised when a routing configuration input fails validation before use."""


class DuplicateProviderError(RoutingValidationError):
    """Raised when the provider registry is built with a duplicate provider name."""


class UnknownProviderError(RoutingValidationError):
    """Raised when a route candidate references a provider not in the registry."""


class UnsupportedModelError(RoutingValidationError):
    """Raised when a route candidate's model is not supported by its provider."""


class EmptyRouteError(RoutingValidationError):
    """Raised when a route has zero candidates."""


class DuplicateCandidateError(RoutingValidationError):
    """Raised when a route contains the same (provider, model) pair twice.

    Duplicates are always rejected: a route repeating the same candidate
    is redundant with RetryPolicy.max_attempts_per_candidate, which is
    the mechanism for retrying a single candidate, so there is no
    legitimate use case for a duplicated candidate slot in this step.
    """


class RetryableProviderError(ProviderError):
    """A provider failure that is safe to retry against the same candidate."""


class NonRetryableProviderError(ProviderError):
    """A provider failure that must not be retried; move to the next candidate immediately."""


class ProviderContractError(RoutingError):
    """Raised when a provider adapter returns a CompletionResult that
    violates its contract (wrong type, mismatched provider/model, or
    invalid completion_text/token/latency fields). Treated as an adapter
    programming error, NOT a provider failure -- it propagates immediately,
    is never retried, and never triggers fallback to another candidate.
    Error messages here name only field/identifier information, never
    prompt or completion content.
    """


class AllCandidatesFailedError(RoutingError):
    """Raised when every candidate in a route was exhausted without success.

    Carries the full (bounded) attempt history as safe operational
    metadata -- never prompts, completions, or secrets, since AttemptRecord
    itself never stores those fields.
    """

    def __init__(self, message: str, attempts: tuple["AttemptRecord", ...]) -> None:
        super().__init__(message)
        self.attempts = attempts

    def __repr__(self) -> str:
        return f"AllCandidatesFailedError(message={self.args[0]!r}, attempts={self.attempts!r})"


def _validate_identifier(label: str, value: str) -> None:
    if not isinstance(value, str):
        raise RoutingValidationError(f"{label} must be a str, got {type(value).__name__}")
    if not value.strip():
        raise RoutingValidationError(f"{label} must not be blank")


def _validate_int_range(label: str, value: int, *, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoutingValidationError(f"{label} must be a plain int, got {type(value).__name__}")
    if value < minimum or value > maximum:
        raise RoutingValidationError(f"{label} must be between {minimum} and {maximum} (got {value})")


def _validate_timeout_ms(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoutingValidationError(f"timeout_ms must be a plain int, got {type(value).__name__}")
    if value <= 0:
        raise RoutingValidationError("timeout_ms must be strictly greater than zero")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts_per_candidate: int
    base_backoff_ms: int
    max_backoff_ms: int
    jitter: bool = False

    def __post_init__(self) -> None:
        _validate_int_range(
            "max_attempts_per_candidate", self.max_attempts_per_candidate,
            minimum=1, maximum=_MAX_ATTEMPTS_PER_CANDIDATE_CAP,
        )
        _validate_int_range("base_backoff_ms", self.base_backoff_ms, minimum=0, maximum=_MAX_BACKOFF_MS_CAP)
        _validate_int_range("max_backoff_ms", self.max_backoff_ms, minimum=0, maximum=_MAX_BACKOFF_MS_CAP)
        if self.max_backoff_ms < self.base_backoff_ms:
            raise RoutingValidationError("max_backoff_ms must be >= base_backoff_ms")
        if not isinstance(self.jitter, bool):
            raise RoutingValidationError(f"jitter must be a bool, got {type(self.jitter).__name__}")


def compute_backoff_ms(policy: RetryPolicy, attempt_number: int) -> int:
    """Exponential backoff, capped at max_backoff_ms. Valid domain:
    attempt_number is the 1-indexed attempt that just failed, and MUST be
    strictly less than policy.max_attempts_per_candidate -- this function
    is only meaningful when a next attempt actually exists; calling it for
    the final permitted attempt (where there is nothing left to back off
    before) is a caller error and raises RoutingValidationError. With
    jitter disabled (the default, and what all deterministic tests use),
    this is a pure function of (policy, attempt_number) -- no randomness.
    """
    if not isinstance(policy, RetryPolicy):
        raise RoutingValidationError(f"policy must be a RetryPolicy, got {type(policy).__name__}")
    if isinstance(attempt_number, bool) or not isinstance(attempt_number, int):
        raise RoutingValidationError(f"attempt_number must be a plain int, got {type(attempt_number).__name__}")
    if attempt_number < 1:
        raise RoutingValidationError("attempt_number must be >= 1")
    if attempt_number >= policy.max_attempts_per_candidate:
        raise RoutingValidationError(
            "attempt_number must be less than policy.max_attempts_per_candidate "
            "(compute_backoff_ms is only valid when a next attempt exists)"
        )

    raw = policy.base_backoff_ms * (2 ** (attempt_number - 1))
    capped = min(raw, policy.max_backoff_ms)
    if policy.jitter and capped > 0:
        return random.randint(0, capped)
    return capped


@dataclass(frozen=True)
class ProviderRegistration:
    name: str
    adapter: ProviderAdapter
    supported_models: frozenset[str]

    def __post_init__(self) -> None:
        _validate_identifier("name", self.name)

        if not callable(getattr(self.adapter, "complete", None)):
            raise RoutingValidationError("adapter must implement a callable complete(request) method")

        adapter_name = getattr(self.adapter, "name", None)
        if not isinstance(adapter_name, str) or not adapter_name.strip():
            raise RoutingValidationError("adapter.name must exist and be a nonblank string")
        if adapter_name != self.name:
            raise RoutingValidationError(
                f"adapter.name ({adapter_name!r}) must exactly equal the registration name ({self.name!r})"
            )

        if not isinstance(self.supported_models, frozenset) or not self.supported_models:
            raise RoutingValidationError("supported_models must be a non-empty frozenset of model names")
        for model in self.supported_models:
            _validate_identifier("supported_models entry", model)


def build_provider_registry(
    registrations: tuple[ProviderRegistration, ...],
) -> Mapping[str, ProviderRegistration]:
    """Build an immutable name -> ProviderRegistration registry. Fails fast
    on a non-ProviderRegistration entry or a duplicate provider name.
    Mirrors app.services.pricing's build_pricing_catalog pattern
    deliberately, for architectural consistency across immutable-catalog-
    style modules in this project.
    """
    registry: dict[str, ProviderRegistration] = {}
    for reg in registrations:
        if not isinstance(reg, ProviderRegistration):
            raise RoutingValidationError(
                f"every registry entry must be a ProviderRegistration, got {type(reg).__name__}"
            )
        if reg.name in registry:
            raise DuplicateProviderError(f"duplicate provider registry entry for {reg.name!r}")
        registry[reg.name] = reg
    return MappingProxyType(registry)


@dataclass(frozen=True)
class RouteCandidate:
    provider: str
    model: str

    def __post_init__(self) -> None:
        _validate_identifier("provider", self.provider)
        _validate_identifier("model", self.model)


@dataclass(frozen=True)
class Route:
    candidates: tuple[RouteCandidate, ...]


def build_route(candidates) -> Route:
    """Validate structural correctness of a candidate list/tuple
    (non-empty, within the size cap, no duplicates), independent of any
    registry -- this lets a Route be constructed and unit-tested without a
    registry present. Registry-dependent checks (unknown provider,
    unsupported model) are a separate step: validate_route_against_registry.
    Rejects any input that is not itself a list or tuple (e.g. None, a
    str, or a Mapping) immediately, before any iteration -- otherwise a
    str would silently iterate into individual characters and a Mapping
    would iterate into its keys, both producing confusing downstream
    TypeErrors instead of a clean validation error.
    """
    if not isinstance(candidates, (list, tuple)):
        raise RoutingValidationError(
            f"candidates must be a list or tuple of RouteCandidate, got {type(candidates).__name__}"
        )
    candidates = tuple(candidates)
    if len(candidates) == 0:
        raise EmptyRouteError("route must contain at least one candidate")
    if len(candidates) > _MAX_CANDIDATES_CAP:
        raise RoutingValidationError(f"route must not exceed {_MAX_CANDIDATES_CAP} candidates")

    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if not isinstance(candidate, RouteCandidate):
            raise RoutingValidationError(f"each candidate must be a RouteCandidate, got {type(candidate).__name__}")
        key = (candidate.provider, candidate.model)
        if key in seen:
            raise DuplicateCandidateError(
                f"duplicate route candidate: provider={candidate.provider!r} model={candidate.model!r}"
            )
        seen.add(key)

    return Route(candidates=candidates)


def validate_route_against_registry(registry: Mapping[str, ProviderRegistration], route: Route) -> None:
    for candidate in route.candidates:
        registration = registry.get(candidate.provider)
        if registration is None:
            raise UnknownProviderError(f"unknown provider: {candidate.provider!r}")
        if candidate.model not in registration.supported_models:
            raise UnsupportedModelError(
                f"provider {candidate.provider!r} does not support model {candidate.model!r}"
            )


class AttemptOutcome(Enum):
    SUCCESS = "success"
    FAILURE = "failure"


class FailureCategory(Enum):
    TIMEOUT = "timeout"
    RETRYABLE_PROVIDER_ERROR = "retryable_provider_error"
    NON_RETRYABLE_PROVIDER_ERROR = "non_retryable_provider_error"


@dataclass(frozen=True)
class AttemptRecord:
    """Safe operational metadata only -- deliberately holds no prompt,
    completion, or secret material, so it never needs redaction anywhere
    it's logged, repr'd, or attached to AllCandidatesFailedError.
    """
    provider: str
    model: str
    attempt_number: int
    outcome: AttemptOutcome
    failure_category: FailureCategory | None
    elapsed_ms: int


@dataclass(frozen=True)
class RouteResult:
    primary_provider: str
    primary_model: str
    final_provider: str
    final_model: str
    fallback_used: bool
    result: CompletionResult
    total_latency_ms: int
    attempts: tuple[AttemptRecord, ...]


def _validate_nonneg_plain_int(label: str, value) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderContractError(f"{label} must be a plain int, got {type(value).__name__}")
    if value < 0:
        raise ProviderContractError(f"{label} must be >= 0")


def _validate_completion_result(candidate: RouteCandidate, result) -> None:
    if not isinstance(result, CompletionResult):
        raise ProviderContractError(f"provider {candidate.provider!r} returned a non-CompletionResult object")
    if result.provider != candidate.provider:
        raise ProviderContractError(f"provider {candidate.provider!r} returned a result naming a different provider")
    if result.model != candidate.model:
        raise ProviderContractError(f"provider {candidate.provider!r} returned a result naming a different model")
    if not isinstance(result.completion_text, str):
        raise ProviderContractError(f"provider {candidate.provider!r} returned non-str completion_text")
    _validate_nonneg_plain_int("prompt_tokens", result.prompt_tokens)
    _validate_nonneg_plain_int("completion_tokens", result.completion_tokens)
    _validate_nonneg_plain_int("latency_ms", result.latency_ms)


async def _run_candidate(
    registration: ProviderRegistration,
    candidate: RouteCandidate,
    prompt: str,
    max_tokens: int,
    retry_policy: RetryPolicy,
    timeout_seconds: float,
    sleep_fn: Callable[[float], Awaitable[None]],
    attempts: list[AttemptRecord],
) -> CompletionResult | None:
    for attempt_number in range(1, retry_policy.max_attempts_per_candidate + 1):
        request = CompletionRequest(model=candidate.model, prompt=prompt, max_tokens=max_tokens)
        start = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await registration.adapter.complete(request)
        except TimeoutError:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            attempts.append(
                AttemptRecord(candidate.provider, candidate.model, attempt_number,
                               AttemptOutcome.FAILURE, FailureCategory.TIMEOUT, elapsed_ms)
            )
            if attempt_number < retry_policy.max_attempts_per_candidate:
                await sleep_fn(compute_backoff_ms(retry_policy, attempt_number) / 1000)
                continue
            return None
        except NonRetryableProviderError:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            attempts.append(
                AttemptRecord(candidate.provider, candidate.model, attempt_number,
                               AttemptOutcome.FAILURE, FailureCategory.NON_RETRYABLE_PROVIDER_ERROR, elapsed_ms)
            )
            return None  # non-retryable: abandon this candidate immediately, no backoff sleep
        except RetryableProviderError:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            attempts.append(
                AttemptRecord(candidate.provider, candidate.model, attempt_number,
                               AttemptOutcome.FAILURE, FailureCategory.RETRYABLE_PROVIDER_ERROR, elapsed_ms)
            )
            if attempt_number < retry_policy.max_attempts_per_candidate:
                await sleep_fn(compute_backoff_ms(retry_policy, attempt_number) / 1000)
                continue
            return None
        except ProviderError:
            # A bare/base ProviderError, not one of the two typed subclasses
            # above: treated as non-retryable by default (fail-safe).
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            attempts.append(
                AttemptRecord(candidate.provider, candidate.model, attempt_number,
                               AttemptOutcome.FAILURE, FailureCategory.NON_RETRYABLE_PROVIDER_ERROR, elapsed_ms)
            )
            return None
        else:
            # Validate the contract BEFORE recording success. A raised
            # ProviderContractError here is NOT caught by any except
            # clause above (exceptions from an `else` block are not
            # caught by that same try's except clauses) -- it propagates
            # immediately out of _run_candidate and execute_route,
            # exactly once, with no retry and no fallback.
            _validate_completion_result(candidate, result)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            attempts.append(
                AttemptRecord(candidate.provider, candidate.model, attempt_number,
                               AttemptOutcome.SUCCESS, None, elapsed_ms)
            )
            return result
        # Note: asyncio.CancelledError, KeyboardInterrupt, SystemExit, and
        # GeneratorExit are BaseException subclasses, not caught by any
        # except clause above -- they always propagate immediately.
    return None


def _validate_execute_route_inputs(
    registry, route, retry_policy, prompt, max_tokens, timeout_ms, sleep_fn
) -> None:
    if not isinstance(registry, Mapping):
        raise RoutingValidationError(f"registry must be a Mapping, got {type(registry).__name__}")
    if not isinstance(route, Route):
        raise RoutingValidationError(f"route must be a Route, got {type(route).__name__}")
    if not isinstance(retry_policy, RetryPolicy):
        raise RoutingValidationError(f"retry_policy must be a RetryPolicy, got {type(retry_policy).__name__}")
    if not isinstance(prompt, str):
        raise RoutingValidationError(f"prompt must be a str, got {type(prompt).__name__}")
    if not prompt.strip():
        raise RoutingValidationError("prompt must not be blank")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
        raise RoutingValidationError(f"max_tokens must be a plain int, got {type(max_tokens).__name__}")
    if max_tokens < 1:
        raise RoutingValidationError("max_tokens must be >= 1")
    _validate_timeout_ms(timeout_ms)
    if not callable(sleep_fn):
        raise RoutingValidationError("sleep_fn must be callable")


async def execute_route(
    registry: Mapping[str, ProviderRegistration],
    route: Route,
    prompt: str,
    max_tokens: int,
    retry_policy: RetryPolicy,
    timeout_ms: int,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RouteResult:
    """Execute a route: try each candidate in order, applying retry_policy
    and a per-attempt timeout_ms to each. Returns on the first success.
    Raises AllCandidatesFailedError if every candidate is exhausted. All
    inputs are fully validated (structurally, then against the registry)
    before any provider is ever called.
    """
    _validate_execute_route_inputs(registry, route, retry_policy, prompt, max_tokens, timeout_ms, sleep_fn)
    validate_route_against_registry(registry, route)

    timeout_seconds = timeout_ms / 1000
    attempts: list[AttemptRecord] = []
    route_start = time.perf_counter()

    for candidate_index, candidate in enumerate(route.candidates):
        registration = registry[candidate.provider]
        result = await _run_candidate(
            registration, candidate, prompt, max_tokens, retry_policy, timeout_seconds, sleep_fn, attempts
        )
        if result is not None:
            total_latency_ms = int((time.perf_counter() - route_start) * 1000)
            primary = route.candidates[0]
            return RouteResult(
                primary_provider=primary.provider,
                primary_model=primary.model,
                final_provider=candidate.provider,
                final_model=candidate.model,
                fallback_used=candidate_index > 0,
                result=result,
                total_latency_ms=total_latency_ms,
                attempts=tuple(attempts),
            )

    raise AllCandidatesFailedError("all route candidates failed", attempts=tuple(attempts))
