"""Project-to-robot assignment dashboard and call queues."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import asyncpg
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    status,
)
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

_BACKGROUND_AUDIO_MAX_BYTES = 50 * 1024 * 1024
_BACKGROUND_AUDIO_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}


class AssignmentCreate(BaseModel):
    project_id: int
    robot_id: str


class AssignmentUpdate(BaseModel):
    source_stage_id: int | None = None
    source_stage_name: str | None = None
    lead_stage_id: int | None = None
    lead_stage_name: str | None = None
    special_stage_id: int | None = None
    special_stage_name: str | None = None
    refusal_stage_id: int | None = None
    refusal_stage_name: str | None = None
    callback_stage_id: int | None = None
    callback_stage_name: str | None = None
    stop_list_stage_id: int | None = None
    stop_list_stage_name: str | None = None
    answering_machine_stage_id: int | None = None
    answering_machine_stage_name: str | None = None
    no_answer_stage_id: int | None = None
    no_answer_stage_name: str | None = None
    count_special_as_lead: bool | None = None
    call_limit: int | None = Field(default=None, ge=1, le=1000)
    lead_limit: int | None = Field(default=None, ge=0, le=1000)
    max_call_minutes: int | None = Field(default=None, ge=1, le=120)
    background_audio_volume: int | None = Field(default=None, ge=0, le=100)


def _call_queue(request: Request) -> CallQueueManager:
    return request.app.state.call_queue


def _background_audio_dir(request: Request) -> Path:
    return request.app.state.settings.data_dir / "background-audio"


def _background_audio_path(request: Request, assignment_id: str) -> Path:
    return _background_audio_dir(request) / f"{assignment_id}.pcm"


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
                "lead_limit": 0,
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


@router.post("/assignments/{assignment_id}/background-audio")
async def upload_background_audio(
    assignment_id: str,
    request: Request,
    file: Annotated[UploadFile, File(...)],
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    assignment = await store.get_assignment(assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Назначение не найдено.")

    original_name = Path(file.filename or "background-audio").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in _BACKGROUND_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Поддерживаются WAV, MP3, M4A, AAC, FLAC, OGG, OPUS и WEBM.",
        )
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(
            status_code=503,
            detail="На сервере не найден ffmpeg для подготовки фонового аудио.",
        )

    audio_dir = _background_audio_dir(request)
    audio_dir.mkdir(parents=True, exist_ok=True)
    source_path = audio_dir / f".{assignment_id}-{uuid4().hex}{suffix}"
    output_tmp = audio_dir / f".{assignment_id}-{uuid4().hex}.pcm"
    target_path = _background_audio_path(request, assignment_id)
    total = 0
    try:
        with source_path.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > _BACKGROUND_AUDIO_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Фоновый аудиофайл не должен превышать 50 МБ.",
                    )
                destination.write(chunk)
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-acodec",
            "pcm_s16le",
            "-f",
            "s16le",
            str(output_tmp),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if (
            process.returncode != 0
            or not output_tmp.exists()
            or output_tmp.stat().st_size < 2
        ):
            message = stderr.decode("utf-8", errors="replace").strip()
            raise HTTPException(
                status_code=400,
                detail=(
                    "Не удалось декодировать аудиофайл."
                    + (f" {message[:300]}" if message else "")
                ),
            )
        output_tmp.replace(target_path)
        await store.update_assignment(
            assignment_id,
            {"background_audio_filename": original_name},
        )
    finally:
        await file.close()
        source_path.unlink(missing_ok=True)
        output_tmp.unlink(missing_ok=True)

    return {"success": True, "item": await store.get_assignment(assignment_id)}


@router.delete("/assignments/{assignment_id}/background-audio")
async def delete_background_audio(
    assignment_id: str,
    request: Request,
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    assignment = await store.get_assignment(assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Назначение не найдено.")
    _background_audio_path(request, assignment_id).unlink(missing_ok=True)
    await store.update_assignment(
        assignment_id,
        {"background_audio_filename": ""},
    )
    return {"success": True, "item": await store.get_assignment(assignment_id)}


@router.delete("/assignments/{assignment_id}")
async def delete_assignment(
    assignment_id: str,
    request: Request,
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, bool]:
    deleted = await store.delete_assignment(assignment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Назначение не найдено.")
    _background_audio_path(request, assignment_id).unlink(missing_ok=True)
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
