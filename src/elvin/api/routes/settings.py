"""Authenticated application settings endpoints."""

import asyncio
import logging
from typing import Annotated, Literal

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
    director_api_key: str = Field(default="", max_length=2000)


class GeminiTestPayload(BaseModel):
    api_key: str = Field(default="", max_length=2000)
    director_api_key: str = Field(default="", max_length=2000)
    target: Literal["actor", "director", "both"] = "both"


async def _resolve_key(
    request: Request,
    store: StateStore,
    *,
    director: bool = False,
) -> tuple[str, str]:
    storage_key = "gemini_director_api_key" if director else "gemini_api_key"
    stored = (await store.get_setting(storage_key) or "").strip()
    if stored:
        return stored, "storage"

    configured = (
        request.app.state.settings.gemini_director_api_key
        if director
        else request.app.state.settings.gemini_api_key
    )
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
    actor_key, actor_source = await _resolve_key(request, store)
    director_key, director_source = await _resolve_key(
        request, store, director=True
    )
    return {
        # Keep ``api_key`` for backward-compatible frontend/API clients.
        "api_key": actor_key,
        "actor_api_key": actor_key,
        "director_api_key": director_key,
        "configured": bool(actor_key),
        "actor_configured": bool(actor_key),
        "director_configured": bool(director_key),
        "source": actor_source,
        "actor_source": actor_source,
        "director_source": director_source,
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
    actor_key = payload.api_key.strip()
    director_key = payload.director_api_key.strip()
    if actor_key:
        await store.set_setting("gemini_api_key", actor_key)
    else:
        await store.delete_settings(["gemini_api_key"])
    if director_key:
        await store.set_setting("gemini_director_api_key", director_key)
    else:
        await store.delete_settings(["gemini_director_api_key"])

    return {
        "success": True,
        "configured": bool(actor_key),
        "actor_configured": bool(actor_key),
        "director_configured": bool(director_key),
        "model_id": GEMINI_LIVE_MODEL_ID,
        "websocket_endpoint": GEMINI_LIVE_WEBSOCKET_ENDPOINT,
    }


async def _test_key(label: str, key: str) -> dict[str, object]:
    if not key:
        return {"label": label, "success": False, "message": "Ключ не указан."}
    try:
        result = await test_gemini_live_connection(key)
    except GeminiConnectionError as exc:
        return {"label": label, "success": False, "message": str(exc)}
    return {"label": label, "success": True, **result}


@router.post("/gemini/test")
async def test_gemini_settings(
    payload: GeminiTestPayload,
    request: Request,
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    actor = payload.api_key.strip()
    director = payload.director_api_key.strip()
    if not actor:
        actor, _ = await _resolve_key(request, store)
    if not director:
        director, _ = await _resolve_key(request, store, director=True)

    targets: list[tuple[str, str]] = []
    if payload.target in {"actor", "both"}:
        targets.append(("Актёр", actor))
    if payload.target in {"director", "both"}:
        targets.append(("Режиссёр", director))
    if not any(key for _, key in targets):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Сначала укажите хотя бы один Gemini API key.",
        )

    results = await asyncio.gather(*(_test_key(label, key) for label, key in targets))
    success = all(bool(item.get("success")) for item in results)
    if not success:
        messages = "; ".join(
            f"{item['label']}: {item['message']}" for item in results
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=messages,
        )
    return {
        "success": True,
        "results": results,
        "message": "Оба подключения Gemini Live подтверждены."
        if len(results) == 2
        else str(results[0].get("message") or "Подключение подтверждено."),
    }
