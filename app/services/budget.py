import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import TeamBudget

_MAX_MONETARY_AMOUNT = Decimal("999999.999999")
_MAX_DECIMAL_PLACES = 6


def _validate_monetary_amount(amount: Decimal) -> None:
    if not isinstance(amount, Decimal):
        raise ValueError(
            f"amount must be a Decimal, got {type(amount).__name__}"
        )

    if not amount.is_finite():
        raise ValueError("amount must be finite (not NaN or Infinity)")

    if amount <= 0:
        raise ValueError("amount must be strictly greater than zero")

    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -_MAX_DECIMAL_PLACES:
        raise ValueError(
            f"amount must not have more than "
            f"{_MAX_DECIMAL_PLACES} decimal places"
        )

    if amount > _MAX_MONETARY_AMOUNT:
        raise ValueError(
            f"amount exceeds NUMERIC(12,6) maximum of "
            f"{_MAX_MONETARY_AMOUNT}"
        )


def get_current_budget_period(
    db: Session,
    team_id: uuid.UUID,
    on_date: date,
) -> TeamBudget | None:
    return db.execute(
        select(TeamBudget).where(
            TeamBudget.team_id == team_id,
            TeamBudget.period_start <= on_date,
            TeamBudget.period_end > on_date,
        )
    ).scalar_one_or_none()


def reserve_budget(
    db: Session,
    team_id: uuid.UUID,
    period_start: date,
    amount: Decimal,
) -> bool:
    """Atomically reserve an amount from a team's budget.

    The caller owns the transaction and must commit or roll it back.
    """
    _validate_monetary_amount(amount)

    result = db.execute(
        update(TeamBudget)
        .where(
            TeamBudget.team_id == team_id,
            TeamBudget.period_start == period_start,
            TeamBudget.remaining_amount >= amount,
        )
        .values(
            remaining_amount=TeamBudget.remaining_amount - amount
        )
    )

    return result.rowcount == 1


def refund_budget(
    db: Session,
    team_id: uuid.UUID,
    period_start: date,
    amount: Decimal,
) -> bool:
    """Atomically return an amount to a team's budget.

    The refund is rejected if it would make the remaining amount exceed
    the allocated amount. The caller owns the transaction.
    """
    _validate_monetary_amount(amount)

    result = db.execute(
        update(TeamBudget)
        .where(
            TeamBudget.team_id == team_id,
            TeamBudget.period_start == period_start,
            TeamBudget.remaining_amount + amount
            <= TeamBudget.allocated_amount,
        )
        .values(
            remaining_amount=TeamBudget.remaining_amount + amount
        )
    )

    return result.rowcount == 1