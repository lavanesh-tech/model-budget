"""
POST /v1/chat/completions -- the complete authenticated inference
workflow. This is the fifth revision; see the corrections/additions
below (earlier revision notes are preserved for history).

--- Step 30 v3 corrections ---

Replay-first ordering: an EXISTING idempotency record (by team_id +
idempotency_key) is resolved BEFORE anything that only makes sense for a
genuinely new request -- unknown-model validation, budget-period
lookup, token counting, reservation sizing, AND (Step 32) rate
limiting. A request replaying an already-COMPLETED result returns the
stored snapshot even if the model has since been removed from the
registry, the budget period that funded it has since ended, or the team
is currently rate-limited -- the stored result is authoritative, current
server config/state is irrelevant to replaying it.

Fingerprint stability when model is omitted: the fingerprint is computed
from `body.model` EXACTLY as the client sent it (which may be None),
never from the server-resolved default.

Idempotency-key format validated before any external call:
`validate_idempotency_key_format` runs immediately after checking the
header is present, before authentication even runs.

Token counting -- explicit timeout, safe error handling, cancellation
preserved: runs inside its own `asyncio.timeout(...)` with exceptions
explicitly caught and mapped to safe HTTP responses.
`asyncio.CancelledError` is never caught, and counting is skipped
entirely for any request with an existing idempotency row.

Cost-overrun / response-construction-failure handling: actual_cost is
computed from the provider's real returned usage IMMEDIATELY after a
successful call, before anything else that could fail. See
app.services.settlement's own module docstring for the full accounting
policy.

claimed=False inspected on EVERY settlement call, not only the success
path.

Uncertain upstream cost, honestly labeled: an exhausted-retries failure
never implies a "confirmed" zero cost.

settlement_unresolved vs. confirmed failure: distinguished explicitly --
a settlement attempt that itself fails to commit is never reported to
the client as a confirmed refund.

Async execution boundary: every synchronous (Postgres) block runs via
`await asyncio.to_thread(...)`. Genuinely async I/O -- execute_route,
token counting, and (Step 32) the Redis rate-limit check -- is awaited
directly on the event loop, since none of it is blocking.

Crash window: if the process crashes after Transaction 1 commits but
before Transaction 2 ever runs, the idempotency row is left PENDING with
its reservation deducted. The accurate guarantee: idempotent database
acquisition with at-most-one active owner during normal execution, not
exactly-once execution across an arbitrary crash, and not exactly-once
OpenAI billing.

--- Step 31 addition ---

Structured logging: one logger ("app.chat_completions") emits a small,
fixed set of safe-field log events at each major transition. Every call
site passes an explicit, reviewed set of keyword arguments via
extra={...} -- never a whole request/response/row object -- so it is
structurally impossible for a prompt, completion, Authorization header,
secret_hash, or (Step 32) Redis URL to end up in a log line by accident.

--- Step 32 (Redis redesign) ---

Backend: PostgreSQL was the original Step 32 design; this REPLACES it
entirely with Redis, for the standard distributed-rate-limiting pattern
this project is meant to demonstrate (Redis for ephemeral rate state,
deployed later as AWS ElastiCache for Redis; Postgres remains the
durable system of record for everything else). No Postgres model,
migration, or table exists for rate limiting.

Placement: checked immediately after replay resolution (existing.kind
== "none", i.e. only for a genuinely new request), BEFORE unknown-model
validation, token counting, or any reservation work -- the cheapest
possible rejection point among the "new request only" checks, and one
that correctly never blocks a replay.

Atomicity and multi-instance correctness: app.services.rate_limit's
RedisRateLimiter runs one atomic Lua script per check (sliding-window
log via a Redis sorted set) -- see that module's docstring for the full
algorithm and rationale. Redis, not any single FastAPI process, is the
shared state, so this is correct under concurrent requests from any
number of application instances.

Fail-closed policy (explicit, tested): RateLimitUnavailableError (Redis
unreachable or erroring) is mapped to a 503 "rate_limiter_unavailable"
response -- the request is REJECTED, never silently allowed through
unlimited. See app.services.rate_limit's module docstring for why this
is the deliberate choice for a budget-protection gateway specifically.

Correlation ID reuse: the Lua script's unique per-request sorted-set
member is the SAME request_id already assigned by
app.main.RequestIDMiddleware (via request.state.request_id) -- reusing
the existing correlation ID rather than generating a second one.

Headers: standard X-RateLimit-Limit / X-RateLimit-Remaining /
X-RateLimit-Reset headers are set on EVERY genuinely-new request's
response (allowed or rejected), plus Retry-After specifically on the
429 response.

Rejected-request guarantee: because this check happens before token
counting, budget reservation, idempotency acquisition, or any provider
call, a 429 (or a 503 from a failed-closed unavailable check) is
guaranteed to have caused none of: an OpenAI call, a budget reservation,
a new idempotency row, or a usage record -- a structural property of the
code's ordering, not a separately enforced check.
"""

