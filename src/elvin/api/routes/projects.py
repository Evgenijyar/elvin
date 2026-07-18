"""LPTracker projects, stages and lead-preview endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from elvin.api.dependencies import get_lptracker, require_lptracker_token
from elvin.integrations.lptracker import LPTrackerClient, LPTrackerError

router = APIRouter(prefix="/projects", tags=["projects"])


def _http_error(exc: LPTrackerError) -> HTTPException:
    code = (
        status.HTTP_401_UNAUTHORIZED
        if exc.http_status == 401 or exc.api_code == 401
        else status.HTTP_502_BAD_GATEWAY
    )
    return HTTPException(status_code=code, detail=str(exc))


@router.get("")
async def projects(
    token: Annotated[str, Depends(require_lptracker_token)],
    client: Annotated[LPTrackerClient, Depends(get_lptracker)],
) -> dict[str, object]:
    try:
        items = await client.projects(token)
    except LPTrackerError as exc:
        raise _http_error(exc) from exc
    return {"items": items}


@router.get("/{project_id}/stages")
async def stages(
    project_id: int,
    token: Annotated[str, Depends(require_lptracker_token)],
    client: Annotated[LPTrackerClient, Depends(get_lptracker)],
) -> dict[str, object]:
    try:
        items = await client.stages(token, project_id)
    except LPTrackerError as exc:
        raise _http_error(exc) from exc
    return {"items": items}


@router.get("/{project_id}/lead-preview")
async def lead_preview(
    project_id: int,
    stage_id: Annotated[int, Query(gt=0)],
    token: Annotated[str, Depends(require_lptracker_token)],
    client: Annotated[LPTrackerClient, Depends(get_lptracker)],
) -> dict[str, object]:
    try:
        return await client.lead_preview(token, project_id, stage_id)
    except LPTrackerError as exc:
        raise _http_error(exc) from exc
