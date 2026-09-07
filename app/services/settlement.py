"""
Atomic settlement: claims a pending idempotency record, adjusts the
team's budget, and records the final UsageRecord -- all in one
caller-owned transaction.

Transaction ownership: every function here takes a Session and never
commits or rolls it back. On success, the caller commits. On ANY failure
after the claim (a raised exception), the caller must roll back -- this
module never catches its own exceptions except to translate a failed
refund into SettlementIntegrityError, so an exception here always means
"roll back the whole transaction," restoring the idempotency record to
PENDING, the budget to its pre-settlement value, and leaving no
UsageRecord behind, since none of the writes below have committed.

Authoritative accounting data (SECURITY-RELEVANT): settle_success and
settle_failure accept ONLY idempotency_key_id and outcome data from the
caller -- never team_id, budget_period_start, or reserved_cost. After a
winning claim_settlement() call, this module re-selects the
IdempotencyKey row (same Session, same open transaction, with
execution_options(populate_existing=True) to guarantee a fresh read
rather than a possibly-stale identity-map hit) and derives team_id,
budget_period_start, and reserved_cost from THAT row. No additional
locking is required for this re-select: the winning claim's UPDATE
already holds the row lock, and a losing caller never reaches this code
path at all (it returns early with claimed=False). This eliminates any
possibility of a caller supplying a mismatched team, period, or inflated
reservation -- those values are never taken from the caller's arguments,
because the public functions do not accept them at all. The
actual_cost <= reserved_cost check therefore compares against the
DATABASE's own reserved_cost; if it fails, the raised
SettlementCostExceededError happens after the claim, by necessity (the
authoritative reserved_cost isn't known until after a successful claim),
and relies on the caller's rollback to restore PENDING.

Provider-call boundary: settlement must be called AFTER a provider
attempt (success or failure) has already completed -- never while a
provider call is in flight. This module makes no network calls and does
not import anything from app.providers or app.services.routing, keeping
it fully provider-independent -- required so the same settlement code
path settles both MockProvider-driven test runs and, starting in a later
step, real OpenAI-driven production runs without modification.

Fault-injection hooks (test-only, no-op in production): three
module-level no-op functions -- _before_refund_hook,
_before_usage_insert_hook, and _after_usage_flush_hook -- are called at
fixed points inside settle_success/settle_failure. In production they do
nothing. Tests patch them with unittest.mock.patch(..., side_effect=...)
to inject a fault at an exact point while still calling the REAL,
unmodified settle_success/settle_failure -- so rollback behavior is
verified against the actual service, never a hand-reimplemented copy of
it in the test script.

Retry-accounting note (Step 27 interaction): app.services.routing may
perform multiple attempts across multiple candidates for one logical
request. `actual_cost` passed into settle_success/settle_failure here is
expected to represent the TOTAL billable cost across every attempt that
was actually billed by a provider -- NOT just the cost of the final
attempt. This module does not compute that total itself; it is supplied
by the caller (a future orchestration layer, and eventually the real
OpenAI integration in Step 29). This module's only responsibility
regarding cost is to fail closed: actual_cost must never exceed the
authoritative reserved_cost. Whether a single worst-case reservation is
large enough to cover cumulative cost across retries is explicitly NOT
solved here -- matching the same gap flagged in app.services.routing's
module docstring.
"""

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IdempotencyKey, UsageRecord
from app.models.enums import IdempotencyStatus, UsageStatus
from app.services.budget import refund_budget
from app.services.idempotency import claim_settlement

_MAX_MONETARY_AMOUNT = Decimal("999999.999999")
_MAX_ERROR_CODE_LENGTH = 100
_MAX_PROVIDER_LENGTH = 64
_MAX_MODEL_LENGTH = 128


class SettlementValidationError(ValueError):
    """Raised when a settlement input fails validation before any SQL executes."""


