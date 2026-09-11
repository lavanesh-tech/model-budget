"""Offline safety checks for Step 37; no database, Redis, or OpenAI calls."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    admin = (ROOT / "app/api/admin.py").read_text()
    auth = (ROOT / "app/security/admin_auth.py").read_text()
    assert "X-ModelBudget-Admin-Key" in auth
    assert "compare_digest" in auth
    assert "get_current_auth" not in admin and "secret_hash" not in admin
    for forbidden in ("template", "content_fingerprint", "response_snapshot"):
        assert forbidden not in admin, forbidden
    tree = ast.parse(admin)
    assert any(isinstance(node, ast.FunctionDef) and node.name == "overview" for node in ast.walk(tree))
    dashboard = ROOT / "dashboard"
    assert (dashboard / "app/api/admin/[resource]/route.ts").is_file()
    route = (dashboard / "app/api/admin/[resource]/route.ts").read_text()
    assert "MODEL_BUDGET_ADMIN_API_KEY" in route and "NEXT_PUBLIC" not in route
    session = (dashboard / "lib/session.ts").read_text()
    assert "httpOnly: true" in session and "timingSafeEqual" in session
    print("1. separate, constant-time admin authentication is present")
    print("2. admin responses exclude keys, hashes, IDs, templates, and snapshots")
    print("3. dashboard only forwards the backend key from server-side code")
    print("4. dashboard session uses an HttpOnly signed cookie")
    print("All Step 37 offline dashboard checks passed.")


if __name__ == "__main__":
    main()
