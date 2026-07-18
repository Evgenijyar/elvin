"""Health, readiness and runtime metadata endpoints."""

from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from elvin import __version__

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="elvin-backend",
        version=__version__,
    )


@router.get("/readiness")
async def readiness(
    request: Request,
    response: Response,
) -> dict[str, object]:
    store = request.app.state.store
    ready = await store.ping()

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if ready else "not_ready",
        "service": "elvin-backend",
        "version": __version__,
        "storage": store.mode,
        "storage_error": store.last_error,
    }


@router.get("/meta")
async def meta(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    store = request.app.state.store
    stored_key = (
        await store.get_setting("gemini_api_key") or ""
    ).strip()

    return {
        "version": __version__,
        "environment": settings.environment,
        "calls_enabled": request.app.state.calls_enabled,
        "media_ready": request.app.state.media_ready,
        "gemini_key_configured": bool(
            stored_key or settings.gemini_key_configured
        ),
        "public_base_url": settings.public_base_url,
    }
