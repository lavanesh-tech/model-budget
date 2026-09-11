import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import UsageStatus

if TYPE_CHECKING:
    from app.models.idempotency_key import IdempotencyKey
    from app.models.prompt_version import PromptVersion


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (
        CheckConstraint(
            "length(trim(primary_provider)) > 0",
            name="primary_provider_not_blank",
        ),
        CheckConstraint(
            "length(trim(primary_model)) > 0",
            name="primary_model_not_blank",
        ),
        CheckConstraint(
            "final_provider IS NULL OR length(trim(final_provider)) > 0",
            name="final_provider_not_blank",
        ),
        CheckConstraint(
            "final_model IS NULL OR length(trim(final_model)) > 0",
            name="final_model_not_blank",
        ),
        CheckConstraint(
            "error_code IS NULL OR length(trim(error_code)) > 0",
            name="error_code_not_blank",
        ),
        CheckConstraint(
            "prompt_tokens >= 0",
            name="prompt_tokens_non_negative",
        ),
        CheckConstraint(
            "completion_tokens >= 0",
            name="completion_tokens_non_negative",
        ),
        CheckConstraint(
            "actual_cost >= 0",
            name="actual_cost_non_negative",
        ),
        CheckConstraint(
            "latency_ms >= 0",
            name="latency_ms_non_negative",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR "
            "(final_provider IS NOT NULL AND final_model IS NOT NULL)",
            name="succeeded_has_final_provider_and_model",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR error_code IS NULL",
            name="succeeded_has_no_error_code",
        ),
        CheckConstraint(
            "status <> 'failed' OR error_code IS NOT NULL",
            name="failed_has_error_code",
        ),
        Index(
            "ix_usage_records_created_at",
            "created_at",
        ),
        # Step 36: supports "which requests used prompt version X" audit
        # queries. Nullable (a request with no prompt version selected
        # has no linkage) -- see app.models.prompt_version's own
        # docstring for the full immutable-versioning design.
        Index(
            "ix_usage_records_prompt_version_id",
            "prompt_version_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    idempotency_key_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("idempotency_keys.id", ondelete="RESTRICT"),
        unique=True,
    )
    primary_provider: Mapped[str] = mapped_column(String(64))
    primary_model: Mapped[str] = mapped_column(String(128))
    final_provider: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    final_model: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    prompt_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    actual_cost: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[UsageStatus] = mapped_column(
        Enum(
            UsageStatus,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum_class: [
                member.value for member in enum_class
            ],
            name="status_values",
        ),
    )
    fallback_used: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
    )
    error_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    # Step 36: which immutable prompt version (if any) produced the
    # effective prompt for this request. Nullable -- a request that did
    # not select a prompt version has no linkage, exactly preserving
    # pre-Step-36 rows and behavior. ON DELETE RESTRICT: a prompt
    # version that has ever been used can never be deleted (it can only
    # be retired -- see app.models.prompt_version), so this audit trail
    # can never be silently orphaned.
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prompt_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )

    idempotency_key: Mapped["IdempotencyKey"] = relationship(
        "IdempotencyKey",
        back_populates="usage_record",
    )
    prompt_version: Mapped["PromptVersion | None"] = relationship(
        "PromptVersion",
        back_populates="usage_records",
    )
