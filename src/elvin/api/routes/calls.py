"""Persistent completed-call history and Gemini Live transcripts."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from elvin.api.dependencies import get_store, require_session
from elvin.infrastructure.state_store import StateStore

router = APIRouter(prefix="/calls", tags=["calls"])


@router.get("")
async def list_calls(
    store: Annotated[StateStore, Depends(get_store)],
    _session: Annotated[str, Depends(require_session)],
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    phone: str = Query(default="", max_length=64),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    items, total = await store.list_calls(
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        phone=phone,
        limit=limit,
        offset=offset,
    )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
