import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import IdempotencyStatus

if TYPE_CHECKING:
    from app.models.team_budget import TeamBudget
    from app.models.usage_record import UsageRecord


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        ForeignKeyConstraint(
            ["team_id", "budget_period_start"],
            ["team_budgets.team_id", "team_budgets.period_start"],
            ondelete="RESTRICT",
            name="fk_idempotency_keys_team_budget",
        ),
        UniqueConstraint(
            "team_id",
            "idempotency_key",
            name="uq_idempotency_keys_team_key",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="idempotency_key_not_blank",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="request_hash_length_64",
        ),
        CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name="request_hash_lowercase_hex",
        ),
        CheckConstraint(
            "reserved_cost >= 0",
            name="reserved_cost_non_negative",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="expires_at_after_created_at",
        ),
        CheckConstraint(
            "status <> 'pending' OR completed_at IS NULL",
            name="pending_has_no_completed_at",
        ),
        CheckConstraint(
            "status = 'pending' OR completed_at IS NOT NULL",
            name="settled_has_completed_at",
        ),
        CheckConstraint(
            "status <> 'completed' OR response_snapshot IS NOT NULL",
            name="completed_has_response_snapshot",
        ),
        CheckConstraint(
            "status = 'completed' OR response_snapshot IS NULL",
            name="non_completed_has_no_response_snapshot",
        ),
        CheckConstraint(
            "status <> 'failed' OR error_code IS NOT NULL",
            name="failed_has_error_code",
        ),
        CheckConstraint(
            "status = 'failed' OR error_code IS NULL",
            name="non_failed_has_no_error_code",
        ),
        Index(
            "ix_idempotency_keys_status_expires_at",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    team_id: Mapped[uuid.UUID] = mapped_column()
    budget_period_start: Mapped[date] = mapped_column(Date)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[IdempotencyStatus] = mapped_column(
        Enum(
            IdempotencyStatus,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum_class: [
                member.value for member in enum_class
            ],
            name="status_values",
        ),
        default=IdempotencyStatus.PENDING,
        server_default=text("'pending'"),
    )
    reserved_cost: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    response_snapshot: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
    )

    team_budget: Mapped["TeamBudget"] = relationship(
        "TeamBudget",
        back_populates="idempotency_keys",
    )
    usage_record: Mapped["UsageRecord | None"] = relationship(
        "UsageRecord",
        back_populates="idempotency_key",
        uselist=False,
        passive_deletes=True,
    )