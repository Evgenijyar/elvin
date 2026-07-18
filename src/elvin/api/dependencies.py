"""FastAPI dependencies shared by API routes."""

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status

from elvin.config import Settings
from elvin.core.security import hash_session_token, session_is_active
from elvin.infrastructure.state_store import StateStore
from elvin.integrations.lptracker import LPTrackerClient


def get_store(request: Request) -> StateStore:
    return request.app.state.store


def get_lptracker(request: Request) -> LPTrackerClient:
    return request.app.state.lptracker


def get_settings_from_app(request: Request) -> Settings:
    return request.app.state.settings


async def require_session(
    request: Request,
    store: Annotated[StateStore, Depends(get_store)],
    raw_cookie: Annotated[str | None, Cookie(alias="elvin_session")] = None,
) -> str:
    settings: Settings = request.app.state.settings
    cookie_value = request.cookies.get(settings.session_cookie_name) or raw_cookie
    if not cookie_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется вход через LPTracker.",
        )

    stored_hash = await store.get_setting("auth_session_hash")
    expires_at = await store.get_setting("auth_session_expires_at")
    if (
        not stored_hash
        or hash_session_token(cookie_value) != stored_hash
        or not session_is_active(expires_at)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Сессия истекла. Выполните вход повторно.",
        )
    return cookie_value


async def require_lptracker_token(
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> str:
    token = await store.get_setting("lptracker_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Токен LPTracker отсутствует. Выполните вход повторно.",
        )
    return token
