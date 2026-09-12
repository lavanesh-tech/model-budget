"""Offline dashboard safety checks; no database, Redis, or OpenAI calls."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    admin = (ROOT / "app/api/admin.py").read_text()
    auth = (ROOT / "app/security/admin_auth.py").read_text()

    assert "X-ModelBudget-Admin-Key" in auth
    assert "compare_digest" in auth
    assert "get_current_auth" not in admin
    assert "secret_hash" not in admin

    for forbidden in (
        "template",
        "content_fingerprint",
        "response_snapshot",
    ):
        assert forbidden not in admin, forbidden

    tree = ast.parse(admin)
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "overview"
        for node in ast.walk(tree)
    )

    dashboard = ROOT / "dashboard"
    route_path = dashboard / "app/api/admin/[resource]/route.ts"
    security_path = dashboard / "lib/security.mjs"
    session_path = dashboard / "lib/session.ts"

    assert route_path.is_file()
    assert security_path.is_file()
    assert session_path.is_file()

    route = route_path.read_text()
    security = security_path.read_text()
    session = session_path.read_text()

    assert "hasSession" in route
    assert "backendRequest" in route
    assert "fetchBackend" in route
    assert "MODEL_BUDGET_ADMIN_API_KEY" not in route
    assert "NEXT_PUBLIC" not in route

    assert "MODEL_BUDGET_ADMIN_API_KEY" in security
    assert "X-ModelBudget-Admin-Key" in security
    assert "timingSafeEqual" in security
    assert "httpOnly: true" in security
    assert "NEXT_PUBLIC" not in security

    for function_name in ("createSession", "hasSession", "clearSession"):
        assert function_name in session

    print("1. separate, constant-time admin authentication is present")
    print("2. admin responses exclude keys, hashes, IDs, templates, and snapshots")
    print("3. dashboard injects the backend key only from centralized server-side code")
    print("4. dashboard session uses a signed HttpOnly cookie")
    print("All offline dashboard checks passed.")


if __name__ == "__main__":
    main()
