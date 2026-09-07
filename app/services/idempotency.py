"""
Idempotency acquisition and safe-replay handling for inference requests.

Transaction ownership: every function here takes a Session and never
commits or rolls it back -- the caller owns the transaction boundary.

Reservation rule (READ THIS BEFORE WIRING THIS INTO A REQUEST HANDLER):
ONLY the AcquireOutcome.ACQUIRED outcome represents a genuinely new,
owned idempotency row. Only in that case may the caller proceed to call
app.services.budget.reserve_budget(...) in the SAME transaction, then
commit. Every other outcome must NOT trigger a reservation:

  - ALREADY_COMPLETED / ALREADY_FAILED: replay the stored result, no
    reservation.
  - IN_PROGRESS: another owner is actively working on this request right
    now (not expired); no reservation, caller should report "in
    progress" or retry later.
  - EXPIRED_PENDING: the row's original owner never settled it before
    its lease expired. This is intentionally a DEAD END in this step --
    see "Expired-pending recovery" below.

Expired-pending recovery (explicitly deferred): the current
idempotency_keys schema has no lease-owner token, no fencing/generation
counter, and no separate marker distinguishing "reservation was made but
never refunded" from "reservation was already refunded by some other
path." Given only expires_at and status, there is no safe way to
determine from this row alone whether it is safe to reserve budget again,
whether a provider call might still be in flight, or whether the original
reservation still needs to be refunded. Blindly reclaiming such a row (as
an earlier draft of this module did) risks double-reserving budget for
the same logical request. Recovering EXPIRED_PENDING rows correctly is
real, separate work for a later, dedicated recovery mechanism (e.g. a
lease-token/fencing-generation schema addition, or a reconciliation sweep
with its own atomicity story) and is NOT implemented here. For this step,
an EXPIRED_PENDING row is simply reported and left untouched.

Zero-cost note: idempotency_keys.reserved_cost has a `>= 0` CHECK
constraint (not `> 0`), so a reserved_cost of exactly Decimal("0") is
valid at the model/service level here -- a free (zero-token) request is a
legitimate case. app.services.budget.reserve_budget, however,
deliberately REQUIRES amount > 0. Callers integrating this module with
the budget service must therefore only call reserve_budget when the
reservation amount is > 0, and must skip the budget call entirely when it
is exactly 0 -- this module does not call budget.py, so it cannot enforce
that rule itself; it is documented here for whoever wires the two
together.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

from sqlalchemy import null, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import IdempotencyKey
from app.models.enums import IdempotencyStatus

_MAX_IDEMPOTENCY_KEY_LENGTH = 255
_MAX_ERROR_CODE_LENGTH = 100
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9_.\-]+")
_REQUEST_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_MAX_RESERVED_COST = Decimal("999999.999999")


class IdempotencyValidationError(ValueError):
    """Raised when an input fails validation before any SQL executes."""


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused with a different request payload."""


def _validate_team_id(value: uuid.UUID) -> None:
    if not isinstance(value, uuid.UUID):
        raise IdempotencyValidationError(f"team_id must be a uuid.UUID, got {type(value).__name__}")


def _validate_budget_period_start(value: date) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise IdempotencyValidationError(
            f"budget_period_start must be a date (not datetime), got {type(value).__name__}"
        )


def _validate_idempotency_key(value: str) -> None:
    if not isinstance(value, str):
        raise IdempotencyValidationError(f"idempotency_key must be a str, got {type(value).__name__}")
    if not value.strip():
        raise IdempotencyValidationError("idempotency_key must not be blank")
    if len(value) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise IdempotencyValidationError(
            f"idempotency_key must not exceed {_MAX_IDEMPOTENCY_KEY_LENGTH} characters"
        )
    if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        raise IdempotencyValidationError(
            "idempotency_key must contain only letters, digits, '.', '_', or '-'"
        )


