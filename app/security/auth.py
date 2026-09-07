from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiKey, Team
from app.security.api_keys import (
    ApiKeyFormatError,
    parse_api_key,
    verify_api_key_secret,
)


def _auth_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


@dataclass(frozen=True)
class AuthContext:
    team: Team
    api_key: ApiKey


def _extract_raw_key(authorization: str | None) -> str:
    if authorization is None:
        raise _auth_error()

    parts = authorization.split()

    if len(parts) != 2:
        raise _auth_error()

    scheme, token = parts

    if scheme.lower() != "bearer":
        raise _auth_error()

    return token


def get_current_auth(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AuthContext:
    raw_key = _extract_raw_key(authorization)

    try:
        parsed = parse_api_key(raw_key)
    except ApiKeyFormatError:
        raise _auth_error() from None

    api_key = db.execute(
        select(ApiKey).where(
            ApiKey.public_key_id == parsed.public_key_id
        )
    ).scalar_one_or_none()

    if api_key is None:
        raise _auth_error()

    secret = parsed.secret.get_secret_value()

    if not verify_api_key_secret(api_key.secret_hash, secret):
        raise _auth_error()

    now = datetime.now(timezone.utc)

    if api_key.revoked_at is not None:
        raise _auth_error()

    if api_key.expires_at is not None and api_key.expires_at <= now:
        raise _auth_error()

    team = db.get(Team, api_key.team_id)

    if team is None or not team.is_active:
        raise _auth_error()

    # last_used_at is intentionally not updated here. Authentication
    # must not commit the request's shared database session. A later
    # background or independently committed mechanism can record it
    # without adding a synchronous write to every authenticated request.

    return AuthContext(team=team, api_key=api_key)