"""Project-to-robot assignment dashboard and call queues."""

from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from elvin.api.dependencies import (
    get_lptracker,
    get_store,
    require_lptracker_token,
    require_session,
)
from elvin.infrastructure.state_store import StateStore
from elvin.integrations.lptracker import LPTrackerClient, LPTrackerError
from elvin.services.call_queue import CallQueueError, CallQueueManager

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class AssignmentCreate(BaseModel):
    project_id: int
    robot_id: str


class AssignmentUpdate(BaseModel):
    source_stage_id: int | None = None
    source_stage_name: str | None = None
    call_limit: int | None = Field(default=None, ge=1, le=1000)
    max_call_minutes: int | None = Field(default=None, ge=1, le=120)


def _call_queue(request: Request) -> CallQueueManager:
    return request.app.state.call_queue


async def _gemini_key(request: Request, store: StateStore) -> str:
    stored = (await store.get_setting("gemini_api_key") or "").strip()
    if stored:
        return stored
    configured = request.app.state.settings.gemini_api_key
    return configured.get_secret_value().strip() if configured is not None else ""


@router.get("")
async def dashboard(
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    return {"items": await store.list_assignments()}


@router.post("/assignments", status_code=status.HTTP_201_CREATED)
async def create_assignment(
    payload: AssignmentCreate,
    request: Request,
    store: Annotated[StateStore, Depends(get_store)],
    client: Annotated[LPTrackerClient, Depends(get_lptracker)],
    token: Annotated[str, Depends(require_lptracker_token)],
) -> dict[str, object]:
    robot = await store.get_robot(payload.robot_id)
    if robot is None:
        raise HTTPException(status_code=404, detail="Робот не найден.")

    try:
        projects = await client.projects(token)
    except LPTrackerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    project = next(
        (item for item in projects if item["id"] == payload.project_id),
        None,
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Проект LPTracker не найден.")

    try:
        item = await store.create_assignment(
            {
                "project_id": project["id"],
                "project_name": project["name"],
                "robot_id": payload.robot_id,
                "sort_order": len(await store.list_assignments()),
                "call_limit": 50,
                "max_call_minutes": 5,
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=409,
            detail="Этот робот уже добавлен в выбранный проект.",
        ) from exc

    settings = request.app.state.settings
    webhook_registered = False
    if settings.public_base_url:
        callback_url = f"{settings.public_base_url}/api/webhooks/lptracker/lead"
        try:
            webhook_registered = await client.register_lead_webhook(
                token,
                project["id"],
                callback_url,
            )
        except LPTrackerError:
            webhook_registered = False
        await store.update_assignment(
            item["id"],
            {"webhook_registered": webhook_registered},
        )

    return {
        "success": True,
        "item": await store.get_assignment(item["id"]),
    }


@router.put("/assignments/{assignment_id}")
async def update_assignment(
    assignment_id: str,
    payload: AssignmentUpdate,
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    item = await store.update_assignment(
        assignment_id,
        payload.model_dump(exclude_unset=True),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Назначение не найдено.")
    return {"success": True, "item": await store.get_assignment(assignment_id)}


@router.delete("/assignments/{assignment_id}")
async def delete_assignment(
    assignment_id: str,
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, bool]:
    deleted = await store.delete_assignment(assignment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Назначение не найдено.")
    return {"success": True}


@router.get("/assignments/{assignment_id}/lead-preview")
async def assignment_lead_preview(
    assignment_id: str,
    store: Annotated[StateStore, Depends(get_store)],
    client: Annotated[LPTrackerClient, Depends(get_lptracker)],
    token: Annotated[str, Depends(require_lptracker_token)],
) -> dict[str, object]:
    assignment = await store.get_assignment(assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Назначение не найдено.")
    stage_id = assignment.get("source_stage_id")
    if not stage_id:
        raise HTTPException(
            status_code=400,
            detail="Сначала выберите стадию-источник лидов.",
        )
    try:
        return await client.lead_preview(
            token,
            int(assignment["project_id"]),
            int(stage_id),
        )
    except LPTrackerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/assignments/{assignment_id}/queue")
async def prepare_assignment_queue(
    assignment_id: str,
    request: Request,
    store: Annotated[StateStore, Depends(get_store)],
    token: Annotated[str, Depends(require_lptracker_token)],
) -> dict[str, object]:
    assignment = await store.get_assignment(assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Назначение не найдено.")
    try:
        batch = await _call_queue(request).prepare(assignment, token)
    except (CallQueueError, LPTrackerError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "success": True,
        "batch": batch,
        "items": await store.list_call_items(batch["id"]),
    }


@router.get("/assignments/{assignment_id}/queue")
async def get_assignment_queue(
    assignment_id: str,
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    batch = await store.get_latest_call_batch(assignment_id)
    if batch is None:
        return {"batch": None, "items": []}
    return {
        "batch": batch,
        "items": await store.list_call_items(batch["id"]),
    }


@router.post("/assignments/{assignment_id}/start")
async def start_assignment(
    assignment_id: str,
    request: Request,
    store: Annotated[StateStore, Depends(get_store)],
    token: Annotated[str, Depends(require_lptracker_token)],
) -> dict[str, object]:
    assignment = await store.get_assignment(assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Назначение не найдено.")
    if not assignment.get("source_stage_id"):
        raise HTTPException(status_code=400, detail="Не выбрана стадия лидов.")
    if not await _gemini_key(request, store):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Сначала сохраните и проверьте Gemini API key в Настройках.",
        )
    try:
        batch = await _call_queue(request).start(assignment, token)
    except (CallQueueError, LPTrackerError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return {"success": True, "batch": batch}


@router.post("/assignments/{assignment_id}/stop")
async def stop_assignment(
    assignment_id: str,
    request: Request,
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    batch = await _call_queue(request).stop(assignment_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Активная очередь не найдена.")
    return {"success": True, "batch": batch}
