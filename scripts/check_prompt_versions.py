"""
Offline verification for Step 36: prompt versioning.

Fake providers only -- no OPENAI_API_KEY, no OpenAI network call, no
billing. Uses a real PostgreSQL (via docker compose, same as every other
check_*.py script in this project) for the prompt_versions table and its
enforcement triggers.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.api.chat_completions import (
    get_provider_registry,
    get_rate_limit_max_requests,
    get_rate_limit_window_seconds,
    get_rate_limiter,
    get_retry_policy,
    get_session_factory,
    get_timeout_ms,
)
from app.db import SessionLocal, engine
from app.main import app
from app.models import ApiKey, IdempotencyKey, PromptVersion, PromptVersionStatus, Team, TeamBudget, UsageRecord
from app.providers.base import CompletionRequest, CompletionResult
from app.security.api_keys import generate_api_key
from app.models.enums import IdempotencyStatus
from app.services.idempotency import compute_request_fingerprint
from app.services.rate_limit import RateLimitResult
from app.services.routing import ProviderRegistration, RetryPolicy, build_provider_registry

MODEL = "gpt-5-mini"
PERIOD_START = datetime.now(timezone.utc).date()


class FakeRateLimiter:
    async def check(self, *, team_id, window_seconds, limit, member):
        return RateLimitResult(
            allowed=True, limit=limit, current_count=1, window_seconds=window_seconds,
            reset_at=0.0, retry_after_seconds=0,
        )


@dataclass
class RecordingProvider:
    """Records the EXACT prompt text it was called with, for both
    count_input_tokens and complete -- lets tests assert the rendered
    (or, when no version was selected, raw) text is what actually
    reached the provider, without any real network/billing.
    """
    name: str = "openai"
    completion_text: str = "ok"
    prompt_tokens: int = 3
    completion_tokens: int = 2
    seen_prompts: list = field(default_factory=list)

    async def count_input_tokens(self, model, prompt):
        self.seen_prompts.append(prompt)
        return self.prompt_tokens

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.seen_prompts.append(request.prompt)
        return CompletionResult(
            provider="openai", model=MODEL, completion_text=self.completion_text,
            prompt_tokens=self.prompt_tokens, completion_tokens=self.completion_tokens, latency_ms=0,
        )


def _bootstrap(suffix: str, budget=Decimal("1000.000000")):
    db = SessionLocal()
    try:
        team = Team(name=f"promptver-check-team-{suffix}-{uuid.uuid4()}")
        db.add(team)
        db.flush()
        tb = TeamBudget(
            team_id=team.id, period_start=PERIOD_START, period_end=PERIOD_START + timedelta(days=30),
            allocated_amount=budget, remaining_amount=budget,
        )
        db.add(tb)
        generated = generate_api_key()
        api_key = ApiKey(
            team_id=team.id, public_key_id=generated.public_key_id, name="promptver-check-key",
            key_prefix=generated.key_prefix, secret_hash=generated.secret_hash,
        )
        db.add(api_key)
        db.commit()
        return team.id, generated.plaintext.get_secret_value()
    finally:
        db.close()


def _create_prompt_version(name: str, template: str, *, approve: bool = False) -> uuid.UUID:
    db = SessionLocal()
    try:
        existing_max = db.execute(
            select(PromptVersion.version).where(PromptVersion.name == name).order_by(PromptVersion.version.desc())
        ).scalars().first()
        row = PromptVersion(
            id=uuid.uuid4(),
            name=name,
            version=(existing_max or 0) + 1,
            template=template,
            content_fingerprint=hashlib.sha256(template.encode("utf-8")).hexdigest(),
            status=PromptVersionStatus.DRAFT,
        )
        db.add(row)
        db.flush()
        if approve:
            row.status = PromptVersionStatus.APPROVED
            row.approved_at = datetime.now(timezone.utc)
        db.commit()
        return row.id
    finally:
        db.close()


def _retire_prompt_version(prompt_version_id: uuid.UUID) -> None:
    db = SessionLocal()
    try:
        row = db.get(PromptVersion, prompt_version_id)
        row.status = PromptVersionStatus.RETIRED
        row.retired_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def _override(provider):
    app.dependency_overrides[get_provider_registry] = lambda: build_provider_registry(
        (ProviderRegistration("openai", provider, frozenset({MODEL})),)
    )
    app.dependency_overrides[get_retry_policy] = lambda: RetryPolicy(
        max_attempts_per_candidate=1, base_backoff_ms=1, max_backoff_ms=10
    )
    app.dependency_overrides[get_timeout_ms] = lambda: 1000
    app.dependency_overrides[get_session_factory] = lambda: SessionLocal
    app.dependency_overrides[get_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[get_rate_limit_max_requests] = lambda: 60
    app.dependency_overrides[get_rate_limit_window_seconds] = lambda: 60


def _headers(key: str, idem: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Idempotency-Key": idem}


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list = []

    def emit(self, record):
        self.lines.append(self.format(record))


class _FakeSpan:
    """Minimal stand-in for a real OTel span -- just enough surface
    (set_attribute) for a direct, isolated unit test of
    app.tracing.record_safe_attributes's allowlist, with no dependency
    on a real TracerProvider/exporter pipeline.
    """
    def __init__(self):
        self.attributes: dict = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value


def _check_tracing_allowlist() -> None:
    from app.tracing import record_safe_attributes

    span = _FakeSpan()
    record_safe_attributes(span, **{"db.operation": "db.prompt_version_lookup"})
    assert span.attributes.get("db.operation") == "db.prompt_version_lookup", (
        "db.operation='db.prompt_version_lookup' must be in tracing's allowlist -- it was silently dropped"
    )
    # Confirm the allowlist is still genuinely an allowlist, not wide open:
    # an arbitrary/unbounded value under the SAME key must still be dropped.
    span2 = _FakeSpan()
    record_safe_attributes(span2, **{"db.operation": "not-a-real-operation-" + uuid.uuid4().hex})
    assert "db.operation" not in span2.attributes, (
        "an arbitrary db.operation value must still be dropped -- the allowlist must stay bounded"
    )
    print("0. tracing allowlist: db.operation='db.prompt_version_lookup' is now recorded (not dropped); "
          "an arbitrary/unbounded db.operation value is still correctly dropped")


def _check_database_level_enforcement() -> uuid.UUID:
    """Attempts invalid mutations via RAW SQL (bypassing every Python-
    level guard entirely) to prove enforcement lives in PostgreSQL
    itself, not merely in scripts/manage_prompt_versions.py's own
    discipline.
    """
    db = SessionLocal()
    created_id = None
    try:
        vid = _create_prompt_version("immutable_test", "original content", approve=False)
        created_id = vid

        # --- immutable fields: each attempted mutation must be rejected ---
        for column, new_value in [
            ("name", "'changed-name'"),
            ("version", "999"),
            ("template", "'changed template content'"),
            ("content_fingerprint", "'" + ("a" * 64) + "'"),
        ]:
            try:
                db.execute(text(f"UPDATE prompt_versions SET {column} = {new_value} WHERE id = :id"), {"id": vid})
                db.commit()
                raise AssertionError(f"expected the immutability trigger to block changing {column}")
            except DBAPIError:
                db.rollback()
        print("1. database-level immutability: raw SQL UPDATEs to name, version, template, and "
              "content_fingerprint are ALL rejected by the trigger, independent of any application code")

        # --- invalid transitions, each constructed to satisfy every OTHER
        #     CHECK constraint so ONLY the transition trigger can be what
        #     blocks it (isolating what is actually being tested) ---
        try:
            db.execute(
                text(
                    "UPDATE prompt_versions SET status = 'retired', "
                    "approved_at = now(), retired_at = now() WHERE id = :id"
                ),
                {"id": vid},
            )
            db.commit()
            raise AssertionError("expected the transition trigger to block draft->retired")
        except DBAPIError:
            db.rollback()

        # valid: draft -> approved
        db.execute(
            text("UPDATE prompt_versions SET status = 'approved', approved_at = now() WHERE id = :id"), {"id": vid}
        )
        db.commit()

        try:
            db.execute(
                text("UPDATE prompt_versions SET status = 'draft', approved_at = NULL WHERE id = :id"), {"id": vid}
            )
            db.commit()
            raise AssertionError("expected the transition trigger to block approved->draft")
        except DBAPIError:
            db.rollback()

        # valid: approved -> retired
        db.execute(
            text("UPDATE prompt_versions SET status = 'retired', retired_at = now() WHERE id = :id"), {"id": vid}
        )
        db.commit()

        try:
            db.execute(
                text("UPDATE prompt_versions SET status = 'approved', retired_at = NULL WHERE id = :id"), {"id": vid}
            )
            db.commit()
            raise AssertionError("expected the transition trigger to block retired->approved")
        except DBAPIError:
            db.rollback()

        try:
            db.execute(
                text(
                    "UPDATE prompt_versions SET status = 'draft', approved_at = NULL, "
                    "retired_at = NULL WHERE id = :id"
                ),
                {"id": vid},
            )
            db.commit()
            raise AssertionError("expected the transition trigger to block retired->draft")
        except DBAPIError:
            db.rollback()

        print("2. database-level lifecycle: draft->approved and approved->retired succeed via raw SQL; "
              "draft->retired, approved->draft, retired->approved, and retired->draft are ALL rejected")
        return vid
    finally:
        db.close()


def _check_pre_step36_fingerprint_compatibility() -> list:
    """Regression proof for the compatibility bug found in real
    production verification: a pre-Step-36 idempotency row was
    fingerprinted under the OLD three-key shape
    ({"model", "prompt", "max_tokens"}) -- BEFORE the
    "prompt_version_id" key ever existed in this codebase. This test
    constructs rows using that EXACT old shape (via the real,
    unmodified compute_request_fingerprint -- never a hand-rolled
    hash), simulating rows that already existed in a database before
    this deployment, then sends a matching no-prompt_version_id request
    and asserts PENDING/COMPLETED/FAILED are each still handled exactly
    as they were before Step 36 ever existed. Returns the created team
    ids for cleanup.
    """
    team_ids: list = []

    # --- PENDING: must still 409 request_in_progress ---
    team_pending, key_pending = _bootstrap("legacy-pending")
    team_ids.append(team_pending)
    old_shape_payload = {"model": None, "prompt": "legacy pending prompt", "max_tokens": 10}
    old_hash = compute_request_fingerprint(old_shape_payload)
    idem_pending = "idem-legacy-pending-" + uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        db.add(IdempotencyKey(
            id=uuid.uuid4(), team_id=team_pending, budget_period_start=PERIOD_START,
            idempotency_key=idem_pending, request_hash=old_hash,
            status=IdempotencyStatus.PENDING, reserved_cost=Decimal("0.010000"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ))
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        _override(RecordingProvider())
        r = client.post(
            "/v1/chat/completions", headers=_headers(key_pending, idem_pending),
            json={"prompt": "legacy pending prompt", "max_tokens": 10},
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "request_in_progress", r.json()

    # --- COMPLETED: must still replay successfully, from a snapshot
    #     that (like every real pre-Step-36 snapshot) has NO
    #     prompt_version_id key at all. ---
    team_completed, key_completed = _bootstrap("legacy-completed")
    team_ids.append(team_completed)
    old_shape_payload_2 = {"model": None, "prompt": "legacy completed prompt", "max_tokens": 10}
    old_hash_2 = compute_request_fingerprint(old_shape_payload_2)
    idem_completed = "idem-legacy-completed-" + uuid.uuid4().hex[:8]
    legacy_snapshot = {
        "id": str(uuid.uuid4()), "model": MODEL, "provider": "openai", "completion": "legacy completion",
        "usage": {"prompt_tokens": 3, "completion_tokens": 2}, "actual_cost": "0.000010",
        "fallback_used": False, "status": "succeeded",
        # deliberately NO "prompt_version_id" key -- exactly what a real
        # pre-Step-36 stored snapshot looks like.
    }
    db = SessionLocal()
    try:
        db.add(IdempotencyKey(
            id=uuid.uuid4(), team_id=team_completed, budget_period_start=PERIOD_START,
            idempotency_key=idem_completed, request_hash=old_hash_2,
            status=IdempotencyStatus.COMPLETED, reserved_cost=Decimal("0.000010"),
            response_snapshot=legacy_snapshot, completed_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ))
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        _override(RecordingProvider())
        r = client.post(
            "/v1/chat/completions", headers=_headers(key_completed, idem_completed),
            json={"prompt": "legacy completed prompt", "max_tokens": 10},
        )
        assert r.status_code == 200, r.text
        assert r.headers.get("Idempotent-Replayed") == "true"
        assert r.json()["completion"] == "legacy completion"
        assert r.json()["prompt_version_id"] is None, (
            "a legacy snapshot with no prompt_version_id key must deserialize with the field defaulting "
            "to None, not fail or fabricate a value"
        )

    # --- FAILED: must still return the existing safe failed-replay
    #     response (502, provider_error, echoing the stored error_code). ---
    team_failed, key_failed = _bootstrap("legacy-failed")
    team_ids.append(team_failed)
    old_shape_payload_3 = {"model": None, "prompt": "legacy failed prompt", "max_tokens": 10}
    old_hash_3 = compute_request_fingerprint(old_shape_payload_3)
    idem_failed = "idem-legacy-failed-" + uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        db.add(IdempotencyKey(
            id=uuid.uuid4(), team_id=team_failed, budget_period_start=PERIOD_START,
            idempotency_key=idem_failed, request_hash=old_hash_3,
            status=IdempotencyStatus.FAILED, reserved_cost=Decimal("0.010000"),
            error_code="LEGACY_ERROR_CODE", completed_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ))
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        _override(RecordingProvider())
        r = client.post(
            "/v1/chat/completions", headers=_headers(key_failed, idem_failed),
            json={"prompt": "legacy failed prompt", "max_tokens": 10},
        )
        assert r.status_code == 502, r.text
        assert r.headers.get("Idempotent-Replayed") == "true"
        assert r.json()["detail"]["error_code"] == "LEGACY_ERROR_CODE"

    print("2b. pre-Step-36 fingerprint compatibility: idempotency rows fingerprinted under the OLD "
          "3-key shape (no prompt_version_id key at all, exactly as they existed before this deployment) "
          "still correctly produce 409 request_in_progress (PENDING), successful replay with a legacy "
          "snapshot missing the new field (COMPLETED), and the existing safe failed-replay response "
          "(FAILED) -- none became a spurious idempotency_conflict")
    return team_ids


def main() -> None:
    created_team_ids: list = []
    created_prompt_version_ids: list = []
    try:
        _check_tracing_allowlist()
        trigger_test_id = _check_database_level_enforcement()
        created_prompt_version_ids.append(trigger_test_id)
        created_team_ids.extend(_check_pre_step36_fingerprint_compatibility())

        # ================================================================
        # 3. NO VERSION SELECTED: byte-for-byte pre-Step-36 behavior --
        #    the provider receives body.prompt EXACTLY, response has
        #    prompt_version_id=None.
        # ================================================================
        with TestClient(app) as client:
            team_id, key = _bootstrap("no-version")
            created_team_ids.append(team_id)
            provider = RecordingProvider()
            _override(provider)
            r = client.post(
                "/v1/chat/completions", headers=_headers(key, "idem-noversion-" + uuid.uuid4().hex[:8]),
                json={"prompt": "hello there", "max_tokens": 10},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["prompt_version_id"] is None
            assert all(p == "hello there" for p in provider.seen_prompts), provider.seen_prompts
        print("3. no prompt_version_id selected: provider received body.prompt UNCHANGED; "
              "response prompt_version_id is null -- exact pre-Step-36 behavior preserved")

        # ================================================================
        # 4. RENDERING + PRIVACY: an APPROVED version's template is
        #    rendered with $input == body.prompt, and THAT rendered text
        #    -- not the raw prompt -- reaches the provider. The log
        #    capture handler is attached AFTER TestClient(app) starts,
        #    since app startup's configure_logging() calls
        #    root.handlers.clear() -- attaching before that point would
        #    silently discard the handler and make the privacy assertion
        #    below pass vacuously regardless of correctness.
        # ================================================================
        template = "SYSTEM: Be concise.\nUSER: $input\nSECRET_TEMPLATE_MARKER_9f3a"
        version_id = _create_prompt_version("greeting", template, approve=True)
        created_prompt_version_ids.append(version_id)
        expected_rendered = "SYSTEM: Be concise.\nUSER: hi there\nSECRET_TEMPLATE_MARKER_9f3a"

        with TestClient(app) as client:
            capture = _CaptureHandler()
            root_logger = logging.getLogger()
            root_logger.addHandler(capture)
            try:
                team_id2, key2 = _bootstrap("rendering")
                created_team_ids.append(team_id2)
                provider2 = RecordingProvider()
                _override(provider2)
                idem2 = "idem-render-" + uuid.uuid4().hex[:8]
                r = client.post(
                    "/v1/chat/completions", headers=_headers(key2, idem2),
                    json={"prompt": "hi there", "max_tokens": 10, "prompt_version_id": str(version_id)},
                )
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["prompt_version_id"] == str(version_id)
                assert all(p == expected_rendered for p in provider2.seen_prompts), provider2.seen_prompts
            finally:
                root_logger.removeHandler(capture)

        assert capture.lines, (
            "the capture handler recorded ZERO log lines -- it is not actually attached to anything; "
            "this check would be meaningless"
        )
        print(f"4. rendering: an APPROVED version's template was rendered with $input=<client prompt>, and "
              f"the RENDERED text reached the provider ({len(capture.lines)} log lines genuinely captured)")

        joined_logs = "\n".join(capture.lines)
        assert "SECRET_TEMPLATE_MARKER_9f3a" not in joined_logs, "template content leaked into logs"
        assert "Be concise" not in joined_logs, "template content leaked into logs"
        assert expected_rendered not in joined_logs, "rendered prompt leaked into logs"
        print("4b. privacy: neither the template content nor the rendered prompt text appears in any of the "
              f"{len(capture.lines)} captured log lines")

        db = SessionLocal()
        try:
            usage_row = db.execute(
                select(UsageRecord).join(IdempotencyKey).where(
                    IdempotencyKey.team_id == team_id2, IdempotencyKey.idempotency_key == idem2
                )
            ).scalar_one()
            assert usage_row.prompt_version_id == version_id
        finally:
            db.close()
        print("4c. usage/audit linkage: usage_records.prompt_version_id correctly references the version used")

        # ================================================================
        # 5. APPROVAL RULE: a DRAFT (not yet approved) version cannot be
        #    selected.
        # ================================================================
        draft_id = _create_prompt_version("draft_only", "hello $input", approve=False)
        created_prompt_version_ids.append(draft_id)
        with TestClient(app) as client:
            team_id3, key3 = _bootstrap("draft-rejected")
            created_team_ids.append(team_id3)
            _override(RecordingProvider())
            r = client.post(
                "/v1/chat/completions", headers=_headers(key3, "idem-draft-" + uuid.uuid4().hex[:8]),
                json={"prompt": "hi", "max_tokens": 10, "prompt_version_id": str(draft_id)},
            )
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["code"] == "prompt_version_not_available"
        print("5. approval rule: a DRAFT (not-yet-approved) version is correctly rejected with 409 "
              "prompt_version_not_available")

        # ================================================================
        # 6. NONEXISTENT ID -> same 409 code.
        # ================================================================
        with TestClient(app) as client:
            team_id4, key4 = _bootstrap("nonexistent")
            created_team_ids.append(team_id4)
            _override(RecordingProvider())
            r = client.post(
                "/v1/chat/completions", headers=_headers(key4, "idem-nx-" + uuid.uuid4().hex[:8]),
                json={"prompt": "hi", "max_tokens": 10, "prompt_version_id": str(uuid.uuid4())},
            )
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["code"] == "prompt_version_not_available"
        print("6. a nonexistent prompt_version_id is rejected with the same 409 prompt_version_not_available")

        # ================================================================
        # 7. MALFORMED (non-UUID) prompt_version_id -> 400 invalid_request.
        # ================================================================
        with TestClient(app) as client:
            team_id5, key5 = _bootstrap("malformed")
            created_team_ids.append(team_id5)
            _override(RecordingProvider())
            r = client.post(
                "/v1/chat/completions", headers=_headers(key5, "idem-malformed-" + uuid.uuid4().hex[:8]),
                json={"prompt": "hi", "max_tokens": 10, "prompt_version_id": "not-a-uuid"},
            )
            assert r.status_code == 400, r.text
            assert r.json()["detail"]["code"] == "invalid_request"
        print("7. a malformed (non-UUID) prompt_version_id is rejected with 400 invalid_request")

        # ================================================================
        # 8. RETIREMENT + REPLAY AUTHORITY: retiring a version blocks NEW
        #    selections, but a REPLAY of an earlier successful request
        #    that used it still succeeds.
        # ================================================================
        retire_template = "retire-me: $input"
        retire_version_id = _create_prompt_version("retire_test", retire_template, approve=True)
        created_prompt_version_ids.append(retire_version_id)

        with TestClient(app) as client:
            team_id6, key6 = _bootstrap("retirement")
            created_team_ids.append(team_id6)
            provider6 = RecordingProvider()
            _override(provider6)
            idem6 = "idem-retire-" + uuid.uuid4().hex[:8]
            r1 = client.post(
                "/v1/chat/completions", headers=_headers(key6, idem6),
                json={"prompt": "first call", "max_tokens": 10, "prompt_version_id": str(retire_version_id)},
            )
            assert r1.status_code == 200, r1.text

            _retire_prompt_version(retire_version_id)

            r2 = client.post(
                "/v1/chat/completions", headers=_headers(key6, "idem-retire-new-" + uuid.uuid4().hex[:8]),
                json={"prompt": "second call", "max_tokens": 10, "prompt_version_id": str(retire_version_id)},
            )
            assert r2.status_code == 409, r2.text
            assert r2.json()["detail"]["code"] == "prompt_version_not_available"

            r3 = client.post(
                "/v1/chat/completions", headers=_headers(key6, idem6),
                json={"prompt": "first call", "max_tokens": 10, "prompt_version_id": str(retire_version_id)},
            )
            assert r3.status_code == 200, r3.text
            assert r3.headers.get("Idempotent-Replayed") == "true"
            assert r3.json()["prompt_version_id"] == str(retire_version_id)
            assert len(provider6.seen_prompts) == 2, "the replay must NOT have called the provider again"
        print("8. retirement: a NEW request selecting a retired version is rejected (409), but REPLAYING an "
              "earlier request that used it still succeeds -- replay never re-checks prompt-version status")

        # ================================================================
        # 9. IDEMPOTENCY: same key + same version = replay. Same key +
        #    DIFFERENT version = 409 idempotency_conflict.
        # ================================================================
        version_a = _create_prompt_version("idem_test", "A: $input", approve=True)
        version_b = _create_prompt_version("idem_test", "B: $input", approve=True)
        created_prompt_version_ids.extend([version_a, version_b])
        with TestClient(app) as client:
            team_id7, key7 = _bootstrap("idempotency")
            created_team_ids.append(team_id7)
            provider7 = RecordingProvider()
            _override(provider7)
            idem7 = "idem-conflict-" + uuid.uuid4().hex[:8]
            r1 = client.post(
                "/v1/chat/completions", headers=_headers(key7, idem7),
                json={"prompt": "shared prompt", "max_tokens": 10, "prompt_version_id": str(version_a)},
            )
            assert r1.status_code == 200, r1.text

            r2 = client.post(
                "/v1/chat/completions", headers=_headers(key7, idem7),
                json={"prompt": "shared prompt", "max_tokens": 10, "prompt_version_id": str(version_a)},
            )
            assert r2.status_code == 200 and r2.headers.get("Idempotent-Replayed") == "true"

            r3 = client.post(
                "/v1/chat/completions", headers=_headers(key7, idem7),
                json={"prompt": "shared prompt", "max_tokens": 10, "prompt_version_id": str(version_b)},
            )
            assert r3.status_code == 409, r3.text
            assert r3.json()["detail"]["code"] == "idempotency_conflict"
        print("9. idempotency: the SAME key+version replays correctly; the SAME key with a DIFFERENT "
              "version correctly 409s as idempotency_conflict")

        # ================================================================
        # 10. BUDGET ACCOUNTING: reservation/refund still correctly track
        #     actual_cost for a versioned (rendered) request.
        # ================================================================
        with TestClient(app) as client:
            team_id8, key8 = _bootstrap("budget", budget=Decimal("1000.000000"))
            created_team_ids.append(team_id8)
            budget_version = _create_prompt_version("budget_test", "template: $input", approve=True)
            created_prompt_version_ids.append(budget_version)
            provider8 = RecordingProvider(prompt_tokens=5, completion_tokens=3)
            _override(provider8)

            db = SessionLocal()
            try:
                balance_before = db.execute(
                    select(TeamBudget.remaining_amount).where(TeamBudget.team_id == team_id8)
                ).scalar_one()
            finally:
                db.close()

            r = client.post(
                "/v1/chat/completions", headers=_headers(key8, "idem-budget-" + uuid.uuid4().hex[:8]),
                json={"prompt": "check budget", "max_tokens": 10, "prompt_version_id": str(budget_version)},
            )
            assert r.status_code == 200, r.text
            actual_cost = Decimal(r.json()["actual_cost"])
            assert actual_cost > 0

            db = SessionLocal()
            try:
                balance_after = db.execute(
                    select(TeamBudget.remaining_amount).where(TeamBudget.team_id == team_id8)
                ).scalar_one()
            finally:
                db.close()
            assert balance_after == balance_before - actual_cost, (
                f"expected balance to drop by exactly actual_cost={actual_cost}, "
                f"before={balance_before} after={balance_after}"
            )
        print("10. budget accounting: reservation/refund/final charge remain exactly correct for a "
              "versioned (rendered-prompt) request")

        print("All prompt-version checks passed.")

    finally:
        app.dependency_overrides.clear()
        cleanup_db = SessionLocal()
        try:
            for tid in created_team_ids:
                key_ids = [r[0] for r in cleanup_db.execute(
                    select(IdempotencyKey.id).where(IdempotencyKey.team_id == tid)
                ).all()]
                if key_ids:
                    cleanup_db.query(UsageRecord).filter(UsageRecord.idempotency_key_id.in_(key_ids)).delete(
                        synchronize_session=False
                    )
                cleanup_db.query(IdempotencyKey).filter(IdempotencyKey.team_id == tid).delete()
                cleanup_db.query(ApiKey).filter(ApiKey.team_id == tid).delete()
                cleanup_db.query(TeamBudget).filter(TeamBudget.team_id == tid).delete()
                cleanup_db.query(Team).filter(Team.id == tid).delete()
            cleanup_db.commit()
        except Exception:
            cleanup_db.rollback()
            raise
        finally:
            cleanup_db.close()

        cleanup_db2 = SessionLocal()
        try:
            for vid in created_prompt_version_ids:
                cleanup_db2.query(PromptVersion).filter(PromptVersion.id == vid).delete()
            cleanup_db2.commit()
        except Exception:
            cleanup_db2.rollback()
            raise
        finally:
            cleanup_db2.close()

        verify_db = SessionLocal()
        try:
            for tid in created_team_ids:
                assert verify_db.get(Team, tid) is None
            for vid in created_prompt_version_ids:
                assert verify_db.get(PromptVersion, vid) is None
            print("11. all created database rows (teams, budgets, keys, usage records, prompt versions) "
                  "cleaned up")
        finally:
            verify_db.close()

        assert engine.pool.checkedout() == 0, f"expected 0 checked-out connections, got {engine.pool.checkedout()}"
        print("12. no database connections remain checked out at the end")


if __name__ == "__main__":
    main()
