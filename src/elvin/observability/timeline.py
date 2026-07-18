"""Per-call monotonic event timeline."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("elvin.timeline")


@dataclass(slots=True)
class TimelineEvent:
    sequence: int
    relative_ms: float
    event: str
    details: dict[str, Any]


class CallTimeline:
    """Collect important events without blocking the real-time audio path."""

    def __init__(self, call_id: str, directory: Path) -> None:
        self.call_id = call_id
        self.directory = directory
        self.started_at = time.monotonic()
        self.events: list[TimelineEvent] = []

    def add(self, event: str, **details: Any) -> TimelineEvent:
        item = TimelineEvent(
            sequence=len(self.events) + 1,
            relative_ms=round((time.monotonic() - self.started_at) * 1000, 3),
            event=event,
            details=details,
        )
        self.events.append(item)
        if details:
            logger.info(
                "Call timeline: call=%s t=%.3fms event=%s details=%s",
                self.call_id,
                item.relative_ms,
                event,
                details,
            )
        else:
            logger.info(
                "Call timeline: call=%s t=%.3fms event=%s",
                self.call_id,
                item.relative_ms,
                event,
            )
        return item

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started_at) * 1000

    async def save(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{self.call_id}-timeline.json"
        payload = {
            "call_id": self.call_id,
            "events": [asdict(item) for item in self.events],
        }
        import asyncio

        await asyncio.to_thread(
            target.write_text,
            json.dumps(payload, ensure_ascii=False, indent=2),
            "utf-8",
        )
        return target
