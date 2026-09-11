"""add prompt_versions and usage_records.prompt_version_id

Revision ID: 509346923a9f
Revises: 3238df7fb2d5
Create Date: 2026-09-11 00:00:00.000000

Step 36: immutable, versioned prompt templates. See
app.models.prompt_version's own module docstring for the full design.

Status column strategy MUST match app.models.prompt_version.PromptVersion
exactly (create_constraint=True on the same sa.Enum values/name, via the
same naming convention) -- otherwise `alembic check` reports drift
between the model's metadata and the live schema. An earlier draft of
this migration set create_constraint=False and added a SEPARATE, manually
-named CheckConstraint with equivalent logic -- that produces a
DIFFERENT (if logically equivalent) constraint than what the model's
metadata would generate, which is exactly the drift `alembic check` is
designed to catch. Corrected here: create_constraint=True in BOTH
places, letting SQLAlchemy generate the identical constraint from the
identical Enum definition.

Two triggers enforce, at the PostgreSQL level (not just through
scripts/manage_prompt_versions.py's own discipline), the two invariants
the CHECK constraints alone cannot express (a CHECK constraint only ever
sees the NEW row, never OLD vs NEW):

  1. prompt_versions_block_immutable_field_changes: rejects any UPDATE
     that would change name, version, template, or content_fingerprint
     -- the four fields that must never change after a row is created.
  2. prompt_versions_enforce_status_transition: rejects any status
     change that is not EXACTLY draft->approved or approved->retired --
     blocking draft->retired (skipping approval) and any backward move
     (approved->draft, retired->approved, retired->draft).

Both raise a plain PostgreSQL exception (caught by SQLAlchemy as
DBAPIError), which aborts the offending UPDATE's transaction -- the same
class of protection this project's existing CHECK constraints already
provide, just for a rule that needs to compare OLD and NEW.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "509346923a9f"
down_revision = "3238df7fb2d5"
branch_labels = None
depends_on = None


_IMMUTABLE_FIELDS_FUNCTION = """
CREATE FUNCTION prompt_versions_block_immutable_field_changes() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.name IS DISTINCT FROM OLD.name
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.template IS DISTINCT FROM OLD.template
       OR NEW.content_fingerprint IS DISTINCT FROM OLD.content_fingerprint THEN
        RAISE EXCEPTION 'prompt_versions.% is immutable and cannot be changed after creation',
            CASE
                WHEN NEW.name IS DISTINCT FROM OLD.name THEN 'name'
                WHEN NEW.version IS DISTINCT FROM OLD.version THEN 'version'
                WHEN NEW.template IS DISTINCT FROM OLD.template THEN 'template'
                ELSE 'content_fingerprint'
            END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_IMMUTABLE_FIELDS_TRIGGER = """
CREATE TRIGGER trg_prompt_versions_immutable_fields
BEFORE UPDATE ON prompt_versions
FOR EACH ROW
EXECUTE FUNCTION prompt_versions_block_immutable_field_changes();
"""

_STATUS_TRANSITION_FUNCTION = """
CREATE FUNCTION prompt_versions_enforce_status_transition() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (
            (OLD.status = 'draft' AND NEW.status = 'approved')
            OR (OLD.status = 'approved' AND NEW.status = 'retired')
        ) THEN
            RAISE EXCEPTION 'invalid prompt_versions status transition: % -> % (only draft->approved and approved->retired are allowed)',
                OLD.status, NEW.status;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_STATUS_TRANSITION_TRIGGER = """
CREATE TRIGGER trg_prompt_versions_status_transition
BEFORE UPDATE ON prompt_versions
FOR EACH ROW
EXECUTE FUNCTION prompt_versions_enforce_status_transition();
"""


def upgrade() -> None:
    op.create_table(
        "prompt_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft", "approved", "retired",
                name="prompt_version_status_values",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_prompt_versions"),
        sa.UniqueConstraint("name", "version", name="uq_prompt_versions_name_version"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_prompt_versions_name_not_blank"),
        sa.CheckConstraint("version > 0", name="ck_prompt_versions_version_positive"),
        sa.CheckConstraint("length(trim(template)) > 0", name="ck_prompt_versions_template_not_blank"),
        sa.CheckConstraint(
            "length(content_fingerprint) = 64", name="ck_prompt_versions_content_fingerprint_length_64"
        ),
        sa.CheckConstraint(
            "content_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_prompt_versions_content_fingerprint_lowercase_hex",
        ),
        sa.CheckConstraint("status <> 'draft' OR approved_at IS NULL", name="ck_prompt_versions_draft_has_no_approved_at"),
        sa.CheckConstraint(
            "status = 'draft' OR approved_at IS NOT NULL", name="ck_prompt_versions_non_draft_has_approved_at"
        ),
        sa.CheckConstraint(
            "status = 'retired' OR retired_at IS NULL", name="ck_prompt_versions_non_retired_has_no_retired_at"
        ),
        sa.CheckConstraint(
            "status <> 'retired' OR retired_at IS NOT NULL", name="ck_prompt_versions_retired_has_retired_at"
        ),
        sa.CheckConstraint(
            "retired_at IS NULL OR retired_at >= approved_at", name="ck_prompt_versions_retired_at_after_approved_at"
        ),
    )

    op.add_column(
        "usage_records",
        sa.Column("prompt_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_usage_records_prompt_version_id_prompt_versions",
        "usage_records",
        "prompt_versions",
        ["prompt_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_usage_records_prompt_version_id",
        "usage_records",
        ["prompt_version_id"],
    )

    op.execute(_IMMUTABLE_FIELDS_FUNCTION)
    op.execute(_IMMUTABLE_FIELDS_TRIGGER)
    op.execute(_STATUS_TRANSITION_FUNCTION)
    op.execute(_STATUS_TRANSITION_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_prompt_versions_status_transition ON prompt_versions")
    op.execute("DROP FUNCTION IF EXISTS prompt_versions_enforce_status_transition()")
    op.execute("DROP TRIGGER IF EXISTS trg_prompt_versions_immutable_fields ON prompt_versions")
    op.execute("DROP FUNCTION IF EXISTS prompt_versions_block_immutable_field_changes()")

    op.drop_index("ix_usage_records_prompt_version_id", table_name="usage_records")
    op.drop_constraint(
        "fk_usage_records_prompt_version_id_prompt_versions", "usage_records", type_="foreignkey"
    )
    op.drop_column("usage_records", "prompt_version_id")
    op.drop_table("prompt_versions")
