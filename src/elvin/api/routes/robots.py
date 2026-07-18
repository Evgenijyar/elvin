"""Saved AI robot profile CRUD."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from elvin.api.dependencies import get_store, require_session
from elvin.infrastructure.state_store import StateStore
from elvin.integrations.gemini import GEMINI_LIVE_MODEL_ID
from elvin.integrations.voices import VOICE_OPTIONS

router = APIRouter(prefix="/robots", tags=["robots"])
VOICE_NAMES = {item.name for item in VOICE_OPTIONS}


def _normalized_payload(payload: "RobotPayload") -> dict[str, object]:
    data = payload.model_dump()
    data["model_id"] = GEMINI_LIVE_MODEL_ID
    if data["voice_name"] not in VOICE_NAMES:
        raise HTTPException(status_code=400, detail="Неизвестный голос Gemini.")
    data["first_phrase"] = ""
    return data


class RobotPayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    model_id: str = Field(
        default="gemini-3.1-flash-live-preview",
        min_length=1,
        max_length=200,
    )
    voice_name: str = Field(default="Kore", min_length=1, max_length=100)
    temperature: float = Field(default=0.3, ge=0, le=2)
    role_prompt: str = Field(default="", max_length=100_000)
    knowledge_base: str = Field(default="", max_length=200_000)
    first_phrase: str = Field(default="", max_length=5000)
    active: bool = True


@router.get("")
async def list_robots(
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    return {"items": await store.list_robots()}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_robot(
    payload: RobotPayload,
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    item = await store.create_robot(_normalized_payload(payload))
    return {"success": True, "item": item}


@router.put("/{robot_id}")
async def update_robot(
    robot_id: str,
    payload: RobotPayload,
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    item = await store.update_robot(robot_id, _normalized_payload(payload))
    if item is None:
        raise HTTPException(status_code=404, detail="Робот не найден.")
    return {"success": True, "item": item}


@router.delete("/{robot_id}")
async def delete_robot(
    robot_id: str,
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, bool]:
    deleted = await store.delete_robot(robot_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Робот не найден.")
    return {"success": True}