def _validate_request_hash(value: str) -> None:
    if not isinstance(value, str):
        raise IdempotencyValidationError(f"request_hash must be a str, got {type(value).__name__}")
    if not _REQUEST_HASH_PATTERN.fullmatch(value):
        raise IdempotencyValidationError(
            "request_hash must be exactly 64 lowercase hexadecimal characters"
        )


def _validate_reserved_cost(value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise IdempotencyValidationError(f"reserved_cost must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise IdempotencyValidationError("reserved_cost must be finite (not NaN or Infinity)")
    if value < 0:
        raise IdempotencyValidationError("reserved_cost must not be negative")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -6:
        raise IdempotencyValidationError("reserved_cost must not have more than 6 decimal places")
    if value > _MAX_RESERVED_COST:
        raise IdempotencyValidationError(
            f"reserved_cost exceeds NUMERIC(12,6) maximum of {_MAX_RESERVED_COST}"
        )


def _validate_ttl(value: timedelta) -> None:
    if not isinstance(value, timedelta):
        raise IdempotencyValidationError(f"ttl must be a timedelta, got {type(value).__name__}")
    if value <= timedelta(0):
        raise IdempotencyValidationError("ttl must be strictly greater than zero")


def compute_request_fingerprint(payload: Mapping) -> str:
    """Deterministically hash a request payload into a 64-character
    lowercase hex digest, matching idempotency_keys.request_hash's own
    CHECK constraints exactly. Requires a Mapping (e.g. dict). Uses
    canonical JSON serialization (sorted keys, compact separators,
    allow_nan=False so NaN/Infinity floats are rejected rather than
    silently serialized as non-standard JSON) so the same logical payload
    always produces the same hash. Any serialization failure (an
    unsupported object, a set, NaN/Infinity, etc.) is caught and reraised
    as IdempotencyValidationError -- nothing is ever silently hashed.
    """
    if not isinstance(payload, Mapping):
        raise IdempotencyValidationError(f"payload must be a mapping, got {type(payload).__name__}")
    try:
        canonical = json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise IdempotencyValidationError(f"payload could not be canonically serialized: {exc}") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AcquireOutcome(Enum):
    ACQUIRED = "acquired"  # brand-new row created; caller owns it and may reserve budget
    ALREADY_COMPLETED = "already_completed"
    ALREADY_FAILED = "already_failed"
    IN_PROGRESS = "in_progress"  # pending, not expired, owned by someone else
    EXPIRED_PENDING = "expired_pending"  # pending but lease expired; recovery deferred, no ownership granted


@dataclass(frozen=True)
class AcquireResult:
    outcome: AcquireOutcome
    record: IdempotencyKey


def acquire_idempotency_slot(
    db: Session,
    team_id: uuid.UUID,
    budget_period_start: date,
    idempotency_key: str,
    request_hash: str,
    reserved_cost: Decimal,
    ttl: timedelta,
) -> AcquireResult:
    """Atomically acquire ownership of an idempotency key, or report the
    status of an existing one. Never commits or rolls back -- the caller
    owns the transaction. Raises IdempotencyConflictError if the key
    already exists with a different request_hash. See module docstring:
    ONLY the ACQUIRED outcome may proceed to reserve_budget().
    """
    _validate_team_id(team_id)
    _validate_budget_period_start(budget_period_start)
    _validate_idempotency_key(idempotency_key)
    _validate_request_hash(request_hash)
    _validate_reserved_cost(reserved_cost)
    _validate_ttl(ttl)

    now = datetime.now(timezone.utc)
    expires_at = now + ttl

    insert_stmt = (
        pg_insert(IdempotencyKey)
        .values(
            id=uuid.uuid4(),
            team_id=team_id,
            budget_period_start=budget_period_start,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status=IdempotencyStatus.PENDING,
            reserved_cost=reserved_cost,
            created_at=now,
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(index_elements=["team_id", "idempotency_key"])
        .returning(IdempotencyKey.id)
    )
    inserted_id = db.execute(insert_stmt).scalar_one_or_none()

    if inserted_id is not None:
        record = db.get(IdempotencyKey, inserted_id)
        return AcquireResult(outcome=AcquireOutcome.ACQUIRED, record=record)

    existing = db.execute(
        select(IdempotencyKey)
        .where(
            IdempotencyKey.team_id == team_id,
            IdempotencyKey.idempotency_key == idempotency_key,
        )
        .execution_options(populate_existing=True)
    ).scalar_one()

    if existing.request_hash != request_hash:
        raise IdempotencyConflictError("idempotency key reused with a different request payload")

    if existing.status == IdempotencyStatus.COMPLETED:
        return AcquireResult(outcome=AcquireOutcome.ALREADY_COMPLETED, record=existing)
    if existing.status == IdempotencyStatus.FAILED:
        return AcquireResult(outcome=AcquireOutcome.ALREADY_FAILED, record=existing)

    # status is PENDING at this point -- report expired vs. in-progress,
    # but take NO action either way (no UPDATE, no ownership granted).
    if existing.expires_at <= now:
        return AcquireResult(outcome=AcquireOutcome.EXPIRED_PENDING, record=existing)

    return AcquireResult(outcome=AcquireOutcome.IN_PROGRESS, record=existing)


def claim_settlement(
    db: Session,
    idempotency_key_id: uuid.UUID,
    status: IdempotencyStatus,
    response_snapshot: dict | None = None,
    error_code: str | None = None,
) -> bool:
    """Atomically claim settlement of a pending idempotency row via a
    single conditional UPDATE ... WHERE id = :id AND status = 'pending'.
    Returns True if this call won the claim (rowcount == 1), False if the
    row was already settled by another caller (rowcount == 0) -- in which
    case nothing is changed. Never commits or rolls back. Composable with
    a future refund_budget() call and a usage_records insert in the same
    settlement transaction -- this function only touches the
    idempotency_keys row itself.
    """
    if not isinstance(idempotency_key_id, uuid.UUID):
        raise IdempotencyValidationError(
            f"idempotency_key_id must be a uuid.UUID, got {type(idempotency_key_id).__name__}"
        )
    if not isinstance(status, IdempotencyStatus):
        raise IdempotencyValidationError(
            f"status must be an IdempotencyStatus, got {type(status).__name__}"
        )
    if status == IdempotencyStatus.PENDING:
        raise IdempotencyValidationError("status must be COMPLETED or FAILED to settle, not PENDING")

    if status == IdempotencyStatus.COMPLETED:
        if response_snapshot is None:
            raise IdempotencyValidationError("response_snapshot is required when settling as COMPLETED")
        if error_code is not None:
            raise IdempotencyValidationError("error_code must be None when settling as COMPLETED")
    else:  # FAILED
        if response_snapshot is not None:
            raise IdempotencyValidationError("response_snapshot must be None when settling as FAILED")
        if not isinstance(error_code, str):
            raise IdempotencyValidationError(
                f"error_code must be a str when settling as FAILED, got {type(error_code).__name__}"
            )
        if not error_code.strip():
            raise IdempotencyValidationError("error_code must not be blank when settling as FAILED")
        if len(error_code) > _MAX_ERROR_CODE_LENGTH:
            raise IdempotencyValidationError(
                f"error_code must not exceed {_MAX_ERROR_CODE_LENGTH} characters"
            )

    now = datetime.now(timezone.utc)
    stmt = (
        update(IdempotencyKey)
        .where(
            IdempotencyKey.id == idempotency_key_id,
            IdempotencyKey.status == IdempotencyStatus.PENDING,
        )
        .values(
            status=status,
            completed_at=now,
            response_snapshot=(
                response_snapshot
                if response_snapshot is not None
                else null()
            ),
            error_code=error_code,
        )
    )
    result = db.execute(stmt)
    return result.rowcount == 1
