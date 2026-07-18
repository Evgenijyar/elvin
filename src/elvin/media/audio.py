"""PCM utilities for telephony diagnostics, buffering and recording."""

from __future__ import annotations

import asyncio
import math
import sys
from array import array
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

TELEPHONY_SAMPLE_RATE = 16_000
GEMINI_OUTPUT_SAMPLE_RATE = 24_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
DEFAULT_PTIME_MS = 20
DEFAULT_FRAME_BYTES = 640


@dataclass(frozen=True, slots=True)
class AudioLevels:
    rms: float
    peak: float
    dbfs: float


def measure_pcm16(pcm: bytes) -> AudioLevels:
    """Return normalized RMS, peak and dBFS for signed PCM16 mono audio."""
    if not pcm:
        return AudioLevels(0.0, 0.0, -120.0)
    usable = pcm[: len(pcm) - (len(pcm) % 2)]
    samples = array("h")
    samples.frombytes(usable)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return AudioLevels(0.0, 0.0, -120.0)
    square_sum = sum(int(sample) * int(sample) for sample in samples)
    rms_raw = math.sqrt(square_sum / len(samples))
    peak_raw = max(abs(int(sample)) for sample in samples)
    rms = rms_raw / 32768.0
    peak = peak_raw / 32768.0
    dbfs = 20.0 * math.log10(max(rms, 1.0 / 32768.0))
    return AudioLevels(rms=rms, peak=peak, dbfs=dbfs)


class AdaptiveNoiseFloor:
    """Slowly track line noise while avoiding adaptation to active speech."""

    def __init__(self, initial_dbfs: float = -60.0) -> None:
        self.dbfs = initial_dbfs
        self.initialized = False

    def update(self, current_dbfs: float, *, speech_likely: bool) -> float:
        if not self.initialized:
            self.dbfs = current_dbfs
            self.initialized = True
            return self.dbfs
        # Quiet frames update more quickly; probable speech only lets the
        # estimate decay very slowly so the voice cannot become "the noise".
        alpha = 0.08 if not speech_likely else 0.002
        capped = min(current_dbfs, self.dbfs + 4.0) if speech_likely else current_dbfs
        self.dbfs = (1.0 - alpha) * self.dbfs + alpha * capped
        self.dbfs = max(-90.0, min(-20.0, self.dbfs))
        return self.dbfs


class PreRollBuffer:
    """Bounded FIFO retaining audio immediately before VAD fires."""

    def __init__(
        self,
        *,
        milliseconds: int = 240,
        sample_rate: int = TELEPHONY_SAMPLE_RATE,
    ) -> None:
        self.max_bytes = int(
            sample_rate * (milliseconds / 1000.0) * SAMPLE_WIDTH_BYTES
        )
        self._parts: deque[bytes] = deque()
        self._size = 0

    def append(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._parts.append(pcm)
        self._size += len(pcm)
        while self._parts and self._size > self.max_bytes:
            removed = self._parts.popleft()
            self._size -= len(removed)

    def snapshot(self) -> bytes:
        return b"".join(self._parts)

    def clear(self) -> None:
        self._parts.clear()
        self._size = 0


class PcmLevelWindow:
    """Aggregate all frames but produce one readable line per interval."""

    def __init__(self, interval_seconds: float = 1.0) -> None:
        self.interval_seconds = interval_seconds
        self.started_at = time.monotonic()
        self.window_started_at = self.started_at
        self.frames = 0
        self.bytes = 0
        self.square_sum = 0.0
        self.peak = 0.0
        self.speech_frames = 0

    def add(
        self,
        levels: AudioLevels,
        frame_bytes: int,
        *,
        speech: bool,
    ) -> dict[str, float | int] | None:
        self.frames += 1
        self.bytes += frame_bytes
        self.square_sum += levels.rms * levels.rms
        self.peak = max(self.peak, levels.peak)
        if speech:
            self.speech_frames += 1

        now = time.monotonic()
        if now - self.window_started_at < self.interval_seconds:
            return None

        rms = math.sqrt(self.square_sum / max(1, self.frames))
        dbfs = 20.0 * math.log10(max(rms, 1.0 / 32768.0))
        result: dict[str, float | int] = {
            "age": round(now - self.started_at, 1),
            "frames": self.frames,
            "bytes": self.bytes,
            "speech_frames": self.speech_frames,
            "rms": round(rms, 5),
            "peak": round(self.peak, 5),
            "dbfs": round(dbfs, 1),
        }
        self.window_started_at = now
        self.frames = 0
        self.bytes = 0
        self.square_sum = 0.0
        self.peak = 0.0
        self.speech_frames = 0
        return result


class Pcm24To16Resampler:
    """Stateful 24 kHz → 16 kHz PCM16 linear converter.

    The implementation deliberately avoids the deprecated ``audioop`` module,
    so the project remains compatible with Python versions after 3.12.  The
    3:2 ratio is exact and state is retained across Gemini packets.
    """

    def __init__(self) -> None:
        self._samples: list[int] = []
        self._position = 0.0
        self._step = GEMINI_OUTPUT_SAMPLE_RATE / TELEPHONY_SAMPLE_RATE  # 1.5

    def convert(self, pcm24: bytes) -> bytes:
        if pcm24:
            usable = pcm24[: len(pcm24) - (len(pcm24) % 2)]
            incoming = array("h")
            incoming.frombytes(usable)
            if sys.byteorder != "little":
                incoming.byteswap()
            self._samples.extend(incoming)
        if len(self._samples) < 2:
            return b""

        output = array("h")
        while self._position + 1.0 < len(self._samples):
            left = int(self._position)
            fraction = self._position - left
            sample = round(
                self._samples[left] * (1.0 - fraction)
                + self._samples[left + 1] * fraction
            )
            output.append(max(-32768, min(32767, sample)))
            self._position += self._step

        consumed = int(self._position)
        if consumed:
            self._samples = self._samples[consumed:]
            self._position -= consumed
        if sys.byteorder != "little":
            output.byteswap()
        return output.tobytes()

    def reset(self) -> None:
        self._samples.clear()
        self._position = 0.0


class AsyncWaveWriter:
    """Background WAV writer with a bounded, non-blocking media-side API."""

    def __init__(
        self,
        path: Path,
        *,
        sample_rate: int,
        max_queue: int = 4000,
    ) -> None:
        self.path = path
        self.sample_rate = sample_rate
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max_queue)
        self.dropped = 0
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(
            self._run(), name=f"wav-writer-{self.path.stem}"
        )

    def submit(self, pcm: bytes) -> None:
        if not pcm:
            return
        try:
            self.queue.put_nowait(pcm)
        except asyncio.QueueFull:
            self.dropped += 1

    async def close(self) -> None:
        if self._task is None:
            return
        await self.queue.put(None)
        await self._task
        self._task = None

    async def _run(self) -> None:
        with wave.open(str(self.path), "wb") as output:
            output.setnchannels(CHANNELS)
            output.setsampwidth(SAMPLE_WIDTH_BYTES)
            output.setframerate(self.sample_rate)
            while True:
                chunk = await self.queue.get()
                if chunk is None:
                    break
                # wave.writeframesraw itself is tiny for 20ms chunks. Yielding
                # after each write prevents this diagnostics task from starving
                # the media tasks even on a slow filesystem.
                output.writeframesraw(chunk)
                await asyncio.sleep(0)
            output.writeframes(b"")
