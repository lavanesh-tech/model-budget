"""
Atomic settlement: claims a pending idempotency record, adjusts the
team's budget, and records the final UsageRecord -- all in one
caller-owned transaction.

Transaction ownership: every function here takes a Session and never
commits or rolls it back. On success, the caller commits. On ANY failure
after the claim (a raised exception), the caller must roll back.

Authoritative accounting data: settle_success and settle_failure accept
ONLY idempotency_key_id and outcome data from the caller -- never
team_id, budget_period_start, or reserved_cost. After a winning
claim_settlement() call, this module re-selects the IdempotencyKey row
(same Session, same open transaction, execution_options(populate_existing
=True)) and derives team_id, budget_period_start, and reserved_cost from
THAT row.

--- Step 30 v3 correction: cost-overrun handling no longer discards
    known billed usage ---

The previous revision raised SettlementCostExceededError when
actual_cost > reserved_cost, forcing the caller to abandon the request
and record a full refund with actual_cost=0 -- discarding the one piece
of evidence (the true actual_cost) that we actually had. That was wrong:
it destroyed real accounting information to make an edge case easier to
handle.

Corrected policy, and it needed NO schema change (usage_records.
actual_cost has only an `actual_cost >= 0` CHECK -- nothing ties it to
reserved_cost, so a larger true value is perfectly representable):

  - settle_success/settle_failure now ALWAYS record the actual_cost the
    caller supplies -- the true, known cost -- in usage_records.actual_cost,
    even when it exceeds reserved_cost.
  - The TEAM is never overcharged: the refund is
    max(reserved_cost - actual_cost, Decimal("0")). When actual_cost >=
    reserved_cost, the refund is exactly zero -- the team is charged
    exactly the reservation, no more, no less.
  - The gap between the true actual_cost and what the team was charged
    is NOT separately flagged with its own column -- it does not need to
    be: reading idempotency_keys.reserved_cost alongside
    usage_records.actual_cost for the same request already tells you
    whether (and by how much) the true cost exceeded the reservation.
    `actual_cost > reserved_cost` on a joined row IS the audit signal.
  - This is a deliberate POLICY choice, stated plainly: when the true
    upstream cost exceeds what was reserved, the PLATFORM (not the team)
    absorbs the difference. The team's budget invariant (never charged
    more than it explicitly reserved) is preserved. This is a business
    policy decision, not a technical inevitability -- a different
    project could choose to bill the team the true cost and let a
    reservation be exceeded, but that would require loosening
    reserve_budget/refund_budget's own invariants, which this project
    has deliberately kept strict throughout every prior step.

SettlementCostExceededError is kept defined (for any external code that
may still reference the type) but is no longer raised by this module.

Fault-injection hooks (test-only, no-op in production): unchanged from
the prior revision -- _before_refund_hook, _before_usage_insert_hook,
_after_usage_flush_hook.

Retry-accounting note (unchanged): `actual_cost` passed in here is
expected to represent the TOTAL billable cost across every attempt that
was actually billed by a provider for a SUCCESSFUL settlement. For a
settle_failure call after every candidate/attempt was exhausted, this
module has no way to know what OpenAI may have billed for attempts that
never returned a usable result -- see app.api.chat_completions' own
docstring for how that uncertainty is represented to the client (never
as a confident "zero cost was confirmed").
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
    """No longer raised by this module -- see the module docstring for
    the corrected cost-overrun policy (cap the team's charge, preserve
    the true actual_cost). Kept defined only for backward compatibility
    with any external code that may still catch this type.
    """


class SettlementIntegrityError(RuntimeError):
    """Raised when a refund that should have succeeded (a matching budget
    row was expected to exist) instead returned False from
    refund_budget -- an unexpected data-integrity condition. Propagates
    immediately so the caller rolls back the whole transaction.
    """


@dataclass(frozen=True)
class SettlementResult:
    claimed: bool
    refunded_amount: Decimal | None
    usage_record_id: uuid.UUID | None
    cost_capped: bool = False
    """True if actual_cost exceeded reserved_cost and the team's charge
    was capped at the reservation (refunded_amount == 0 in that case).
    """


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
    if (final_provider is None) != (final_model is None):
        raise SettlementValidationError("final_provider and final_model must both be None or both be provided")
    if final_provider is not None:
        _validate_identifier("final_provider", final_provider, _MAX_PROVIDER_LENGTH)
        _validate_identifier("final_model", final_model, _MAX_MODEL_LENGTH)


def _load_authoritative_row(db: Session, idempotency_key_id: uuid.UUID) -> IdempotencyKey:
    return db.execute(
        select(IdempotencyKey)
        .where(IdempotencyKey.id == idempotency_key_id)
        .execution_options(populate_existing=True)
    ).scalar_one()


def _compute_refund_amount(reserved_cost: Decimal, actual_cost: Decimal) -> tuple[Decimal, bool]:
    """Returns (refund_amount, cost_capped). Never raises for an overrun
    -- see module docstring for the corrected policy.
    """
    if actual_cost >= reserved_cost:
        return Decimal("0"), actual_cost > reserved_cost
    return reserved_cost - actual_cost, False


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


def _before_refund_hook(db: Session, row: IdempotencyKey) -> None:
    return None


def _before_usage_insert_hook(db: Session, row: IdempotencyKey) -> None:
    return None


def _after_usage_flush_hook(db: Session, row: IdempotencyKey, record: UsageRecord) -> None:
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
    """Settle a successfully-completed request. If actual_cost exceeds
    the authoritative reserved_cost, the team's charge is capped at the
    reservation (refund=0) and the TRUE actual_cost is still recorded --
    see module docstring. Never commits or rolls back.
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
    refund_amount, cost_capped = _compute_refund_amount(row.reserved_cost, actual_cost)

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

    return SettlementResult(
        claimed=True, refunded_amount=refund_amount, usage_record_id=record.id, cost_capped=cost_capped
    )


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
    """Settle a request whose provider attempts ultimately failed. Same
    cost-capping policy as settle_success applies if actual_cost happens
    to be positive (e.g. a provider billed for a failed attempt) and
    exceeds reserved_cost. Never commits or rolls back.
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
    refund_amount, cost_capped = _compute_refund_amount(row.reserved_cost, actual_cost)

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

    return SettlementResult(
        claimed=True, refunded_amount=refund_amount, usage_record_id=record.id, cost_capped=cost_capped
    )
