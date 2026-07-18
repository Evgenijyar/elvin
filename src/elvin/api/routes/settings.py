"""Authenticated application settings endpoints."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from elvin.api.dependencies import get_store, require_session
from elvin.infrastructure.state_store import StateStore
from elvin.integrations.voices import as_api_items
from elvin.integrations.gemini import (
    GEMINI_LIVE_MODEL_ID,
    GEMINI_LIVE_WEBSOCKET_ENDPOINT,
    GeminiConnectionError,
    test_gemini_live_connection,
)

logger = logging.getLogger("elvin.gemini")
router = APIRouter(prefix="/settings", tags=["settings"])


class GeminiSettingsPayload(BaseModel):
    api_key: str = Field(default="", max_length=2000)


class GeminiTestPayload(BaseModel):
    api_key: str = Field(default="", max_length=2000)


async def _resolve_key(
    request: Request,
    store: StateStore,
) -> tuple[str, str]:
    stored = (await store.get_setting("gemini_api_key") or "").strip()
    if stored:
        return stored, "storage"

    configured = request.app.state.settings.gemini_api_key
    if configured is not None:
        environment_value = configured.get_secret_value().strip()
        if environment_value:
            return environment_value, "environment"

    return "", "not_configured"


@router.get("/gemini")
async def get_gemini_settings(
    request: Request,
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    api_key, source = await _resolve_key(request, store)
    return {
        "api_key": api_key,
        "configured": bool(api_key),
        "source": source,
        "model_id": GEMINI_LIVE_MODEL_ID,
        "websocket_endpoint": GEMINI_LIVE_WEBSOCKET_ENDPOINT,
        "voices": as_api_items(),
    }


@router.put("/gemini")
async def save_gemini_settings(
    payload: GeminiSettingsPayload,
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    api_key = payload.api_key.strip()
    if api_key:
        await store.set_setting("gemini_api_key", api_key)
    else:
        await store.delete_settings(["gemini_api_key"])

    return {
        "success": True,
        "configured": bool(api_key),
        "model_id": GEMINI_LIVE_MODEL_ID,
        "websocket_endpoint": GEMINI_LIVE_WEBSOCKET_ENDPOINT,
    }


@router.post("/gemini/test")
async def test_gemini_settings(
    payload: GeminiTestPayload,
    request: Request,
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    candidate = payload.api_key.strip()
    if not candidate:
        candidate, _source = await _resolve_key(request, store)
    if not candidate:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Сначала укажите Gemini API key.",
        )

    try:
        result = await test_gemini_live_connection(candidate)
    except GeminiConnectionError as exc:
        # Never log the key; the exception text contains only safe
        # endpoint, status, close-code, and Google error diagnostics.
        logger.warning("Gemini Live validation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return {"success": True, **result}
