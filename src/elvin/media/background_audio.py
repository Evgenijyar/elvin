"""Isolated looping PCM background overlay for the Asterisk outbound leg."""

from __future__ import annotations

import asyncio
import sys
from array import array
from pathlib import Path


class LoopingBackgroundAudio:
    """Read-only 16 kHz mono PCM loop with an independent volume control.

    The object never receives caller audio and is never connected to Gemini.
    It only produces bytes for the final Asterisk outbound leg.
    """

    def __init__(self, pcm16: bytes, *, volume_percent: int) -> None:
        usable = pcm16[: len(pcm16) - (len(pcm16) % 2)]
        self._pcm16 = usable
        self.volume_percent = max(0, min(int(volume_percent), 100))
        self._position = 0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._pcm16) and self.volume_percent > 0

    @classmethod
    async def load(
        cls,
        path: Path | None,
        *,
        volume_percent: int,
    ) -> "LoopingBackgroundAudio | None":
        if path is None or volume_percent <= 0 or not path.is_file():
            return None
        pcm16 = await asyncio.to_thread(path.read_bytes)
        track = cls(pcm16, volume_percent=volume_percent)
        return track if track.enabled else None

    async def background_bytes(self, byte_count: int) -> bytes:
        """Return the next scaled loop segment with exactly ``byte_count`` bytes."""
        size = max(0, int(byte_count))
        size -= size % 2
        if size <= 0 or not self.enabled:
            return b""
        async with self._lock:
            raw = self._read_loop_unlocked(size)
        return _scale_pcm16(raw, self.volume_percent / 100.0)

    async def mix_with_voice(self, voice_pcm16: bytes) -> bytes:
        """Saturating mix preserving the exact voice packet length."""
        usable = voice_pcm16[: len(voice_pcm16) - (len(voice_pcm16) % 2)]
        if not usable or not self.enabled:
            return usable
        async with self._lock:
            background = self._read_loop_unlocked(len(usable))
        return _mix_pcm16(
            usable,
            background,
            background_gain=self.volume_percent / 100.0,
        )

    def _read_loop_unlocked(self, size: int) -> bytes:
        if not self._pcm16:
            return b""
        output = bytearray()
        while len(output) < size:
            remaining = len(self._pcm16) - self._position
            take = min(size - len(output), remaining)
            output.extend(self._pcm16[self._position : self._position + take])
            self._position += take
            if self._position >= len(self._pcm16):
                self._position = 0
        return bytes(output)


def _pcm16_array(pcm: bytes) -> array:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _to_little_endian_bytes(samples: array) -> bytes:
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def _scale_pcm16(pcm: bytes, gain: float) -> bytes:
    samples = _pcm16_array(pcm)
    for index, sample in enumerate(samples):
        samples[index] = max(-32768, min(32767, round(sample * gain)))
    return _to_little_endian_bytes(samples)


def _mix_pcm16(voice: bytes, background: bytes, *, background_gain: float) -> bytes:
    voice_samples = _pcm16_array(voice)
    background_samples = _pcm16_array(background)
    count = min(len(voice_samples), len(background_samples))
    for index in range(count):
        mixed = int(voice_samples[index]) + round(
            int(background_samples[index]) * background_gain
        )
        voice_samples[index] = max(-32768, min(32767, mixed))
    return _to_little_endian_bytes(voice_samples)
