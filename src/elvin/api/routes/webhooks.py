"""Public LPTracker lead webhook receiver."""

import json
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/webhooks/lptracker", tags=["webhooks"])


@router.api_route("/lead", methods=["POST", "PUT"])
async def lead_webhook(request: Request) -> dict[str, bool]:
    content_type = request.headers.get("content-type", "")
    raw = await request.body()
    payload: dict[str, Any]
    try:
        decoded = json.loads(raw.decode("utf-8")) if raw else {}
        payload = decoded if isinstance(decoded, dict) else {"data": decoded}
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {"raw": raw.decode("utf-8", errors="replace")}

    project_id = _find_project_id(payload)
    await request.app.state.store.save_webhook_event(
        project_id,
        request.method,
        content_type,
        payload,
    )
    return {"success": True}


def _find_project_id(payload: dict[str, Any]) -> int | None:
    candidates = [
        payload.get("project_id"),
        payload.get("projectId"),
        (payload.get("lead") or {}).get("project_id")
        if isinstance(payload.get("lead"), dict)
        else None,
        (payload.get("view") or {}).get("project_id")
        if isinstance(payload.get("view"), dict)
        else None,
    ]
    for value in candidates:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None