class SettlementCostExceededError(SettlementValidationError):
    """Raised when actual_cost > the AUTHORITATIVE reserved_cost read from
    the claimed database row. Fail-closed: no reservation may be
    overspent. Raised after the claim (the authoritative value is only
    known then) -- the caller's rollback restores the idempotency record
    to PENDING.
    """


class SettlementIntegrityError(RuntimeError):
    """Raised when a refund that should have succeeded (a matching budget
    row was expected to exist) instead returned False from
    refund_budget -- an unexpected data-integrity condition, not a normal
    control-flow outcome. Propagates immediately so the caller rolls back
    the whole transaction.
    """


@dataclass(frozen=True)
class SettlementResult:
    claimed: bool
    refunded_amount: Decimal | None
    usage_record_id: uuid.UUID | None


def _validate_uuid(label: str, value) -> None:
    if not isinstance(value, uuid.UUID):
        raise SettlementValidationError(f"{label} must be a uuid.UUID, got {type(value).__name__}")


def _validate_cost(label: str, value, *, allow_zero: bool) -> None:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise SettlementValidationError(f"{label} must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise SettlementValidationError(f"{label} must be finite (not NaN or Infinity)")
    if allow_zero:
        if value < 0:
            raise SettlementValidationError(f"{label} must not be negative")
    else:
        if value <= 0:
            raise SettlementValidationError(f"{label} must be strictly greater than zero")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -6:
        raise SettlementValidationError(f"{label} must not have more than 6 decimal places")
    if value > _MAX_MONETARY_AMOUNT:
        raise SettlementValidationError(f"{label} exceeds NUMERIC(12,6) maximum of {_MAX_MONETARY_AMOUNT}")


def _validate_identifier(label: str, value, max_length: int) -> None:
    if not isinstance(value, str):
        raise SettlementValidationError(f"{label} must be a str, got {type(value).__name__}")
    if not value.strip():
        raise SettlementValidationError(f"{label} must not be blank")
    if len(value) > max_length:
        raise SettlementValidationError(f"{label} must not exceed {max_length} characters")


def _validate_nonneg_int(label: str, value) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettlementValidationError(f"{label} must be a plain int, got {type(value).__name__}")
    if value < 0:
        raise SettlementValidationError(f"{label} must not be negative")


def _validate_bool(label: str, value) -> None:
    if not isinstance(value, bool):
        raise SettlementValidationError(f"{label} must be a bool, got {type(value).__name__}")


def _validate_error_code(value) -> None:
    if not isinstance(value, str):
        raise SettlementValidationError(f"error_code must be a str, got {type(value).__name__}")
    if not value.strip():
        raise SettlementValidationError("error_code must not be blank")
    if len(value) > _MAX_ERROR_CODE_LENGTH:
        raise SettlementValidationError(f"error_code must not exceed {_MAX_ERROR_CODE_LENGTH} characters")


def _validate_response_snapshot(value) -> None:
    """Requires a dict whose contents are strictly JSON-compatible: a
    successful json.dumps(value, allow_nan=False) call, with no
    TypeError/ValueError. This rejects sets, NaN, Infinity, and any other
    non-JSON-serializable value nested anywhere inside the snapshot.
    Never includes the snapshot's own contents in the raised error
    message -- only the generic fact that validation failed.
    """
    if value is None:
        raise SettlementValidationError("response_snapshot is required for a successful settlement")
    if not isinstance(value, dict):
        raise SettlementValidationError(f"response_snapshot must be a dict, got {type(value).__name__}")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SettlementValidationError(
            "response_snapshot must be strictly JSON-compatible (no NaN, Infinity, sets, "
            "or other non-serializable values)"
        ) from exc


def _validate_final_pair(final_provider, final_model) -> None:
    """final_provider and final_model must either both be None or both be
    valid, non-blank, length-bounded strings -- never one present and the
    other missing.
    """
    if (final_provider is None) != (final_model is None):
        raise SettlementValidationError("final_provider and final_model must both be None or both be provided")
    if final_provider is not None:
        _validate_identifier("final_provider", final_provider, _MAX_PROVIDER_LENGTH)
        _validate_identifier("final_model", final_model, _MAX_MODEL_LENGTH)


def _load_authoritative_row(db: Session, idempotency_key_id: uuid.UUID) -> IdempotencyKey:
    """Re-select the IdempotencyKey row within the current transaction,
    immediately after a winning claim. See module docstring: this is a
    read of our own uncommitted write, requires no additional locking,
    and is the sole source of team_id/budget_period_start/reserved_cost
    for the rest of settlement.
    """
    return db.execute(
        select(IdempotencyKey)
        .where(IdempotencyKey.id == idempotency_key_id)
        .execution_options(populate_existing=True)
    ).scalar_one()


def _compute_refund_amount(reserved_cost: Decimal, actual_cost: Decimal) -> Decimal:
    if actual_cost > reserved_cost:
        raise SettlementCostExceededError(
            "actual_cost exceeds the authoritative reserved_cost -- refusing to settle "
            "(fail closed; caller must roll back to restore PENDING)"
        )
    return reserved_cost - actual_cost


def _apply_refund_if_needed(db: Session, row: IdempotencyKey, refund_amount: Decimal) -> Decimal | None:
    if refund_amount == 0:
        return None  # refund_budget rejects amount <= 0 -- skip the call entirely
    ok = refund_budget(db, row.team_id, row.budget_period_start, refund_amount)
    if not ok:
        raise SettlementIntegrityError(
            "refund_budget returned False for a refund that should have succeeded "
            "-- the expected team_budgets row was not found or updated"
        )
    return refund_amount


# --- Fault-injection hook points -------------------------------------
# All three are no-ops in production. Tests patch them via
# unittest.mock.patch(..., side_effect=SomeException(...)) to force a
# rollback at an exact stage while still exercising the REAL
# settle_success/settle_failure implementation end to end.

def _before_refund_hook(db: Session, row: IdempotencyKey) -> None:
    """Called immediately after the winning claim and authoritative row
    load, before the refund step runs."""
    return None


def _before_usage_insert_hook(db: Session, row: IdempotencyKey) -> None:
    """Called immediately after the refund step completes, before the
    UsageRecord is constructed and added."""
    return None


def _after_usage_flush_hook(db: Session, row: IdempotencyKey, record: UsageRecord) -> None:
    """Called immediately after the UsageRecord has been flushed
    (assigned a real id), before settle_success/settle_failure return
    control to the caller (who will then commit)."""
    return None


def settle_success(
    db: Session,
    *,
    idempotency_key_id: uuid.UUID,
    actual_cost: Decimal,
    response_snapshot: dict,
    primary_provider: str,
    primary_model: str,
    final_provider: str,
    final_model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    fallback_used: bool,
) -> SettlementResult:
    """Settle a successfully-completed request. Every input is validated
    before any SQL executes, EXCEPT the actual_cost <= reserved_cost
    check, which necessarily happens after the claim (see module
    docstring). If the claim is won: loads the authoritative
    IdempotencyKey row, refunds reserved_cost - actual_cost (skipping the
    refund call entirely if that difference is zero), inserts one
    successful UsageRecord, and returns claimed=True. If another caller
    already settled this record, or the ID does not exist, returns
    claimed=False with no other side effects. Never commits or rolls
    back; the caller owns the transaction.
    """
    _validate_uuid("idempotency_key_id", idempotency_key_id)
    _validate_cost("actual_cost", actual_cost, allow_zero=True)
    _validate_response_snapshot(response_snapshot)
    _validate_identifier("primary_provider", primary_provider, _MAX_PROVIDER_LENGTH)
    _validate_identifier("primary_model", primary_model, _MAX_MODEL_LENGTH)
    _validate_identifier("final_provider", final_provider, _MAX_PROVIDER_LENGTH)
    _validate_identifier("final_model", final_model, _MAX_MODEL_LENGTH)
    _validate_nonneg_int("prompt_tokens", prompt_tokens)
    _validate_nonneg_int("completion_tokens", completion_tokens)
    _validate_nonneg_int("latency_ms", latency_ms)
    _validate_bool("fallback_used", fallback_used)

    claimed = claim_settlement(
        db, idempotency_key_id, IdempotencyStatus.COMPLETED, response_snapshot=response_snapshot
    )
    if not claimed:
        return SettlementResult(claimed=False, refunded_amount=None, usage_record_id=None)

    row = _load_authoritative_row(db, idempotency_key_id)
    refund_amount = _compute_refund_amount(row.reserved_cost, actual_cost)

    _before_refund_hook(db, row)
    _apply_refund_if_needed(db, row, refund_amount)

    _before_usage_insert_hook(db, row)
    record = UsageRecord(
        idempotency_key_id=idempotency_key_id,
        primary_provider=primary_provider,
        primary_model=primary_model,
        final_provider=final_provider,
        final_model=final_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        actual_cost=actual_cost,
        latency_ms=latency_ms,
        status=UsageStatus.SUCCEEDED,
        fallback_used=fallback_used,
        error_code=None,
    )
    db.add(record)
    db.flush()
    _after_usage_flush_hook(db, row, record)

    return SettlementResult(claimed=True, refunded_amount=refund_amount, usage_record_id=record.id)


def settle_failure(
    db: Session,
    *,
    idempotency_key_id: uuid.UUID,
    actual_cost: Decimal,
    error_code: str,
    primary_provider: str,
    primary_model: str,
    final_provider: str | None = None,
    final_model: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: int,
    fallback_used: bool = False,
) -> SettlementResult:
    """Settle a request whose provider attempts ultimately failed.
    actual_cost is normally Decimal("0") (no billable usage occurred),
    but may be positive if a provider billed for a failed attempt --
    still validated against the AUTHORITATIVE reserved_cost read from the
    claimed row, never a caller-supplied value. final_provider and
    final_model must both be None or both be valid strings; a mismatched
    pair is rejected. Refunds reserved_cost - actual_cost (normally the
    full reservation), inserts one failed UsageRecord, and never sets
    IdempotencyKey.response_snapshot (claim_settlement stores SQL NULL
    for a FAILED settlement). Never commits or rolls back; the caller
    owns the transaction.
    """
    _validate_uuid("idempotency_key_id", idempotency_key_id)
    _validate_cost("actual_cost", actual_cost, allow_zero=True)
    _validate_error_code(error_code)
    _validate_identifier("primary_provider", primary_provider, _MAX_PROVIDER_LENGTH)
    _validate_identifier("primary_model", primary_model, _MAX_MODEL_LENGTH)
    _validate_final_pair(final_provider, final_model)
    _validate_nonneg_int("prompt_tokens", prompt_tokens)
    _validate_nonneg_int("completion_tokens", completion_tokens)
    _validate_nonneg_int("latency_ms", latency_ms)
    _validate_bool("fallback_used", fallback_used)

    claimed = claim_settlement(db, idempotency_key_id, IdempotencyStatus.FAILED, error_code=error_code)
    if not claimed:
        return SettlementResult(claimed=False, refunded_amount=None, usage_record_id=None)

    row = _load_authoritative_row(db, idempotency_key_id)
    refund_amount = _compute_refund_amount(row.reserved_cost, actual_cost)

    _before_refund_hook(db, row)
    _apply_refund_if_needed(db, row, refund_amount)

    _before_usage_insert_hook(db, row)
    record = UsageRecord(
        idempotency_key_id=idempotency_key_id,
        primary_provider=primary_provider,
        primary_model=primary_model,
        final_provider=final_provider,
        final_model=final_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        actual_cost=actual_cost,
        latency_ms=latency_ms,
        status=UsageStatus.FAILED,
        fallback_used=fallback_used,
        error_code=error_code,
    )
    db.add(record)
    db.flush()
    _after_usage_flush_hook(db, row, record)

    return SettlementResult(claimed=True, refunded_amount=refund_amount, usage_record_id=record.id)
