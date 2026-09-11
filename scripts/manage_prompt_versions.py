"""
CLI for creating/approving/retiring immutable prompt versions.

Why a script, not a new authenticated HTTP management API (Step 36's
requirement explicitly asks for this decision to be justified): this
project has exactly ONE identity axis -- per-team API keys, which
authenticate API CLIENTS calling /v1/chat/completions. There is no
existing operator/admin identity, session, or RBAC system anywhere in
this codebase to hang a "who is allowed to approve a prompt version"
check on. Building one correctly (and SAFELY -- an under-designed admin
auth surface is a worse outcome than no HTTP surface at all) is real,
separate work outside Step 36's scope. This script uses the SAME trust
model scripts/bootstrap_team.py already established: whoever has direct
database credentials on this host is the operator -- no new trust
boundary is introduced, and none is silently assumed either.

Immutability, enforced by this script's own design: there is no "edit"
subcommand. Once `create` inserts a row, its name/version/template/
content_fingerprint never change again -- only `approve`/`retire`
transition `status` (and set the corresponding timestamp). See
app.models.prompt_version's own module docstring for the full lifecycle
rule (draft -> approved -> retired, enforced by CHECK constraints at the
database level too, not just here).

Template content is NEVER printed by `approve`/`retire`/`list` -- only
by `create` (echoing back what you are ABOUT to submit, for local
operator review before confirming) and `show` (an explicit, deliberate
"I want to see this one template" operation). Every other subcommand
prints only metadata: id, name, version, status, timestamps, and the
content fingerprint -- never the template text itself.
"""

import argparse
import hashlib
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import PromptVersion, PromptVersionStatus


def _fingerprint(template: str) -> str:
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def _next_version(session, name: str) -> int:
    current_max = session.execute(
        select(PromptVersion.version).where(PromptVersion.name == name).order_by(PromptVersion.version.desc())
    ).scalars().first()
    return (current_max or 0) + 1


def cmd_create(args) -> int:
    template = args.template_file.read()
    if not template.strip():
        print("error: template file is blank", file=sys.stderr)
        return 1
    if len(template) > 20_000:
        print("error: template exceeds 20,000 characters (the same ceiling requests are checked against)",
              file=sys.stderr)
        return 1

    session = SessionLocal()
    try:
        version_number = _next_version(session, args.name)
        row = PromptVersion(
            id=uuid.uuid4(),
            name=args.name,
            version=version_number,
            template=template,
            content_fingerprint=_fingerprint(template),
            status=PromptVersionStatus.DRAFT,
        )
        session.add(row)
        session.commit()
        print(f"created: id={row.id} name={row.name!r} version={row.version} status={row.status.value}")
        print(f"content_fingerprint={row.content_fingerprint}")
        print("--- template content (for your review before approving) ---")
        print(template)
        print("--- end template content ---")
        print(f"\nTo approve: python -m scripts.manage_prompt_versions approve --id {row.id}")
        return 0
    except Exception as exc:
        session.rollback()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


def _print_row(row: PromptVersion) -> None:
    print(
        f"id={row.id} name={row.name!r} version={row.version} status={row.status.value} "
        f"content_fingerprint={row.content_fingerprint} created_at={row.created_at} "
        f"approved_at={row.approved_at} retired_at={row.retired_at}"
    )


def cmd_approve(args) -> int:
    session = SessionLocal()
    try:
        row = session.get(PromptVersion, args.id)
        if row is None:
            print(f"error: no prompt version with id={args.id}", file=sys.stderr)
            return 1
        if row.status != PromptVersionStatus.DRAFT:
            print(f"error: only a DRAFT version can be approved (current status: {row.status.value})",
                  file=sys.stderr)
            return 1
        row.status = PromptVersionStatus.APPROVED
        row.approved_at = datetime.now(timezone.utc)
        session.commit()
        print("approved:")
        _print_row(row)
        return 0
    except Exception as exc:
        session.rollback()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


def cmd_retire(args) -> int:
    session = SessionLocal()
    try:
        row = session.get(PromptVersion, args.id)
        if row is None:
            print(f"error: no prompt version with id={args.id}", file=sys.stderr)
            return 1
        if row.status != PromptVersionStatus.APPROVED:
            print(f"error: only an APPROVED version can be retired (current status: {row.status.value})",
                  file=sys.stderr)
            return 1
        row.status = PromptVersionStatus.RETIRED
        row.retired_at = datetime.now(timezone.utc)
        session.commit()
        print("retired:")
        _print_row(row)
        print("note: existing replays of requests that already used this version remain unaffected -- "
              "replay never re-checks a prompt version's current status.")
        return 0
    except Exception as exc:
        session.rollback()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


def cmd_list(args) -> int:
    session = SessionLocal()
    try:
        stmt = select(PromptVersion).order_by(PromptVersion.name, PromptVersion.version)
        if args.name:
            stmt = stmt.where(PromptVersion.name == args.name)
        rows = session.execute(stmt).scalars().all()
        if not rows:
            print("(no prompt versions found)")
            return 0
        for row in rows:
            _print_row(row)
        return 0
    finally:
        session.close()


def cmd_show(args) -> int:
    """The one deliberate exception: prints a SINGLE version's template
    content, for an operator who explicitly asked to see it.
    """
    session = SessionLocal()
    try:
        row = session.get(PromptVersion, args.id)
        if row is None:
            print(f"error: no prompt version with id={args.id}", file=sys.stderr)
            return 1
        _print_row(row)
        print("--- template content ---")
        print(row.template)
        print("--- end template content ---")
        return 0
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage immutable prompt versions (see this module's own docstring).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Create a new DRAFT prompt version from a template file.")
    p_create.add_argument("--name", required=True, help="Logical prompt name/family (e.g. 'support_greeting').")
    p_create.add_argument(
        "--template-file", required=True, type=argparse.FileType("r", encoding="utf-8"), dest="template_file",
        help="Path to a UTF-8 text file containing the template. Reference the client-supplied "
             "prompt text as $input.",
    )
    p_create.set_defaults(func=cmd_create)

    p_approve = sub.add_parser("approve", help="Approve a DRAFT version, making it selectable by clients.")
    p_approve.add_argument("--id", required=True, type=uuid.UUID)
    p_approve.set_defaults(func=cmd_approve)

    p_retire = sub.add_parser("retire", help="Retire an APPROVED version -- no longer selectable for NEW requests.")
    p_retire.add_argument("--id", required=True, type=uuid.UUID)
    p_retire.set_defaults(func=cmd_retire)

    p_list = sub.add_parser("list", help="List prompt versions (metadata only -- never template content).")
    p_list.add_argument("--name", required=False, help="Filter to one logical prompt name.")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show ONE version's full metadata AND template content.")
    p_show.add_argument("--id", required=True, type=uuid.UUID)
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
