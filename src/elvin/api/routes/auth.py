"""LPTracker-backed authentication endpoints."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from elvin.api.dependencies import get_lptracker, get_store
from elvin.core.security import create_session, hash_session_token, session_is_active
from elvin.infrastructure.state_store import StateStore
from elvin.integrations.lptracker import LPTrackerClient, LPTrackerError

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=500)


@router.get("/status")
async def auth_status(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    store: StateStore = request.app.state.store
    raw_cookie = request.cookies.get(settings.session_cookie_name)
    stored_hash = await store.get_setting("auth_session_hash")
    expires_at = await store.get_setting("auth_session_expires_at")
    authenticated = bool(
        raw_cookie
        and stored_hash
        and hash_session_token(raw_cookie) == stored_hash
        and session_is_active(expires_at)
    )
    return {
        "authenticated": authenticated,
        "login": await store.get_setting("lptracker_login") if authenticated else None,
    }


@router.post("/login")
async def login(
    payload: LoginRequest,
    response: Response,
    request: Request,
    store: Annotated[StateStore, Depends(get_store)],
    lptracker: Annotated[LPTrackerClient, Depends(get_lptracker)],
) -> dict[str, object]:
    try:
        token = await lptracker.login(payload.login.strip(), payload.password)
    except LPTrackerError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    settings = request.app.state.settings
    raw_session, session_hash, expires_at = create_session(
        settings.session_ttl_hours
    )
    await store.set_setting("lptracker_token", token)
    await store.set_setting("lptracker_login", payload.login.strip())
    await store.set_setting("auth_session_hash", session_hash)
    await store.set_setting("auth_session_expires_at", expires_at)
    await store.set_setting("lptracker_login_at", datetime.now(UTC).isoformat())

    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_session,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )
    return {"success": True, "login": payload.login.strip()}


@router.post("/logout")
async def logout(
    response: Response,
    request: Request,
    store: Annotated[StateStore, Depends(get_store)],
) -> dict[str, bool]:
    settings = request.app.state.settings
    await store.delete_settings(
        [
            "lptracker_token",
            "lptracker_login",
            "auth_session_hash",
            "auth_session_expires_at",
        ]
    )
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"success": True}
