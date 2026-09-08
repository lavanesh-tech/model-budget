"""
POST /v1/chat/completions -- the complete authenticated inference
workflow. This is the third revision; see the corrections below.

--- Step 30 v3 corrections ---

Replay-first ordering (fixes: a replay of an old COMPLETED/FAILED
request could be wrongly rejected by "new request" checks): the
handler now resolves an EXISTING idempotency record (by team_id +
idempotency_key) BEFORE doing anything that only makes sense for a
genuinely new request -- unknown-model validation, budget-period
lookup, token counting, and reservation sizing. A request replaying an
already-COMPLETED result returns the stored snapshot even if the
model has since been removed from the registry, or the budget period
that funded the original request has since ended -- exactly matching
how a real payment-idempotency system (e.g. Stripe's) behaves: the
stored result is authoritative, current server config is irrelevant to
replaying it.

Fingerprint stability when model is omitted (fixes: a fingerprint that
depended on the RESOLVED default model would silently change if the
server's configured default model ever changed, breaking replay of a
request that omitted `model`): the fingerprint is computed from
`body.model` EXACTLY as the client sent it (which may be None), never
from the server-resolved default. Two requests are "the same request"
based on what the client actually specified, not on what the server's
current configuration happens to resolve that to.

Idempotency-key format validated before any external call (fixes: a
malformed key could previously trigger a real OpenAI token-counting
call before ever being rejected): `validate_idempotency_key_format` is
called immediately after checking the header is present, before
authentication even runs.

Token counting -- explicit timeout, safe error handling, cancellation
preserved (fixes: an unguarded, unhandled await): the authoritative
token-counting call now runs inside its own `asyncio.timeout(...)` and
its exceptions are explicitly caught and mapped to safe HTTP responses,
exactly like the main provider call is. `asyncio.CancelledError` is
never caught. It is skipped entirely for any request that turns out to
already have an existing idempotency row (a replay/conflict/in-progress
case never needs a token count at all).

Cost-overrun / response-construction-failure handling (fixes: known
billed usage being replaced with actual_cost=0): actual_cost is now
computed from the provider's real returned usage IMMEDIATELY after a
successful call, before anything else that could fail. If ONLY response
construction fails afterward, the already-computed, REAL actual_cost is
still used when settling -- never zero. See app.services.settlement's
own module docstring for the full accounting policy: the team is never
charged more than its reservation (refund capped at zero on overrun),
but the true actual_cost is always recorded, never discarded.

claimed=False inspected on EVERY settlement call, not only the success
path (fixes: failure-settlement paths previously ignored this): a
shared helper resolves and returns the authoritative stored state
whenever a settlement call reports claimed=False, whether that call was
settle_success or settle_failure.

Uncertain upstream cost, honestly labeled (fixes: an exhausted-retries
failure implied a "confirmed" zero cost): when every route candidate
is exhausted (timeouts/connection failures across retries), this
project genuinely does not know whether OpenAI billed for any of the
failed attempts. The team is not charged for this (a deliberate policy
choice -- see app.services.settlement), but the stored error_code and
the client-facing message now say so honestly ("could not be
confirmed"), rather than implying the zero was a verified fact. This is
distinct from `settlement_unresolved` (below), which is about OUR OWN
database state being unresolved, not OpenAI's.

settlement_unresolved vs. confirmed failure (unchanged from v2, restated):
if a settlement attempt itself raises (fails to commit), that is
DISTINCT from a confirmed, committed refund -- the client is told the
accounting state is unresolved and is explicitly NOT invited to retry,
since the reservation's fate is unknown, not confirmed released.

Async execution boundary (unchanged from v2): every synchronous block
runs via `await asyncio.to_thread(...)`. Only genuinely async I/O
(execute_route, the token-counting call) runs directly on the event
loop.

Crash window (unchanged, restated): if the process crashes after
Transaction 1 commits but before Transaction 2 ever runs, the
idempotency row is left PENDING with its reservation deducted.
app.services.idempotency's EXPIRED_PENDING outcome is the only
recognition of this state once the row's TTL lapses. The accurate
guarantee: idempotent database acquisition with at-most-one active
owner during normal execution, not exactly-once execution across an
arbitrary crash, and not exactly-once OpenAI billing.
"""

