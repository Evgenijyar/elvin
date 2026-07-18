"""Lifecycle for fully prepared voice sessions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from elvin.integrations.gemini_live import GeminiLiveSession
from elvin.media.audio import AsyncWaveWriter, TELEPHONY_SAMPLE_RATE
from elvin.media.turn_detector import LocalTurnDetector, TurnDetectorConfig
from elvin.observability.frame_trace import FrameTraceWriter
from elvin.observability.timeline import CallTimeline

logger = logging.getLogger("elvin.voice_runtime")


@dataclass(slots=True)
class VoiceCallIdentity:
    batch_id: str
    item_id: str
    assignment_id: str
    robot_id: str
    lead_id: int

    @property
    def call_id(self) -> str:
        return f"{self.batch_id}-{self.lead_id}"


class PreparedVoiceCall:
    """All expensive AI/VAD resources ready before LPTracker dialing."""

    def __init__(
        self,
        *,
        identity: VoiceCallIdentity,
        robot: dict[str, Any],
        api_key: str,
        recordings_dir: Path,
        trace_enabled: bool,
        turn_config: TurnDetectorConfig,
    ) -> None:
        self.identity = identity
        self.robot = robot
        self.call_dir = recordings_dir / identity.call_id
        self.timeline = CallTimeline(identity.call_id, self.call_dir)
        self.frame_trace = FrameTraceWriter(
            self.call_dir / "frames.ndjson.gz",
            enabled=trace_enabled,
        )
        self.caller_audio = AsyncWaveWriter(
            self.call_dir / "caller-in.wav",
            sample_rate=TELEPHONY_SAMPLE_RATE,
        )
        self.bot_audio = AsyncWaveWriter(
            self.call_dir / "bot-to-asterisk.wav",
            sample_rate=TELEPHONY_SAMPLE_RATE,
        )
        self.detector = LocalTurnDetector(
            config=turn_config,
            timeline=self.timeline,
            frame_trace=self.frame_trace,
        )
        self.gemini = GeminiLiveSession(
            api_key=api_key,
            robot=robot,
            timeline=self.timeline,
        )
        self.media_attached = False
        self._closed = False

    async def prepare(self) -> None:
        self.call_dir.mkdir(parents=True, exist_ok=True)
        await self.frame_trace.start()
        await self.caller_audio.start()
        await self.bot_audio.start()
        self.timeline.add(
            "VOICE_SESSION_PREPARING",
            lead_id=self.identity.lead_id,
            robot_id=self.identity.robot_id,
        )
        try:
            await self.gemini.connect()
        except Exception:
            await self.close()
            raise
        self.timeline.add("VOICE_SESSION_READY")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.gemini.close()
        finally:
            await self.detector.close()
            await self.caller_audio.close()
            await self.bot_audio.close()
            await self.frame_trace.close()
            self.timeline.add(
                "VOICE_SESSION_CLOSED",
                frame_trace_dropped=self.frame_trace.dropped,
                caller_wav_dropped=self.caller_audio.dropped,
                bot_wav_dropped=self.bot_audio.dropped,
            )
            await self.timeline.save()


class VoiceRuntime:
    def __init__(
        self,
        *,
        recordings_dir: Path,
        trace_enabled: bool = True,
        turn_config: TurnDetectorConfig | None = None,
    ) -> None:
        self.recordings_dir = recordings_dir
        self.trace_enabled = trace_enabled
        self.turn_config = turn_config or TurnDetectorConfig()

    async def prepare_call(
        self,
        *,
        identity: VoiceCallIdentity,
        robot: dict[str, Any],
        api_key: str,
    ) -> PreparedVoiceCall:
        call = PreparedVoiceCall(
            identity=identity,
            robot=robot,
            api_key=api_key,
            recordings_dir=self.recordings_dir,
            trace_enabled=self.trace_enabled,
            turn_config=self.turn_config,
        )
        await call.prepare()
        return call


def preload_voice_runtime() -> None:
    """Load optional packages and ONNX weights during application startup."""
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
        LocalSmartTurnAnalyzerV3,
    )
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat_asterisk import AsteriskWebsocketTransport  # noqa: F401

    vad = SileroVADAnalyzer(sample_rate=16_000)
    vad.set_sample_rate(16_000)
    turn = LocalSmartTurnAnalyzerV3(sample_rate=16_000)
    turn.set_sample_rate(16_000)
    logger.warning(
        "Pipecat voice runtime preloaded: Silero VAD + Smart Turn v3 + "
        "pipecat-asterisk"
    )