import asyncio
import logging
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
from app.services.rate_limit import RateLimiter, RateLimitResult, RateLimitUnavailableError
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
logger = logging.getLogger("app.chat_completions")

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


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "rate_limiter_not_configured", "message": "rate limiting is not configured"},
        )
    return limiter


def get_rate_limit_max_requests(request: Request) -> int:
    return getattr(request.app.state, "rate_limit_max_requests", 60)


def get_rate_limit_window_seconds(request: Request) -> int:
    return getattr(request.app.state, "rate_limit_window_seconds", 60)


def _default_model_for_registry(registry: Mapping[str, ProviderRegistration]) -> str:
    registration = registry.get("openai")
    if registration is None or not registration.supported_models:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "no_provider_configured", "message": "no inference provider is configured"},
        )
    return next(iter(registration.supported_models))


def _set_rate_limit_headers(response: Response, result: RateLimitResult) -> None:
    response.headers["X-RateLimit-Limit"] = str(result.limit)
    response.headers["X-RateLimit-Remaining"] = str(result.remaining)
    response.headers["X-RateLimit-Reset"] = str(int(result.reset_at))


# --- Synchronous (Postgres) helpers, always invoked via asyncio.to_thread ---

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
    _resolve_authoritative_state_if_not_claimed for that.
    """
    fn = _settle_success_sync if success else _settle_failure_sync
    try:
        result = await asyncio.to_thread(fn, session_factory, idempotency_key_id, **kwargs)
    except Exception:
        logger.error(
            "settlement_unresolved",
            extra={"idempotency_key_id": str(idempotency_key_id), "attempted": "success" if success else "failure"},
        )
        raise _unresolved_settlement_error(idempotency_key_id) from None
    logger.info(
        "settlement_completed",
        extra={
            "idempotency_key_id": str(idempotency_key_id),
            "attempted": "success" if success else "failure",
            "claimed": result.claimed,
            "refunded_amount": str(result.refunded_amount) if result.refunded_amount is not None else None,
            "cost_capped": result.cost_capped,
        },
    )
    return result


async def _resolve_authoritative_state_if_not_claimed(
    session_factory: Callable[[], Session], idempotency_key_id: uuid.UUID, settle_result: SettlementResult
) -> "ChatCompletionResponseBody | None":
    """If settle_result.claimed is True, returns None (caller proceeds
    normally). If False, fetches and returns/raises the authoritative
    stored state instead of trusting any locally-computed narrative.
    """
    if settle_result.claimed:
        return None
    logger.info("settlement_lost_race", extra={"idempotency_key_id": str(idempotency_key_id)})
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
    request: Request,
    body: ChatCompletionRequest,
    response: Response,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    registry: Mapping[str, ProviderRegistration] = Depends(get_provider_registry),
    retry_policy: RetryPolicy = Depends(get_retry_policy),
    timeout_ms: int = Depends(get_timeout_ms),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    rate_limit_max_requests: int = Depends(get_rate_limit_max_requests),
    rate_limit_window_seconds: int = Depends(get_rate_limit_window_seconds),
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
    logger.info("authenticated", extra={"team_id": str(team_id)})

    # --- 3. Fingerprint from EXACTLY what the client sent. ---
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
    #        counting, rate limiting) is ever evaluated. ---
    existing = await asyncio.to_thread(
        _resolve_existing_row_sync, session_factory, team_id, idempotency_key, request_hash
    )
    if existing.kind == "response":
        logger.info("replay_completed", extra={"team_id": str(team_id)})
        response.headers["Idempotent-Replayed"] = "true"
        return existing.response
    if existing.kind == "error":
        logger.info(
            "replay_or_conflict_error",
            extra={"team_id": str(team_id), "status_code": existing.error.status_code},
        )
        raise existing.error
    # existing.kind == "none" -- genuinely new request. Continue below.

    # --- 5. Rate limiting (Redis, atomic sliding-window Lua script) --
    #        the cheapest "new request only" check, checked first among
    #        them, so a rejected or fail-closed request never reaches
    #        unknown-model validation, token counting, budget
    #        reservation, idempotency acquisition, or any provider call. ---
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    try:
        rate_result = await rate_limiter.check(
            team_id=str(team_id),
            window_seconds=rate_limit_window_seconds,
            limit=rate_limit_max_requests,
            member=request_id,
        )
    except RateLimitUnavailableError:
        # Fail CLOSED: Redis being unreachable must never be treated as
        # license to allow unlimited requests through -- see
        # app.services.rate_limit's module docstring.
        logger.error("rate_limiter_unavailable", extra={"team_id": str(team_id)})
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "rate_limiter_unavailable",
                "message": "rate limiting is temporarily unavailable; try again shortly",
            },
        ) from None

    if not rate_result.allowed:
        logger.warning(
            "rate_limited",
            extra={
                "team_id": str(team_id),
                "limit": rate_result.limit,
                "current_count": rate_result.current_count,
                "window_seconds": rate_limit_window_seconds,
            },
        )
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": "too many requests for this team; slow down and retry after the window resets",
            },
            headers={
                "Retry-After": str(rate_result.retry_after_seconds),
                "X-RateLimit-Limit": str(rate_result.limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(rate_result.reset_at)),
            },
        )
    _set_rate_limit_headers(response, rate_result)

    # --- 6. Requirements that apply ONLY to a genuinely new request,
    #        beyond rate limiting. ---
    model = body.model or _default_model_for_registry(registry)

    try:
        pricing = get_model_pricing("openai", model)
    except UnknownModelError:
        logger.info("unknown_model_rejected", extra={"team_id": str(team_id), "model": model})
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "unknown_model", "message": "requested model is not supported"},
        ) from None

    try:
        route = build_route([RouteCandidate("openai", model)])
        validate_route_against_registry(registry, route)
    except RoutingValidationError:
        logger.info("unknown_model_rejected", extra={"team_id": str(team_id), "model": model})
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "unknown_model", "message": "requested model is not supported by this deployment"},
        ) from None

    # --- 7. Authoritative token count, with its own explicit timeout
    #        and safe error handling. Cancellation is never caught. ---
    adapter = registry["openai"].adapter
    count_fn = getattr(adapter, "count_input_tokens", None)
    if callable(count_fn):
        try:
            async with asyncio.timeout(_COUNTING_TIMEOUT_MS / 1000):
                prompt_token_count = await count_fn(model, body.prompt)
        except TimeoutError:
            logger.warning("token_counting_timeout", extra={"team_id": str(team_id), "model": model})
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "provider_unavailable", "message": "token counting timed out; retry later"},
            ) from None
        except RetryableProviderError:
            logger.warning("token_counting_failed_retryable", extra={"team_id": str(team_id), "model": model})
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "provider_unavailable", "message": "token counting failed; retry later"},
            ) from None
        except NonRetryableProviderError:
            logger.warning("token_counting_failed_non_retryable", extra={"team_id": str(team_id), "model": model})
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
        logger.info("replay_completed_race", extra={"team_id": str(team_id)})
        response.headers["Idempotent-Replayed"] = "true"
        return txn1_result

    idempotency_key_id = txn1_result.idempotency_key_id
    logger.info(
        "reservation_acquired",
        extra={
            "team_id": str(team_id),
            "idempotency_key_id": str(idempotency_key_id),
            "model": model,
            "reserved_cost": str(reserved_cost),
        },
    )

    # --- Provider execution: no database session held during this call. ---
    try:
        route_result = await execute_route(registry, route, body.prompt, body.max_tokens, retry_policy, timeout_ms)
    except AllCandidatesFailedError as exc:
        last_category = exc.attempts[-1].failure_category if exc.attempts else None
        logger.warning(
            "provider_all_candidates_failed",
            extra={
                "team_id": str(team_id),
                "idempotency_key_id": str(idempotency_key_id),
                "attempt_count": len(exc.attempts),
                "last_failure_category": last_category.value if last_category else None,
            },
        )
        settle_result = await _settle_and_resolve(
            session_factory, idempotency_key_id, success=False,
            actual_cost=Decimal("0"), error_code="RETRIES_EXHAUSTED_COST_UNKNOWN",
            primary_provider="openai", primary_model=model, latency_ms=0,
        )
        replay = await _resolve_authoritative_state_if_not_claimed(session_factory, idempotency_key_id, settle_result)
        if replay is not None:
            response.headers["Idempotent-Replayed"] = "true"
            return replay
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
        logger.info(
            "request_cancelled",
            extra={"team_id": str(team_id), "idempotency_key_id": str(idempotency_key_id)},
        )
        raise
    except Exception:
        logger.exception(
            "provider_unexpected_exception",
            extra={"team_id": str(team_id), "idempotency_key_id": str(idempotency_key_id)},
        )
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

    logger.info(
        "provider_call_succeeded",
        extra={
            "team_id": str(team_id),
            "idempotency_key_id": str(idempotency_key_id),
            "final_provider": route_result.final_provider,
            "final_model": route_result.final_model,
            "fallback_used": route_result.fallback_used,
            "prompt_tokens": route_result.result.prompt_tokens,
            "completion_tokens": route_result.result.completion_tokens,
            "total_latency_ms": route_result.total_latency_ms,
        },
    )

    # --- Actual cost, computed FIRST from authoritative provider usage,
    #     before anything else that could fail -- so it is never lost. ---
    try:
        actual_cost = calculate_actual_cost(
            "openai", model, route_result.result.prompt_tokens, route_result.result.completion_tokens
        )
    except Exception:
        logger.exception(
            "cost_calculation_failed",
            extra={"team_id": str(team_id), "idempotency_key_id": str(idempotency_key_id)},
        )
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
        logger.exception(
            "response_construction_failed",
            extra={"team_id": str(team_id), "idempotency_key_id": str(idempotency_key_id)},
        )
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

    logger.info(
        "request_succeeded",
        extra={
            "team_id": str(team_id),
            "idempotency_key_id": str(idempotency_key_id),
            "actual_cost": str(actual_cost),
        },
    )
    return response_body