import asyncio
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import IdempotencyKey
from app.models.enums import IdempotencyStatus
from app.security.auth import authenticate
from app.services.budget import get_current_budget_period, reserve_budget
from app.services.idempotency import (
    AcquireOutcome,
    IdempotencyConflictError,
    IdempotencyValidationError,
    acquire_idempotency_slot,
    compute_request_fingerprint,
    validate_idempotency_key_format,
)
from app.services.pricing import (
    PricingValidationError,
    UnknownModelError,
    calculate_actual_cost,
    calculate_cost_for_pricing,
    get_model_pricing,
)
from app.services.routing import (
    AllCandidatesFailedError,
    FailureCategory,
    NonRetryableProviderError,
    ProviderRegistration,
    RetryableProviderError,
    RetryPolicy,
    RouteCandidate,
    RoutingValidationError,
    build_route,
    execute_route,
    validate_route_against_registry,
)
from app.services.settlement import SettlementResult, settle_failure, settle_success

router = APIRouter()

_DEFAULT_IDEMPOTENCY_TTL = timedelta(minutes=5)
_MAX_PROMPT_CHARS = 20_000
_MAX_MAX_TOKENS = 32_000
_COUNTING_TIMEOUT_MS = 15_000


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: str | None = None
    prompt: str
    max_tokens: int
    stream: bool = False


class ChatCompletionUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int
    completion_tokens: int


class ChatCompletionResponseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    model: str
    provider: str
    completion: str
    usage: ChatCompletionUsage
    actual_cost: str
    fallback_used: bool
    status: str = "succeeded"


def get_provider_registry(request: Request) -> Mapping[str, ProviderRegistration]:
    registry = getattr(request.app.state, "provider_registry", None)
    if registry is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "no_provider_configured", "message": "no inference provider is configured"},
        )
    return registry


def get_retry_policy(request: Request) -> RetryPolicy:
    return request.app.state.retry_policy


def get_timeout_ms(request: Request) -> int:
    return request.app.state.provider_timeout_ms


def get_session_factory(request: Request) -> Callable[[], Session]:
    return getattr(request.app.state, "session_factory", SessionLocal)


def _default_model_for_registry(registry: Mapping[str, ProviderRegistration]) -> str:
    registration = registry.get("openai")
    if registration is None or not registration.supported_models:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "no_provider_configured", "message": "no inference provider is configured"},
        )
    return next(iter(registration.supported_models))


# --- Synchronous helpers, always invoked via asyncio.to_thread ---------

def _authenticate_sync(session_factory: Callable[[], Session], authorization: str | None):
    session = session_factory()
    try:
        return authenticate(session, authorization)
    finally:
        session.close()


@dataclass(frozen=True)
class _ExistingRowOutcome:
    """Result of resolving an ALREADY-EXISTING idempotency row, before
    any new-request-only work happens. `kind` is one of: "none" (no row
    -- genuinely new request), "response" (a ready-to-return
    ChatCompletionResponseBody -- a COMPLETED replay), "error" (an
    HTTPException to raise -- conflict/in-progress/expired/failed-replay).
    """
    kind: str
    response: "ChatCompletionResponseBody | None" = None
    error: HTTPException | None = None


def _resolve_existing_row_sync(
    session_factory: Callable[[], Session], team_id, idempotency_key: str, request_hash: str
) -> _ExistingRowOutcome:
    session = session_factory()
    try:
        row = session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.team_id == team_id, IdempotencyKey.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()

        if row is None:
            return _ExistingRowOutcome(kind="none")

        if row.request_hash != request_hash:
            return _ExistingRowOutcome(kind="error", error=HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "idempotency_conflict",
                    "message": "Idempotency-Key was already used with a different request",
                },
            ))

        if row.status == IdempotencyStatus.COMPLETED:
            return _ExistingRowOutcome(kind="response", response=ChatCompletionResponseBody(**row.response_snapshot))

        if row.status == IdempotencyStatus.FAILED:
            return _ExistingRowOutcome(kind="error", error=HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "provider_error",
                    "message": "this request previously failed",
                    "error_code": row.error_code,
                },
                headers={"Idempotent-Replayed": "true"},
            ))

        # PENDING
        now = datetime.now(timezone.utc)
        if row.expires_at <= now:
            return _ExistingRowOutcome(kind="error", error=HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "idempotency_key_expired_pending",
                    "message": (
                        "the original request for this idempotency key expired before completing; "
                        "recovery is not supported -- retry with a new Idempotency-Key"
                    ),
                },
            ))
        return _ExistingRowOutcome(kind="error", error=HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "request_in_progress", "message": "an identical request is already being processed"},
        ))
    finally:
        session.close()


