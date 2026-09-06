import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.team import Team


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint(
            "length(trim(name)) > 0",
            name="name_not_blank",
        ),
        CheckConstraint(
            "length(trim(public_key_id)) > 0",
            name="public_key_id_not_blank",
        ),
        CheckConstraint(
            "length(trim(key_prefix)) > 0",
            name="key_prefix_not_blank",
        ),
        CheckConstraint(
            "length(trim(secret_hash)) > 0",
            name="secret_hash_not_blank",
        ),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name="expires_at_after_created_at",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="revoked_at_not_before_created_at",
        ),
        Index("ix_api_keys_team_id", "team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"),
    )
    public_key_id: Mapped[str] = mapped_column(
        String(32),
        unique=True,
    )
    name: Mapped[str] = mapped_column(String(100))
    key_prefix: Mapped[str] = mapped_column(String(20))
    secret_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    team: Mapped["Team"] = relationship(
        "Team",
        back_populates="api_keys",
    )