"""Asynchronous compressed frame trace for audio diagnostics."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("elvin.frame_trace")


class FrameTraceWriter:
    """Write per-frame NDJSON without making audio wait for disk I/O."""

    def __init__(
        self,
        path: Path,
        *,
        max_queue: int = 5000,
        enabled: bool = True,
        batch_size: int = 100,
    ) -> None:
        self.path = path
        self.enabled = enabled
        self.batch_size = batch_size
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=max_queue
        )
        self.dropped = 0
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_file)
        self._task = asyncio.create_task(
            self._writer(), name=f"frame-trace-{self.path.stem}"
        )

    def submit(self, row: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            self.dropped += 1

    async def close(self) -> None:
        if self._task is None:
            return
        await self.queue.put(None)
        await self._task
        self._task = None
        if self.dropped:
            logger.warning(
                "Frame trace dropped records: path=%s dropped=%s",
                self.path,
                self.dropped,
            )

    def _initialize_file(self) -> None:
        with gzip.open(self.path, "wt", encoding="utf-8"):
            pass

    async def _writer(self) -> None:
        batch: list[dict[str, Any]] = []
        while True:
            item = await self.queue.get()
            if item is None:
                if batch:
                    await asyncio.to_thread(self._append_batch, batch)
                return
            batch.append(item)
            if len(batch) >= self.batch_size:
                current = batch
                batch = []
                await asyncio.to_thread(self._append_batch, current)

    def _append_batch(self, batch: list[dict[str, Any]]) -> None:
        text = "".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
            for item in batch
        )
        with gzip.open(self.path, "at", encoding="utf-8") as handle:
            handle.write(text)