@dataclass(frozen=True)
class _Txn1Acquired:
    idempotency_key_id: uuid.UUID


def _run_txn1_sync(
    session_factory: Callable[[], Session],
    team_id,
    idempotency_key: str,
    request_hash: str,
    reserved_cost: Decimal,
):
    """Only reached for a request our precheck saw as genuinely new. A
    race against a concurrent duplicate is still possible here (the
    precheck is a read, not a lock) -- acquire_idempotency_slot remains
    the sole atomic authority, and every one of its outcomes is handled
    here too, exactly as in _resolve_existing_row_sync, for that rare
    race window.
    """
    txn = session_factory()
    try:
        today = datetime.now(timezone.utc).date()
        budget_period = get_current_budget_period(txn, team_id, today)
        if budget_period is None:
            txn.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "no_active_budget_period", "message": "no active budget period for this team"},
            )

        try:
            acquire_result = acquire_idempotency_slot(
                txn, team_id, budget_period.period_start, idempotency_key,
                request_hash, reserved_cost, _DEFAULT_IDEMPOTENCY_TTL,
            )
        except IdempotencyConflictError:
            txn.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "idempotency_conflict",
                    "message": "Idempotency-Key was already used with a different request",
                },
            ) from None

        if acquire_result.outcome == AcquireOutcome.ACQUIRED:
            if reserved_cost > 0:
                reserved = reserve_budget(txn, team_id, budget_period.period_start, reserved_cost)
                if not reserved:
                    txn.rollback()
                    raise HTTPException(
                        status.HTTP_402_PAYMENT_REQUIRED,
                        detail={
                            "code": "insufficient_budget",
                            "message": "insufficient remaining budget for this request",
                        },
                    )
            txn.commit()
            return _Txn1Acquired(idempotency_key_id=acquire_result.record.id)

        if acquire_result.outcome == AcquireOutcome.ALREADY_COMPLETED:
            snapshot = acquire_result.record.response_snapshot
            txn.rollback()
            return ChatCompletionResponseBody(**snapshot)

        if acquire_result.outcome == AcquireOutcome.ALREADY_FAILED:
            error_code = acquire_result.record.error_code
            txn.rollback()
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail={"code": "provider_error", "message": "this request previously failed", "error_code": error_code},
                headers={"Idempotent-Replayed": "true"},
            )

        if acquire_result.outcome == AcquireOutcome.IN_PROGRESS:
            txn.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "request_in_progress", "message": "an identical request is already being processed"},
            )

        txn.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "idempotency_key_expired_pending",
                "message": (
                    "the original request for this idempotency key expired before completing; "
                    "recovery is not supported -- retry with a new Idempotency-Key"
                ),
            },
        )
    finally:
        txn.close()


def _settle_success_sync(session_factory, idempotency_key_id, **kwargs) -> SettlementResult:
    txn = session_factory()
    try:
        result = settle_success(txn, idempotency_key_id=idempotency_key_id, **kwargs)
        txn.commit()
        return result
    except Exception:
        txn.rollback()
        raise
    finally:
        txn.close()


def _settle_failure_sync(session_factory, idempotency_key_id, **kwargs) -> SettlementResult:
    txn = session_factory()
    try:
        result = settle_failure(txn, idempotency_key_id=idempotency_key_id, **kwargs)
        txn.commit()
        return result
    except Exception:
        txn.rollback()
        raise
    finally:
        txn.close()


def _fetch_authoritative_state_sync(session_factory, idempotency_key_id: uuid.UUID):
    session = session_factory()
    try:
        row = session.get(IdempotencyKey, idempotency_key_id)
        if row is None:
            return None, None, None
        return row.status, row.response_snapshot, row.error_code
    finally:
        session.close()


