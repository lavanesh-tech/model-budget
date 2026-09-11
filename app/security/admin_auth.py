"""Separate, fail-closed authentication for the operator dashboard.

This deliberately does not accept tenant gateway keys and never logs a key.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.config import get_settings


def require_admin(
    x_modelbudget_admin_key: str | None = Header(default=None, alias="X-ModelBudget-Admin-Key"),
) -> None:
    configured = get_settings().admin_api_key
    if configured is None or x_modelbudget_admin_key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    expected = configured.get_secret_value()
    if not hmac.compare_digest(x_modelbudget_admin_key, expected):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
