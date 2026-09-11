"""
Immutable, versioned prompt templates.

Immutability: once a row is created, its name/version/template/
content_fingerprint NEVER change -- enforced at TWO independent layers:
scripts/manage_prompt_versions.py (the only writer) has no "edit"
subcommand, AND the PostgreSQL triggers created in this table's own
Alembic migration (trg_prompt_versions_immutable_fields,
trg_prompt_versions_status_transition) reject any UPDATE that would
change those four fields or move `status` along any path other than
draft->approved or approved->retired, regardless of what issues the SQL.
The only mutations after creation are the two lifecycle timestamps
(approved_at/retired_at), driven by status transitions.

Content fingerprint: SHA-256 of the template text, stored so an
operator/auditor can verify a version's content integrity or detect
accidental duplicate submissions WITHOUT the template content itself
ever needing to be echoed back anywhere (matches this project's existing
audit-fingerprint pattern -- see idempotency_keys.request_hash, which
this mirrors exactly: 64 lowercase hex characters, checked by the same
kind of regex constraint).

Scope: prompt versions are a PLATFORM-level concept, not team-scoped --
there is no existing per-operator identity/RBAC system in this codebase
(only per-team API keys, which authenticate API CLIENTS, not platform
operators), so no team_id or "created_by" column exists here. See
scripts/manage_prompt_versions.py's own module docstring for why a CLI
script (same trust model as scripts/bootstrap_team.py) is the right
surface for creating/approving/retiring versions, rather than a new
authenticated HTTP management API.
"""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, Enum, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.usage_record import UsageRecord


class PromptVersionStatus(str, enum.Enum):
    """Defined HERE (not in app.models.enums) deliberately: this module
    does not know that file's exact current contents, and adding a new
    enum in its own small, self-contained module is strictly safer than
    guessing at and rewriting an existing shared file for a step that
    must not touch Steps 1-35.
    """
    DRAFT = "draft"
    APPROVED = "approved"
    RETIRED = "retired"


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_prompt_versions_name_version"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("length(trim(template)) > 0", name="template_not_blank"),
        CheckConstraint("length(content_fingerprint) = 64", name="content_fingerprint_length_64"),
        CheckConstraint("content_fingerprint ~ '^[0-9a-f]{64}$'", name="content_fingerprint_lowercase_hex"),
        # A draft has neither timestamp; an approved OR retired version
        # always has approved_at (you must pass through approved to
        # reach retired); only a retired version has retired_at.
        CheckConstraint("status <> 'draft' OR approved_at IS NULL", name="draft_has_no_approved_at"),
        CheckConstraint("status = 'draft' OR approved_at IS NOT NULL", name="non_draft_has_approved_at"),
        CheckConstraint("status = 'retired' OR retired_at IS NULL", name="non_retired_has_no_retired_at"),
        CheckConstraint("status <> 'retired' OR retired_at IS NOT NULL", name="retired_has_retired_at"),
        CheckConstraint("retired_at IS NULL OR retired_at >= approved_at", name="retired_at_after_approved_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer)
    # TEXT, not a bounded String: template content is arbitrary-length
    # prose, unlike the short identifiers elsewhere in this schema. It
    # is never returned by any API response or included in any log/
    # metric/span -- see scripts/manage_prompt_versions.py's own
    # docstring for the one place it IS ever displayed (a local
    # operator's own terminal, deliberately, for review before approval).
    template: Mapped[str] = mapped_column(Text)
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[PromptVersionStatus] = mapped_column(
        Enum(
            PromptVersionStatus,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum_class: [member.value for member in enum_class],
            name="prompt_version_status_values",
        ),
        default=PromptVersionStatus.DRAFT,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    usage_records: Mapped[list["UsageRecord"]] = relationship(
        "UsageRecord",
        back_populates="prompt_version",
    )