def _unresolved_settlement_error(idempotency_key_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "code": "settlement_unresolved",
            "message": (
                "the request could not be safely settled; its accounting state is unresolved. "
                "Do not assume a refund occurred. Contact support with this request identifier."
            ),
            "idempotency_key_id": str(idempotency_key_id),
        },
    )


async def _settle_and_resolve(
    session_factory: Callable[[], Session],
    idempotency_key_id: uuid.UUID,
    *,
    success: bool,
    **kwargs,
) -> SettlementResult:
    """Runs settle_success or settle_failure via to_thread. If the
    settlement call itself raises, converts that to
    _unresolved_settlement_error (never claims a refund occurred that
    did not commit). Does NOT yet inspect `claimed` -- see
    _resolve_authoritative_state_if_not_claimed for that, called
    separately by the caller once it knows what response to build on
    top of a `claimed=True` vs `claimed=False` result.
    """
    fn = _settle_success_sync if success else _settle_failure_sync
    try:
        return await asyncio.to_thread(fn, session_factory, idempotency_key_id, **kwargs)
    except Exception:
        raise _unresolved_settlement_error(idempotency_key_id) from None


async def _resolve_authoritative_state_if_not_claimed(
    session_factory: Callable[[], Session], idempotency_key_id: uuid.UUID, settle_result: SettlementResult
) -> "ChatCompletionResponseBody | None":
    """If settle_result.claimed is True, returns None (caller proceeds
    normally). If False, fetches and returns/raises the authoritative
    stored state instead of trusting any locally-computed narrative --
    used identically whether the settlement attempt was a success or a
    failure path.
    """
    if settle_result.claimed:
        return None
    status_value, snapshot, error_code = await asyncio.to_thread(
        _fetch_authoritative_state_sync, session_factory, idempotency_key_id
    )
    if status_value == IdempotencyStatus.COMPLETED and snapshot is not None:
        return ChatCompletionResponseBody(**snapshot)
    if status_value == IdempotencyStatus.FAILED:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"code": "provider_error", "message": "this request previously failed", "error_code": error_code},
            headers={"Idempotent-Replayed": "true"},
        )
    raise _unresolved_settlement_error(idempotency_key_id)


