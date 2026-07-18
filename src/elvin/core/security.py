"""Single-user session helpers."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta


def create_session(ttl_hours: int) -> tuple[str, str, str]:
    """Return raw token, token hash and ISO expiration timestamp."""
    raw_token = secrets.token_urlsafe(36)
    token_hash = hash_session_token(raw_token)
    expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
    return raw_token, token_hash, expires_at.isoformat()


def hash_session_token(raw_token: str) -> str:
    """Hash a browser session token before persistence."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def session_is_active(expires_at: str | None) -> bool:
    """Check whether a stored session expiration is still valid."""
    if not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(expires_at)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed > datetime.now(UTC)
    except ValueError:
        return False
