"""Local speech start and semantic turn-end detection.

Every Asterisk PCM fragment is inspected.  Silero decides whether human speech
is present; Smart Turn decides whether a pause represents a finished thought.
Gemini's server-side VAD is not used.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from elvin.media.audio import (
    AdaptiveNoiseFloor,
    PcmLevelWindow,
    PreRollBuffer,
    measure_pcm16,
)
from elvin.observability.frame_trace import FrameTraceWriter
from elvin.observability.timeline import CallTimeline

logger = logging.getLogger("elvin.turn")


@dataclass(slots=True)
class TurnDetectorConfig:
    sample_rate: int = 16_000
    ptime_ms: int = 20
    vad_confidence: float = 0.45
    vad_start_secs: float = 0.08
    vad_stop_secs: float = 0.20
    vad_min_volume: float = 0.03
    pre_roll_ms: int = 240
    smart_turn_retry_ms: int = 350
    force_end_silence_ms: int = 1_400
    level_log_interval_seconds: float = 1.0


@dataclass(slots=True)
class TurnDecision:
    frame_sequence: int
    audio_to_gemini: bytes = b""
    speech_started: bool = False
    speech_ended: bool = False
    interrupted_bot: bool = False
    vad_state: str = "QUIET"
    smart_turn_state: str | None = None
    levels: dict[str, float] | None = None


class LocalTurnDetector:
    """Stateful per-call detector using Pipecat's local ONNX analyzers."""

    def __init__(
        self,
        *,
        config: TurnDetectorConfig,
        timeline: CallTimeline,
        frame_trace: FrameTraceWriter,
    ) -> None:
        # Imports are intentionally lazy: the control-plane UI can run even
        # when the optional voice dependency image is not installed locally.
        from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
        from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
            LocalSmartTurnAnalyzerV3,
        )
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams, VADState

        self.EndOfTurnState = EndOfTurnState
        self.VADState = VADState
        self.config = config
        self.timeline = timeline
        self.frame_trace = frame_trace

        self.vad = SileroVADAnalyzer(
            sample_rate=config.sample_rate,
            params=VADParams(
                confidence=config.vad_confidence,
                start_secs=config.vad_start_secs,
                stop_secs=config.vad_stop_secs,
                min_volume=config.vad_min_volume,
            ),
        )
        self.vad.set_sample_rate(config.sample_rate)

        self.smart_turn = LocalSmartTurnAnalyzerV3(
            sample_rate=config.sample_rate,
            params=SmartTurnParams(
                stop_secs=config.force_end_silence_ms / 1000.0,
                pre_speech_ms=float(config.pre_roll_ms),
                max_duration_secs=8.0,
            ),
        )
        self.smart_turn.set_sample_rate(config.sample_rate)
        self.smart_turn.update_vad_start_secs(config.vad_start_secs)

        self.pre_roll = PreRollBuffer(
            milliseconds=config.pre_roll_ms,
            sample_rate=config.sample_rate,
        )
        self.noise = AdaptiveNoiseFloor()
        self.level_window = PcmLevelWindow(config.level_log_interval_seconds)
        self.sequence = 0
        self.previous_vad_state = VADState.QUIET
        self.turn_open = False
        self.bot_speaking = False
        self.silence_started_at: float | None = None
        self.last_smart_turn_check = 0.0

    def set_bot_speaking(self, value: bool) -> None:
        self.bot_speaking = value

    async def process(self, pcm: bytes) -> TurnDecision:
        self.sequence += 1
        now = time.monotonic()
        self.pre_roll.append(pcm)
        levels = measure_pcm16(pcm)

        vad_state = await self.vad.analyze_audio(pcm)
        speech_likely = vad_state in {
            self.VADState.STARTING,
            self.VADState.SPEAKING,
        }
        noise_dbfs = self.noise.update(
            levels.dbfs, speech_likely=speech_likely
        )
        snr_db = levels.dbfs - noise_dbfs

        smart_append_state = self.smart_turn.append_audio(
            pcm,
            is_speech=speech_likely,
        )

        decision = TurnDecision(
            frame_sequence=self.sequence,
            vad_state=vad_state.name,
            levels={
                "rms": levels.rms,
                "peak": levels.peak,
                "dbfs": levels.dbfs,
                "noise_dbfs": noise_dbfs,
                "snr_db": snr_db,
            },
        )

        # The start event is emitted only after Silero has accumulated enough
        # evidence.  The pre-roll then restores the audio that preceded the
        # decision, including the first consonant/syllable.
        if not self.turn_open and vad_state == self.VADState.SPEAKING:
            self.turn_open = True
            self.silence_started_at = None
            decision.speech_started = True
            decision.interrupted_bot = self.bot_speaking
            decision.audio_to_gemini = self.pre_roll.snapshot()
            self.timeline.add(
                "LOCAL_SPEECH_START",
                frame=self.sequence,
                dbfs=round(levels.dbfs, 2),
                noise_dbfs=round(noise_dbfs, 2),
                snr_db=round(snr_db, 2),
                pre_roll_ms=self.config.pre_roll_ms,
                interrupted_bot=self.bot_speaking,
            )
        elif self.turn_open:
            # Once a turn is open, preserve the continuous segment, including
            # short quiet frames.  Do not gate each 20ms fragment by volume.
            decision.audio_to_gemini = pcm

        if self.turn_open:
            if vad_state in {
                self.VADState.STARTING,
                self.VADState.SPEAKING,
            }:
                self.silence_started_at = None
            elif self.silence_started_at is None:
                self.silence_started_at = now

            should_check = False
            if (
                self.previous_vad_state == self.VADState.STOPPING
                and vad_state == self.VADState.QUIET
            ):
                should_check = True
            elif (
                vad_state == self.VADState.QUIET
                and self.silence_started_at is not None
                and (now - self.last_smart_turn_check) * 1000
                >= self.config.smart_turn_retry_ms
            ):
                should_check = True

            if smart_append_state == self.EndOfTurnState.COMPLETE:
                decision.smart_turn_state = "TIMEOUT_COMPLETE"
                self._close_turn(decision, reason="smart_turn_timeout")
            elif should_check:
                self.last_smart_turn_check = now
                state, metrics = await self.smart_turn.analyze_end_of_turn()
                decision.smart_turn_state = state.name
                probability = getattr(metrics, "probability", None)
                analysis_ms = getattr(metrics, "e2e_processing_time_ms", None)
                self.timeline.add(
                    "SMART_TURN_RESULT",
                    result=state.name,
                    probability=(
                        round(float(probability), 4)
                        if probability is not None
                        else None
                    ),
                    analysis_ms=(
                        round(float(analysis_ms), 2)
                        if analysis_ms is not None
                        else None
                    ),
                )
                if state == self.EndOfTurnState.COMPLETE:
                    self._close_turn(decision, reason="smart_turn_complete")
                elif self.silence_started_at is not None:
                    silence_ms = (now - self.silence_started_at) * 1000
                    if silence_ms >= self.config.force_end_silence_ms:
                        self.smart_turn.clear()
                        decision.smart_turn_state = "FORCED_COMPLETE"
                        self._close_turn(decision, reason="silence_guard")

        summary = self.level_window.add(
            levels,
            len(pcm),
            speech=speech_likely,
        )
        if summary is not None:
            logger.warning(
                "Asterisk PCM level: age=%.1fs frames=%s bytes=%s "
                "speech_frames=%s rms=%.5f peak=%.5f dbfs=%.1f "
                "noise_dbfs=%.1f snr_db=%.1f vad=%s turn=%s",
                summary["age"],
                summary["frames"],
                summary["bytes"],
                summary["speech_frames"],
                summary["rms"],
                summary["peak"],
                summary["dbfs"],
                noise_dbfs,
                snr_db,
                vad_state.name,
                "OPEN" if self.turn_open else "CLOSED",
            )

        self.frame_trace.submit(
            {
                "seq": self.sequence,
                "relative_ms": round(self.timeline.elapsed_ms(), 3),
                "bytes": len(pcm),
                "rms": round(levels.rms, 6),
                "peak": round(levels.peak, 6),
                "dbfs": round(levels.dbfs, 2),
                "noise_dbfs": round(noise_dbfs, 2),
                "snr_db": round(snr_db, 2),
                "vad_state": vad_state.name,
                "turn_open": self.turn_open,
                "forwarded": bool(decision.audio_to_gemini),
                "speech_started": decision.speech_started,
                "speech_ended": decision.speech_ended,
                "smart_turn": decision.smart_turn_state,
            }
        )
        self.previous_vad_state = vad_state
        return decision

    def _close_turn(self, decision: TurnDecision, *, reason: str) -> None:
        if not self.turn_open:
            return
        self.turn_open = False
        decision.speech_ended = True
        silence_ms = (
            round((time.monotonic() - self.silence_started_at) * 1000, 1)
            if self.silence_started_at is not None
            else 0.0
        )
        self.timeline.add(
            "LOCAL_SPEECH_END",
            frame=self.sequence,
            reason=reason,
            silence_ms=silence_ms,
        )
        self.silence_started_at = None
        self.last_smart_turn_check = 0.0

    async def close(self) -> None:
        await self.vad.cleanup()
        await self.smart_turn.cleanup()