@router.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponseBody,
    status_code=status.HTTP_200_OK,
)
async def create_chat_completion(
    body: ChatCompletionRequest,
    response: Response,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    registry: Mapping[str, ProviderRegistration] = Depends(get_provider_registry),
    retry_policy: RetryPolicy = Depends(get_retry_policy),
    timeout_ms: int = Depends(get_timeout_ms),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> ChatCompletionResponseBody:
    # --- 1. Structural request validation -- applies to every request,
    #        replay or new, since it is about well-formedness, not
    #        current server configuration. ---
    if body.stream:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "streaming_not_supported", "message": "stream=true is not supported"},
        )
    if not body.prompt.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_request", "message": "prompt must not be blank"},
        )
    if len(body.prompt) > _MAX_PROMPT_CHARS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_request", "message": f"prompt must not exceed {_MAX_PROMPT_CHARS} characters"},
        )
    if body.max_tokens < 1 or body.max_tokens > _MAX_MAX_TOKENS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_request", "message": f"max_tokens must be between 1 and {_MAX_MAX_TOKENS}"},
        )
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "idempotency_key_required", "message": "Idempotency-Key header is required"},
        )
    try:
        validate_idempotency_key_format(idempotency_key)
    except IdempotencyValidationError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_idempotency_key", "message": "Idempotency-Key is invalid"},
        ) from None

    # --- 2. Authentication: own short-lived session, off the event loop. ---
    auth = await asyncio.to_thread(_authenticate_sync, session_factory, authorization)
    team_id = auth.team.id

    # --- 3. Fingerprint from EXACTLY what the client sent (body.model,
    #        which may be None) -- never the server-resolved default, so
    #        replay stays stable even if the default model changes. ---
    try:
        request_hash = compute_request_fingerprint(
            {"model": body.model, "prompt": body.prompt, "max_tokens": body.max_tokens}
        )
    except IdempotencyValidationError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_request", "message": "request payload could not be fingerprinted"},
        ) from None

    # --- 4. REPLAY RESOLUTION FIRST -- before any "new request only"
    #        requirement (unknown-model check, budget period, token
    #        counting) is ever evaluated. ---
    existing = await asyncio.to_thread(
        _resolve_existing_row_sync, session_factory, team_id, idempotency_key, request_hash
    )
    if existing.kind == "response":
        response.headers["Idempotent-Replayed"] = "true"
        return existing.response
    if existing.kind == "error":
        raise existing.error
    # existing.kind == "none" -- genuinely new request. Continue below.

    # --- 5. Requirements that apply ONLY to a genuinely new request. ---
    model = body.model or _default_model_for_registry(registry)

    try:
        pricing = get_model_pricing("openai", model)
    except UnknownModelError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "unknown_model", "message": "requested model is not supported"},
        ) from None

    try:
        route = build_route([RouteCandidate("openai", model)])
        validate_route_against_registry(registry, route)
    except RoutingValidationError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "unknown_model", "message": "requested model is not supported by this deployment"},
        ) from None

    # --- 6. Authoritative token count, with its own explicit timeout
    #        and safe error handling. Cancellation is never caught. ---
    adapter = registry["openai"].adapter
    count_fn = getattr(adapter, "count_input_tokens", None)
    if callable(count_fn):
        try:
            async with asyncio.timeout(_COUNTING_TIMEOUT_MS / 1000):
                prompt_token_count = await count_fn(model, body.prompt)
        except TimeoutError:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "provider_unavailable", "message": "token counting timed out; retry later"},
            ) from None
        except RetryableProviderError:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "provider_unavailable", "message": "token counting failed; retry later"},
            ) from None
        except NonRetryableProviderError:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail={"code": "provider_error", "message": "token counting was rejected by the provider"},
            ) from None
        # asyncio.CancelledError, and any exception type not explicitly
        # matched above, is intentionally NOT caught here -- it
        # propagates. No idempotency row or reservation exists yet at
        # this point, so there is nothing to settle or refund.
    else:
        prompt_token_count = len(body.prompt)  # non-authoritative fallback, test fakes only

    attempts = retry_policy.max_attempts_per_candidate * len(route.candidates)
    try:
        reserved_cost = calculate_cost_for_pricing(
            pricing, prompt_token_count * attempts, body.max_tokens * attempts
        )
    except PricingValidationError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_request",
                "message": "request is too large to safely reserve under the configured retry policy",
            },
        ) from None

    # --- Transaction 1 (off the event loop): budget lookup, idempotency
    #     acquisition, reservation. ---
    txn1_result = await asyncio.to_thread(
        _run_txn1_sync, session_factory, team_id, idempotency_key, request_hash, reserved_cost
    )
    if isinstance(txn1_result, ChatCompletionResponseBody):
        response.headers["Idempotent-Replayed"] = "true"
        return txn1_result

    idempotency_key_id = txn1_result.idempotency_key_id

    # --- Provider execution: no database session held during this call. ---
    try:
        route_result = await execute_route(registry, route, body.prompt, body.max_tokens, retry_policy, timeout_ms)
    except AllCandidatesFailedError as exc:
        last_category = exc.attempts[-1].failure_category if exc.attempts else None
        settle_result = await _settle_and_resolve(
            session_factory, idempotency_key_id, success=False,
            actual_cost=Decimal("0"), error_code="RETRIES_EXHAUSTED_COST_UNKNOWN",
            primary_provider="openai", primary_model=model, latency_ms=0,
        )
        replay = await _resolve_authoritative_state_if_not_claimed(session_factory, idempotency_key_id, settle_result)
        if replay is not None:
            response.headers["Idempotent-Replayed"] = "true"
            return replay
        # settle_result.claimed is True: our own accounting state IS
        # confirmed resolved (the reservation was released), even though
        # OpenAI's true billing for the exhausted attempts could not be
        # confirmed -- see module docstring for why these are different
        # things. The team was not charged for this request.
        if last_category in (FailureCategory.RETRYABLE_PROVIDER_ERROR, FailureCategory.TIMEOUT):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "provider_unavailable",
                    "message": (
                        "the provider was unavailable after retries; you were not charged. "
                        "Upstream billing for the failed attempts could not be confirmed. "
                        "Retry later with a new Idempotency-Key."
                    ),
                },
            ) from None
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "provider_error",
                "message": "the provider rejected or could not complete the request; you were not charged",
            },
        ) from None
    except asyncio.CancelledError:
        # Preserve cancellation exactly. The reservation is deliberately
        # left untouched: we do not know OpenAI's billing outcome.
        raise
    except Exception:
        settle_result = await _settle_and_resolve(
            session_factory, idempotency_key_id, success=False,
            actual_cost=Decimal("0"), error_code="INTERNAL_ERROR",
            primary_provider="openai", primary_model=model, latency_ms=0,
        )
        replay = await _resolve_authoritative_state_if_not_claimed(session_factory, idempotency_key_id, settle_result)
        if replay is not None:
            response.headers["Idempotent-Replayed"] = "true"
            return replay
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "internal_error", "message": "an unexpected internal error occurred"},
        ) from None

    # --- Actual cost, computed FIRST from authoritative provider usage,
    #     before anything else that could fail -- so it is never lost. ---
    try:
        actual_cost = calculate_actual_cost(
            "openai", model, route_result.result.prompt_tokens, route_result.result.completion_tokens
        )
    except Exception:
        # Genuinely last-resort: even the known, real token counts could
        # not be priced. actual_cost is not knowable here; distinctly
        # labeled from every other failure code.
        settle_result = await _settle_and_resolve(
            session_factory, idempotency_key_id, success=False,
            actual_cost=Decimal("0"), error_code="COST_CALCULATION_FAILED",
            primary_provider="openai", primary_model=model, latency_ms=route_result.total_latency_ms,
        )
        replay = await _resolve_authoritative_state_if_not_claimed(session_factory, idempotency_key_id, settle_result)
        if replay is not None:
            response.headers["Idempotent-Replayed"] = "true"
            return replay
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "internal_error", "message": "an unexpected internal error occurred"},
        ) from None

    try:
        response_body = ChatCompletionResponseBody(
            id=str(idempotency_key_id),
            model=route_result.result.model,
            provider=route_result.result.provider,
            completion=route_result.result.completion_text,
            usage=ChatCompletionUsage(
                prompt_tokens=route_result.result.prompt_tokens,
                completion_tokens=route_result.result.completion_tokens,
            ),
            actual_cost=str(actual_cost),
            fallback_used=route_result.fallback_used,
            status="succeeded",
        )
    except Exception:
        # A real completion was produced and actual_cost IS known (computed
        # above) -- settle with the REAL cost, never zero, even though the
        # response body itself could not be constructed.
        settle_result = await _settle_and_resolve(
            session_factory, idempotency_key_id, success=False,
            actual_cost=actual_cost, error_code="RESPONSE_CONSTRUCTION_FAILED",
            primary_provider=route_result.primary_provider, primary_model=route_result.primary_model,
            final_provider=route_result.final_provider, final_model=route_result.final_model,
            prompt_tokens=route_result.result.prompt_tokens, completion_tokens=route_result.result.completion_tokens,
            latency_ms=route_result.total_latency_ms, fallback_used=route_result.fallback_used,
        )
        replay = await _resolve_authoritative_state_if_not_claimed(session_factory, idempotency_key_id, settle_result)
        if replay is not None:
            response.headers["Idempotent-Replayed"] = "true"
            return replay
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "internal_error", "message": "an unexpected internal error occurred"},
        ) from None

    # --- Transaction 2 (off the event loop): settlement. ---
    settle_result = await _settle_and_resolve(
        session_factory, idempotency_key_id, success=True,
        actual_cost=actual_cost, response_snapshot=response_body.model_dump(),
        primary_provider=route_result.primary_provider, primary_model=route_result.primary_model,
        final_provider=route_result.final_provider, final_model=route_result.final_model,
        prompt_tokens=route_result.result.prompt_tokens, completion_tokens=route_result.result.completion_tokens,
        latency_ms=route_result.total_latency_ms, fallback_used=route_result.fallback_used,
    )
    replay = await _resolve_authoritative_state_if_not_claimed(session_factory, idempotency_key_id, settle_result)
    if replay is not None:
        response.headers["Idempotent-Replayed"] = "true"
        return replay

    return response_body
