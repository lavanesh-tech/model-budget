import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.idempotency_key import IdempotencyKey
    from app.models.team import Team


class TeamBudget(Base):
    __tablename__ = "team_budgets"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "period_start",
            name="uq_team_budgets_team_period_start",
        ),
        CheckConstraint(
            "allocated_amount >= 0",
            name="allocated_amount_non_negative",
        ),
        CheckConstraint(
            "remaining_amount >= 0",
            name="remaining_amount_non_negative",
        ),
        CheckConstraint(
            "remaining_amount <= allocated_amount",
            name="remaining_within_allocated",
        ),
        CheckConstraint(
            "period_end > period_start",
            name="period_end_after_start",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"),
    )
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    allocated_amount: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    remaining_amount: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    team: Mapped["Team"] = relationship(
        "Team",
        back_populates="budgets",
    )
    idempotency_keys: Mapped[list["IdempotencyKey"]] = relationship(
        "IdempotencyKey",
        back_populates="team_budget",
        passive_deletes=True,
    )