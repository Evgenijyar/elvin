"""Bidirectional Asterisk chan_websocket ↔ prepared Gemini bridge."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from elvin.media.audio import Pcm24To16Resampler, PlaybackEchoGuard
from elvin.media.conversation_audio import (
    apply_gain_db,
    apply_gain_percent,
    apply_gain_ramp_db,
    duration_ms,
)
from elvin.media.runtime import PreparedVoiceCall
from elvin.services.conversation_effects import any_effect_enabled

logger = logging.getLogger("elvin.asterisk")


@dataclass(slots=True)
class AsteriskMediaInfo:
    format: str = "slin16"
    optimal_frame_size: int = 640
    ptime: int = 20
    channel_id: str = ""
    channel: str = ""


@dataclass(slots=True)
class InterruptionCandidate:
    director_generation: int
    interrupted_actor_generation: int
    started_at: float
    audio: bytearray = field(default_factory=bytearray)
    speech_ended_at: float | None = None
    speech_ended: asyncio.Event = field(default_factory=asyncio.Event)
    committed: bool = False
    resolution: str = "PENDING"
    resume_policy: str = "DISCARD"
    director_decision: Any | None = None
    media_paused: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    def elapsed_ms(self, now: float) -> float:
        end = self.speech_ended_at if self.speech_ended_at is not None else now
        return max(0.0, (end - self.started_at) * 1000.0)


@dataclass(slots=True)
class BackchannelOpportunity:
    generation: int
    created_at: float
    confirmed: bool = False
    rejected: bool = False
    decision: asyncio.Event = field(default_factory=asyncio.Event)


class AsteriskProtocol:
    def __init__(self, websocket: WebSocket, call: PreparedVoiceCall) -> None:
        self.websocket = websocket
        self.call = call
        self.json_mode = True
        self.info = AsteriskMediaInfo()
        self.send_lock = asyncio.Lock()
        self.media_allowed = asyncio.Event()
        self.media_allowed.set()
        self.media_started = asyncio.Event()
        self.mark_waiters: dict[str, asyncio.Event] = {}

    def parse_text(self, text: str) -> dict[str, Any]:
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                self.json_mode = True
                return payload
        except json.JSONDecodeError:
            pass

        self.json_mode = False
        pieces = text.strip().split()
        if not pieces:
            return {"event": "UNKNOWN", "raw": text}
        payload: dict[str, Any] = {"event": pieces[0]}
        for piece in pieces[1:]:
            if ":" in piece:
                key, value = piece.split(":", 1)
                payload[key] = value
        return payload

    async def command(self, command: str, **parameters: Any) -> None:
        if self.json_mode:
            message = json.dumps(
                {"command": command, **parameters},
                separators=(",", ":"),
            )
        else:
            # Legacy plain-text commands use positional values, while JSON
            # uses named fields. Production is configured with f(json).
            suffix = " ".join(str(value) for value in parameters.values())
            message = command if not suffix else f"{command} {suffix}"
        async with self.send_lock:
            await self.websocket.send_text(message)

    async def send_media(
        self,
        pcm: bytes,
        *,
        generation: int | None = None,
    ) -> bool:
        if not pcm:
            return False
        # Asterisk's underlying websocket layer rejects messages > 65500.
        for offset in range(0, len(pcm), 64_000):
            chunk = pcm[offset : offset + 64_000]
            await self.media_allowed.wait()
            # A barge-in can happen while MEDIA_XOFF is active. Re-check the
            # generation after the wait so stale audio is never released into
            # the channel after FLUSH_MEDIA.
            if generation is not None and generation != self.call.gemini.generation:
                return False
            async with self.send_lock:
                if generation is not None and generation != self.call.gemini.generation:
                    return False
                await self.websocket.send_bytes(chunk)
        return True

    async def mark(self, correlation_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self.mark_waiters[correlation_id] = event
        await self.command("MARK_MEDIA", correlation_id=correlation_id)
        return event

    def handle_event(self, event: dict[str, Any]) -> None:
        name = str(event.get("event") or event.get("type") or "UNKNOWN")
        if name == "MEDIA_START":
            self.info = AsteriskMediaInfo(
                format=str(event.get("format") or "slin16"),
                optimal_frame_size=int(event.get("optimal_frame_size") or 640),
                ptime=int(event.get("ptime") or 20),
                channel_id=str(event.get("channel_id") or ""),
                channel=str(event.get("channel") or ""),
            )
            self.media_started.set()
            self.call.timeline.add(
                "ASTERISK_MEDIA_START",
                format=self.info.format,
                frame_bytes=self.info.optimal_frame_size,
                ptime_ms=self.info.ptime,
                channel_id=self.info.channel_id,
            )
        elif name == "MEDIA_XOFF":
            self.media_allowed.clear()
            self.call.timeline.add("ASTERISK_MEDIA_XOFF")
        elif name == "MEDIA_XON":
            self.media_allowed.set()
            self.call.timeline.add("ASTERISK_MEDIA_XON")
        elif name == "MEDIA_MARK_PROCESSED":
            correlation_id = str(event.get("correlation_id") or "")
            waiter = self.mark_waiters.pop(correlation_id, None)
            if waiter is not None:
                waiter.set()
        elif name == "DTMF_END":
            self.call.timeline.add("ASTERISK_DTMF", digit=str(event.get("digit") or ""))
        elif name in {"HANGUP", "MEDIA_END"}:
            self.call.timeline.add("ASTERISK_HANGUP_EVENT", event=name)
        elif name == "STATUS":
            self.call.timeline.add(
                "ASTERISK_STATUS",
                queue_length=event.get("queue_length"),
                queue_full=event.get("queue_full"),
            )


class AsteriskGeminiBridge:
    def __init__(self, websocket: WebSocket, call: PreparedVoiceCall) -> None:
        self.websocket = websocket
        self.call = call
        self.protocol = AsteriskProtocol(websocket, call)
        self.resampler = Pcm24To16Resampler()
        # Optional office/background sound is an isolated final-leg overlay.
        # It never enters caller input, VAD, echo correlation or Gemini.
        self.background_audio = call.background_audio
        self.effects = call.audio_effects
        self.effects_config = call.effects_config
        self.director = call.director
        self.effects_active = any_effect_enabled(self.effects_config)
        self._director_degraded = False
        self.director_resampler = Pcm24To16Resampler()
        self._voice_submission_active = asyncio.Event()
        self._interruption_candidate: InterruptionCandidate | None = None
        self._interruption_lock = asyncio.Lock()
        self._duck_current_db = 0.0
        self._duck_target_db = 0.0
        self._duck_transition_remaining_ms = 0.0
        self._response_delay_applied: set[int] = set()
        self._filler_requested: set[int] = set()
        self._actor_turn_ended_at: dict[int, float] = {}
        self._micro_pause_styles: dict[int, str] = {}
        self._micro_pause_confidence: dict[int, float] = {}
        self._latency_filler_tasks: dict[int, asyncio.Task[None]] = {}
        self._director_output_buffer = bytearray()
        self._director_output_lock = asyncio.Lock()
        self._director_audio_allowed_ids: set[int] = set()
        self._director_aux_active = asyncio.Event()
        self._director_audio_rejected_ids: set[int] = set()
        self._active_filler_actor_generation: int | None = None
        self._fillers_played_by_generation: dict[int, int] = {}
        self._director_generation_for_actor: dict[int, int] = {}
        self._current_director_generation: int | None = None
        self._last_director_generation_for_turn: int | None = None
        self._director_quiet_started_at: float | None = None
        self._director_segment_buffer = bytearray()
        self._director_reopen_task: asyncio.Task[None] | None = None
        self._backchannel_opportunities: dict[int, BackchannelOpportunity] = {}
        self._pending_turn_director_generations: deque[int | None] = deque()
        self._user_speech_started_at = 0.0
        self._backchannels_this_turn = 0
        self._last_backchannel_at = 0.0
        # chan_websocket does not perform acoustic echo cancellation. Keep a
        # short copy of playback submitted to Asterisk so the local VAD can
        # suppress only high-confidence far-end echo while preserving true
        # caller barge-in.
        self.echo_guard = PlaybackEchoGuard()
        self._closed = False
        self._first_input = True
        self._first_output_generation: set[int] = set()
        # chan_websocket re-times media most reliably when every binary frame
        # is an exact multiple of MEDIA_START.optimal_frame_size. Gemini
        # packets are arbitrary chunks, so retain only the small remainder
        # between packets and flush it with silence at turn end.
        self._output_buffer = bytearray()
        self._output_buffer_lock = asyncio.Lock()
        self._last_echo_event_at = 0.0
        self._last_output_submission_at = 0.0
        self._last_output_submission_generation: int | None = None
        self._active_activity_started = False
        # If the caller speaks while Gemini is still preparing a response,
        # starting another Live activity immediately can cancel both turns.
        # Keep the completed caller utterance here and submit it only after
        # the current server turn is complete.
        self._pending_turn_audio: bytearray | None = None
        self._pending_turns: deque[bytes] = deque()
        self._pending_turn_drain_task: asyncio.Task[None] | None = None
        self._pending_drain_active = False
        self._pending_drain_audio: bytes | None = None

    async def run(self) -> str:
        self.call.media_attached = True
        self.call.timeline.add("ASTERISK_WEBSOCKET_ATTACHED")
        input_task = asyncio.create_task(
            self._input_loop(), name=f"asterisk-input-{self.call.identity.call_id}"
        )
        output_task = asyncio.create_task(
            self._output_loop(), name=f"asterisk-output-{self.call.identity.call_id}"
        )
        monitor_task = asyncio.create_task(
            self._playback_monitor(),
            name=f"asterisk-playback-monitor-{self.call.identity.call_id}",
        )
        tasks = {input_task, output_task, monitor_task}
        if self.director is not None:
            director_task = asyncio.create_task(
                self._director_output_loop(),
                name=f"asterisk-director-output-{self.call.identity.call_id}",
            )
            tasks.add(director_task)
        if self.background_audio is not None:
            background_task = asyncio.create_task(
                self._background_loop(),
                name=f"asterisk-background-{self.call.identity.call_id}",
            )
            tasks.add(background_task)
        result = "caller_hangup"
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                exception = task.exception()
                if exception is not None:
                    raise exception
            if input_task in done:
                result = input_task.result()
            else:
                result = "media_task_finished"
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return result
        finally:
            self._closed = True
            for filler_task in self._latency_filler_tasks.values():
                filler_task.cancel()
            if self._latency_filler_tasks:
                await asyncio.gather(
                    *self._latency_filler_tasks.values(),
                    return_exceptions=True,
                )
            self._latency_filler_tasks.clear()
            candidate = self._interruption_candidate
            if candidate is not None and candidate.task is not None:
                candidate.task.cancel()
                await asyncio.gather(candidate.task, return_exceptions=True)
            if self._director_reopen_task is not None:
                self._director_reopen_task.cancel()
                await asyncio.gather(self._director_reopen_task, return_exceptions=True)
                self._director_reopen_task = None
            if self._pending_turn_drain_task is not None:
                self._pending_turn_drain_task.cancel()
                await asyncio.gather(
                    self._pending_turn_drain_task,
                    return_exceptions=True,
                )
                self._pending_turn_drain_task = None
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._director_aux_active.clear()
            self._director_output_buffer.clear()
            for opportunity in self._backchannel_opportunities.values():
                opportunity.rejected = True
                opportunity.decision.set()
            self._backchannel_opportunities.clear()
            self.call.detector.set_bot_speaking(False)
            self.call.timeline.add("ASTERISK_BRIDGE_CLOSED", result=result)

    async def _input_loop(self) -> str:
        try:
            while True:
                message = await self.websocket.receive()
                message_type = message.get("type")
                if message_type == "websocket.disconnect":
                    return "caller_hangup"

                text = message.get("text")
                if text is not None:
                    event = self.protocol.parse_text(text)
                    self.protocol.handle_event(event)
                    event_name = str(event.get("event") or event.get("type") or "")
                    if event_name == "MEDIA_START":
                        if self.protocol.info.format != "slin16":
                            raise RuntimeError(
                                "Asterisk media format must be slin16; got "
                                f"{self.protocol.info.format}"
                            )
                        logger.warning(
                            "Asterisk PCM input started: sample_rate=16000 "
                            "channels=1 frame_bytes=%s ptime=%sms",
                            self.protocol.info.optimal_frame_size,
                            self.protocol.info.ptime,
                        )
                        self.echo_guard.frame_bytes = max(
                            2, self.protocol.info.optimal_frame_size
                        )
                    elif event_name in {"HANGUP", "MEDIA_END"}:
                        return "asterisk_hangup"
                    continue

                pcm = message.get("bytes")
                if pcm is None:
                    continue
                if self._first_input:
                    self._first_input = False
                    self.call.timeline.add(
                        "ASTERISK_FIRST_PCM",
                        bytes=len(pcm),
                    )
                self.call.caller_audio.submit(pcm)
                echo_suppressed = self.echo_guard.is_echo(
                    pcm,
                    active=(
                        self._director_aux_active.is_set()
                        or (
                            self.call.detector.bot_speaking
                            and not self.call.detector.turn_open
                        )
                    ),
                )
                if (
                    echo_suppressed
                    and asyncio.get_running_loop().time() - self._last_echo_event_at
                    >= 0.25
                ):
                    self._last_echo_event_at = asyncio.get_running_loop().time()
                    self.call.timeline.add(
                        "PLAYBACK_ECHO_SUPPRESSED",
                        bytes=len(pcm),
                    )
                decision = await self.call.detector.process(
                    pcm,
                    echo_suppressed=echo_suppressed,
                )

                if decision.speech_started:
                    loop = asyncio.get_running_loop()
                    self._user_speech_started_at = loop.time()
                    self._backchannels_this_turn = 0
                    response_open_generation = getattr(
                        self.call.gemini,
                        "response_open_generation",
                        None,
                    )
                    response_audio_active = (
                        decision.interrupted_bot
                        or self.call.detector.bot_speaking
                        or self.call.gemini.bot_audio_active.is_set()
                    )
                    director_generation: int | None = None
                    if self.director is not None:
                        director_generation = await self._start_director_activity(
                            actor_speaking=response_audio_active
                        )
                    self._current_director_generation = director_generation
                    self._last_director_generation_for_turn = director_generation
                    self._director_quiet_started_at = None
                    self._director_segment_buffer.clear()

                    pending_prefix = b""
                    if response_audio_active and self._pending_drain_active:
                        await self._cancel_pending_drain()
                    if response_audio_active:
                        self._discard_pending_turns()
                    if response_audio_active and self._pending_turn_audio is not None:
                        pending_prefix = bytes(self._pending_turn_audio)
                        self._pending_turn_audio = None

                    if response_audio_active and self._soft_interruption_enabled():
                        candidate = InterruptionCandidate(
                            director_generation=director_generation or 0,
                            interrupted_actor_generation=self.call.gemini.generation,
                            started_at=loop.time(),
                        )
                        if pending_prefix:
                            candidate.audio.extend(pending_prefix)
                        self._interruption_candidate = candidate
                        natural = self.effects_config.get("natural_interruption", {})
                        use_natural_envelope = bool(
                            natural.get("enabled")
                            or self._effect_enabled("natural_cut")
                        )
                        self._set_duck(
                            (
                                float(natural.get("duck_db", -9))
                                if use_natural_envelope
                                else 0.0
                            ),
                            int(natural.get("duck_attack_ms", 55)),
                        )
                        try:
                            await self.protocol.command("PAUSE_MEDIA")
                            candidate.media_paused = True
                            self.call.timeline.add("SOFT_INTERRUPTION_MEDIA_PAUSED")
                        except Exception as exc:
                            # Older Asterisk releases did not expose the pause
                            # command. Continue with the bounded FLUSH fallback
                            # instead of failing the call.
                            self.call.timeline.add(
                                "SOFT_INTERRUPTION_PAUSE_UNAVAILABLE",
                                error=f"{type(exc).__name__}: {exc}",
                            )
                        candidate.task = asyncio.create_task(
                            self._resolve_interruption_candidate(candidate),
                            name=(
                                f"asterisk-interruption-{self.call.identity.call_id}"
                            ),
                        )
                        self.call.timeline.add(
                            "SOFT_INTERRUPTION_STARTED",
                            director_generation=director_generation,
                            actor_generation=self.call.gemini.generation,
                        )
                    else:
                        # Stable v1.1.0 behaviour is retained exactly when the
                        # soft-interruption effects are disabled.
                        self.resampler.reset()
                        await self._discard_output_buffer()
                        if (
                            (
                                response_open_generation is not None
                                and not response_audio_active
                            )
                            or (
                                self._pending_drain_active and not response_audio_active
                            )
                            or (
                                self._active_activity_started
                                and not response_audio_active
                            )
                        ):
                            self._pending_turn_audio = bytearray()
                            self.call.timeline.add(
                                "PENDING_TURN_STARTED",
                                waiting_for_generation=response_open_generation,
                            )
                        elif response_audio_active:
                            cleared = self.call.gemini.clear_output_nowait()
                            actor_generation = await self.call.gemini.start_activity()
                            if director_generation is not None:
                                self._director_generation_for_actor[
                                    actor_generation
                                ] = director_generation
                            await self.protocol.command("FLUSH_MEDIA")
                            self.echo_guard.clear()
                            self._last_output_submission_at = 0.0
                            self.call.detector.set_bot_speaking(False)
                            self._active_activity_started = True
                            self.call.timeline.add(
                                "BARGE_IN_FLUSH",
                                cleared_gemini_packets=cleared,
                                reason=(
                                    "bot_audio"
                                    if decision.interrupted_bot
                                    else "pending_response"
                                ),
                            )
                            if pending_prefix:
                                await self._send_audio_to_gemini(pending_prefix)
                        else:
                            actor_generation = await self.call.gemini.start_activity()
                            if director_generation is not None:
                                self._director_generation_for_actor[
                                    actor_generation
                                ] = director_generation
                            self._active_activity_started = True
                            if pending_prefix:
                                await self._send_audio_to_gemini(pending_prefix)

                if self.director is not None:
                    await self._handle_director_segmentation(decision)

                if decision.audio_to_gemini:
                    if self.director is not None:
                        if self._current_director_generation is not None:
                            await self._send_audio_to_director(decision.audio_to_gemini)
                        elif self.call.detector.turn_open:
                            self._director_segment_buffer.extend(
                                decision.audio_to_gemini
                            )
                    candidate = self._interruption_candidate
                    if candidate is not None and not candidate.done.is_set():
                        candidate.audio.extend(decision.audio_to_gemini)
                    elif self._pending_turn_audio is not None:
                        self._pending_turn_audio.extend(decision.audio_to_gemini)
                    elif self._active_activity_started:
                        await self._send_audio_to_gemini(decision.audio_to_gemini)

                if decision.speech_ended:
                    if self._director_reopen_task is not None:
                        await asyncio.gather(
                            self._director_reopen_task, return_exceptions=True
                        )
                        self._director_reopen_task = None
                    director_generation = (
                        self._current_director_generation
                        or self._last_director_generation_for_turn
                    )
                    if self.director is not None and self.director.activity_open:
                        await self._end_director_activity()
                        director_generation = self.director.generation
                        self._last_director_generation_for_turn = director_generation
                    candidate = self._interruption_candidate
                    if candidate is not None:
                        candidate.speech_ended_at = asyncio.get_running_loop().time()
                        candidate.speech_ended.set()
                        if not candidate.done.is_set():
                            try:
                                await asyncio.wait_for(
                                    candidate.done.wait(), timeout=2.5
                                )
                            except TimeoutError:
                                await self._commit_interruption_candidate(
                                    candidate, reason="resolution_timeout"
                                )
                        if candidate.committed and self._active_activity_started:
                            if self._effect_enabled("interruption_resume"):
                                await self._resolve_resume_policy(candidate)
                                resume = self.effects_config.get(
                                    "interruption_resume", {}
                                )
                                resume_delay_ms = int(
                                    resume.get("resume_delay_ms", 180)
                                )
                                if resume_delay_ms > 0:
                                    await asyncio.sleep(resume_delay_ms / 1000.0)
                                previous_text = (
                                    self.call.gemini.transcript_for_generation(
                                        candidate.interrupted_actor_generation
                                    )
                                )
                                max_chars = int(resume.get("max_context_chars", 1200))
                                context = (
                                    previous_text[-max_chars:] if previous_text else ""
                                )
                                if candidate.resume_policy == "RESUME":
                                    await self.call.gemini.send_context_hint(
                                        "Перебивание было коротким подтверждением. "
                                        "Если смысл не изменился, продолжи с ближайшей "
                                        "смысловой границы без повторения начала. "
                                        + (
                                            "Незавершённая реплика: " + context
                                            if context
                                            else ""
                                        )
                                    )
                                elif candidate.resume_policy == "REFORMULATE":
                                    await self.call.gemini.send_context_hint(
                                        "Собеседник перебил, потому что прежняя формулировка "
                                        "не подошла. Ответь на его последнюю реплику заново, "
                                        "короче и проще. "
                                        + (
                                            "Прежняя реплика: " + context
                                            if context
                                            else ""
                                        )
                                    )
                            actor_generation = self.call.gemini.generation
                            await self.call.gemini.end_activity()
                            self._mark_actor_turn_ended(
                                actor_generation, director_generation
                            )
                            self._active_activity_started = False
                        if self._interruption_candidate is candidate:
                            self._interruption_candidate = None
                    elif self._pending_turn_audio is not None:
                        pending_audio = bytes(self._pending_turn_audio)
                        self._pending_turn_audio = None
                        if pending_audio:
                            self._pending_turns.append(pending_audio)
                            self._pending_turn_director_generations.append(
                                director_generation
                            )
                            self.call.timeline.add(
                                "PENDING_TURN_QUEUED",
                                bytes=len(pending_audio),
                                queue_size=len(self._pending_turns),
                            )
                            self._schedule_pending_turn_drain()
                    elif self._active_activity_started:
                        actor_generation = self.call.gemini.generation
                        if director_generation is not None:
                            self._director_generation_for_actor[actor_generation] = (
                                director_generation
                            )
                        await self.call.gemini.end_activity()
                        self._mark_actor_turn_ended(
                            actor_generation, director_generation
                        )
                        self._active_activity_started = False
                    self._current_director_generation = None
                    self._last_director_generation_for_turn = None
                    self._director_quiet_started_at = None
                    self._director_segment_buffer.clear()
        except WebSocketDisconnect:
            return "caller_hangup"

    async def _start_director_activity(self, *, actor_speaking: bool) -> int | None:
        director = self.director
        if director is None or self._director_degraded:
            return None
        try:
            return await director.start_activity(actor_speaking=actor_speaking)
        except Exception as exc:
            self._degrade_director(exc, operation="start_activity")
            return None

    async def _end_director_activity(self) -> None:
        director = self.director
        if director is None or self._director_degraded:
            return
        try:
            await director.end_activity()
        except Exception as exc:
            self._degrade_director(exc, operation="end_activity")

    def _degrade_director(self, error: BaseException, *, operation: str) -> None:
        if self._director_degraded:
            return
        self._director_degraded = True
        self.call.timeline.add(
            "DIRECTOR_DEGRADED",
            operation=operation,
            error=f"{type(error).__name__}: {error}",
        )
        self._director_aux_active.clear()
        self._director_output_buffer.clear()
        self._reject_backchannel_opportunities(reason="director_degraded")
        logger.error(
            "Gemini Director degraded during %s; Actor continues: %s: %s",
            operation,
            type(error).__name__,
            error,
        )

    async def _handle_director_segmentation(self, decision: Any) -> None:
        director = self.director
        if director is None or not self._effect_enabled("listener_backchannels"):
            return
        if self._interruption_candidate is not None:
            return
        now = asyncio.get_running_loop().time()
        speaking = decision.vad_state in {"STARTING", "SPEAKING"}
        if speaking:
            self._director_quiet_started_at = None
            opportunity = self._latest_backchannel_opportunity()
            if opportunity is not None and not opportunity.decision.is_set():
                # A renewed SPEAKING state is the missing causal evidence that
                # the previous 220 ms pause was actually mid-turn.  Only now
                # may Director audio be released; a final pause is rejected.
                opportunity.confirmed = True
                opportunity.decision.set()
                self._director_segment_buffer.clear()
                self.call.timeline.add(
                    "DIRECTOR_BACKCHANNEL_CONFIRMED",
                    generation=opportunity.generation,
                    confirmation_ms=round((now - opportunity.created_at) * 1000.0),
                )
            if (
                self.call.detector.turn_open
                and self._current_director_generation is None
                and self._director_reopen_task is None
            ):
                generation = await self._start_director_activity(actor_speaking=False)
                if generation is None:
                    return
                self._current_director_generation = generation
                self._last_director_generation_for_turn = generation
                if self._director_segment_buffer:
                    buffered = bytes(self._director_segment_buffer)
                    self._director_segment_buffer.clear()
                    await self._send_audio_to_director(buffered)
            return
        if decision.speech_ended:
            self._reject_backchannel_opportunities(reason="caller_turn_ended")
            return
        if decision.vad_state != "QUIET":
            return
        if self._current_director_generation is None or not director.activity_open:
            return
        config = self.effects_config.get("listener_backchannels", {})
        spoken_ms = max(0.0, (now - self._user_speech_started_at) * 1000.0)
        if spoken_ms < int(config.get("min_user_speech_ms", 3200)):
            return
        if self._backchannels_this_turn >= int(config.get("max_per_turn", 1)):
            return
        if (
            now - self._last_backchannel_at
            < int(config.get("min_interval_ms", 7000)) / 1000.0
        ):
            return
        if self._director_quiet_started_at is None:
            self._director_quiet_started_at = now
            return
        quiet_ms = (now - self._director_quiet_started_at) * 1000.0
        if quiet_ms < int(config.get("opportunity_silence_ms", 220)):
            return
        generation = self._current_director_generation
        self._backchannel_opportunities[generation] = BackchannelOpportunity(
            generation=generation,
            created_at=now,
        )
        try:
            await director.mark_midturn_pause()
        except Exception as exc:
            self._reject_backchannel_opportunities(reason="director_mark_failed")
            self._degrade_director(exc, operation="mark_midturn_pause")
            return
        await self._end_director_activity()
        self._current_director_generation = None
        self._director_quiet_started_at = None
        self._director_reopen_task = asyncio.create_task(
            self._reopen_director_after_midturn(generation),
            name=(f"director-midturn-reopen-{self.call.identity.call_id}-{generation}"),
        )
        self.call.timeline.add(
            "DIRECTOR_MIDTURN_PAUSE",
            generation=generation,
            spoken_ms=round(spoken_ms),
            quiet_ms=round(quiet_ms),
        )

    async def _reopen_director_after_midturn(self, generation: int) -> None:
        director = self.director
        if director is None:
            return
        config = self.effects_config.get("listener_backchannels", {})
        timeout_ms = int(config.get("max_audio_ms", 750)) + 1200
        try:
            deadline = asyncio.get_running_loop().time() + timeout_ms / 1000.0
            completed = False
            while asyncio.get_running_loop().time() < deadline:
                opportunity = self._backchannel_opportunities.get(generation)
                if opportunity is not None and opportunity.rejected:
                    return
                if await director.wait_for_turn_complete(generation, 50):
                    completed = True
                    break
            if not completed:
                self._reject_backchannel_opportunities(
                    reason="director_turn_timeout",
                    generation=generation,
                )
                return
            opportunity = self._backchannel_opportunities.get(generation)
            if opportunity is None:
                return
            if not opportunity.decision.is_set():
                confirmation_ms = int(config.get("resume_confirmation_ms", 1100))
                try:
                    await asyncio.wait_for(
                        opportunity.decision.wait(),
                        timeout=confirmation_ms / 1000.0,
                    )
                except TimeoutError:
                    self._reject_backchannel_opportunities(
                        reason="no_caller_resume",
                        generation=generation,
                    )
                    return
            if not opportunity.confirmed or opportunity.rejected:
                return
            if self._closed:
                return
            buffered = bytes(self._director_segment_buffer)
            if not self.call.detector.turn_open:
                return
            next_generation = await self._start_director_activity(actor_speaking=False)
            if next_generation is None:
                return
            self._current_director_generation = next_generation
            self._last_director_generation_for_turn = next_generation
            if buffered:
                self._director_segment_buffer.clear()
                await self._send_audio_to_director(buffered)
            if not self.call.detector.turn_open:
                await self._end_director_activity()
                self._current_director_generation = None
        finally:
            current = asyncio.current_task()
            if self._director_reopen_task is current:
                self._director_reopen_task = None

    def _latest_backchannel_opportunity(
        self,
    ) -> BackchannelOpportunity | None:
        pending = [
            item
            for item in self._backchannel_opportunities.values()
            if not item.confirmed and not item.rejected
        ]
        return max(pending, key=lambda item: item.generation, default=None)

    def _reject_backchannel_opportunities(
        self, *, reason: str, generation: int | None = None
    ) -> None:
        for item_generation, opportunity in self._backchannel_opportunities.items():
            if generation is not None and item_generation != generation:
                continue
            if opportunity.confirmed or opportunity.rejected:
                continue
            opportunity.rejected = True
            opportunity.decision.set()
            self.call.timeline.add(
                "DIRECTOR_BACKCHANNEL_REJECTED",
                generation=item_generation,
                reason=reason,
            )

    def _effect_enabled(self, key: str) -> bool:
        return bool(self.effects_config.get(key, {}).get("enabled"))

    def _soft_interruption_enabled(self) -> bool:
        return (
            self._effect_enabled("natural_interruption")
            or self._effect_enabled("natural_cut")
            or (
                self.director is not None
                and (
                    self._effect_enabled("semantic_interruption")
                    or self._effect_enabled("interruption_resume")
                )
            )
        )

    def _set_duck(self, target_db: float, transition_ms: int) -> None:
        self._duck_target_db = min(0.0, float(target_db))
        self._duck_transition_remaining_ms = max(0.0, float(transition_ms))
        if self._duck_transition_remaining_ms <= 0:
            self._duck_current_db = self._duck_target_db

    def _apply_duck(self, pcm: bytes) -> bytes:
        if not pcm:
            return pcm
        if abs(self._duck_current_db) < 0.001 and abs(self._duck_target_db) < 0.001:
            return pcm
        packet_ms = max(0.1, duration_ms(pcm))
        if self._duck_transition_remaining_ms <= 0:
            self._duck_current_db = self._duck_target_db
            return apply_gain_ramp_db(pcm, self._duck_current_db, self._duck_current_db)
        fraction = min(1.0, packet_ms / self._duck_transition_remaining_ms)
        end_db = (
            self._duck_current_db
            + (self._duck_target_db - self._duck_current_db) * fraction
        )
        result = apply_gain_ramp_db(pcm, self._duck_current_db, end_db)
        self._duck_current_db = end_db
        self._duck_transition_remaining_ms = max(
            0.0, self._duck_transition_remaining_ms - packet_ms
        )
        if self._duck_transition_remaining_ms <= 0:
            self._duck_current_db = self._duck_target_db
        return result

    async def _send_audio_to_director(self, pcm16: bytes) -> None:
        director = self.director
        if director is None or not pcm16:
            return
        chunk_bytes = 1_280
        if self._director_degraded:
            return
        try:
            for offset in range(0, len(pcm16), chunk_bytes):
                await director.send_audio(pcm16[offset : offset + chunk_bytes])
        except Exception as exc:
            self._degrade_director(exc, operation="send_audio")

    def _mark_actor_turn_ended(
        self, actor_generation: int, director_generation: int | None
    ) -> None:
        ended_at = getattr(self, "_actor_turn_ended_at", None)
        if ended_at is None:
            ended_at = {}
            self._actor_turn_ended_at = ended_at
        ended_at[actor_generation] = asyncio.get_running_loop().time()
        if director_generation is not None:
            mapping = getattr(self, "_director_generation_for_actor", None)
            if mapping is None:
                mapping = {}
                self._director_generation_for_actor = mapping
            mapping[actor_generation] = director_generation
        director = getattr(self, "director", None)
        effects_config = getattr(self, "effects_config", {})
        latency = effects_config.get("latency_fillers", {})
        tasks = getattr(self, "_latency_filler_tasks", None)
        if tasks is None:
            tasks = {}
            self._latency_filler_tasks = tasks
        if (
            director is not None
            and bool(latency.get("enabled"))
            and actor_generation not in tasks
        ):
            task = asyncio.create_task(
                self._latency_filler_watch(actor_generation, director_generation),
                name=(
                    "asterisk-latency-filler-"
                    f"{self.call.identity.call_id}-{actor_generation}"
                ),
            )
            tasks[actor_generation] = task

    async def _latency_filler_watch(
        self, actor_generation: int, director_generation: int | None
    ) -> None:
        try:
            config = self.effects_config.get("latency_fillers", {})
            trigger_ms = int(config.get("trigger_ms", 1100))
            has_audio = await self.call.gemini.wait_for_first_audio(
                actor_generation, trigger_ms / 1000.0
            )
            if has_audio or self._closed or self.director is None:
                return
            if self.call.gemini.generation != actor_generation:
                return
            if self.call.detector.turn_open:
                return
            repeat_guard = int(config.get("repeat_guard_ms", 15000)) / 1000.0
            now = asyncio.get_running_loop().time()
            if now - self._last_backchannel_at < repeat_guard:
                return
            maximum = int(config.get("max_per_turn", 1))
            if self._fillers_played_by_generation.get(actor_generation, 0) >= maximum:
                return
            self._filler_requested.add(actor_generation)
            self._active_filler_actor_generation = actor_generation
            try:
                filler_generation = await self.director.request_latency_filler()
            except Exception as exc:
                self._degrade_director(exc, operation="request_latency_filler")
                return
            if filler_generation is None:
                self._filler_requested.discard(actor_generation)
                self._active_filler_actor_generation = None
                return
            self.call.timeline.add(
                "LATENCY_FILLER_REQUESTED",
                actor_generation=actor_generation,
                director_generation=filler_generation,
                trigger_ms=trigger_ms,
            )
        finally:
            self._latency_filler_tasks.pop(actor_generation, None)

    async def _resolve_interruption_candidate(
        self, candidate: InterruptionCandidate
    ) -> None:
        natural = self.effects_config.get("natural_interruption", {})
        semantic = self.effects_config.get("semantic_interruption", {})
        confirm_ms = int(natural.get("confirm_ms", 140))
        fallback_ms = int(natural.get("fallback_takeover_ms", 360))
        decision_timeout_ms = int(semantic.get("decision_timeout_ms", 320))
        semantic_enabled = bool(
            self.director is not None
            and candidate.director_generation
            and semantic.get("enabled")
        )
        try:
            await asyncio.sleep(max(0, confirm_ms) / 1000.0)
            if candidate.done.is_set():
                return
            if not semantic_enabled:
                # Local VAD has already supplied its own start confirmation.
                # A deterministic effect must not wait for a Director tool
                # that was never declared; this is the path used by the
                # standalone "soft interruption" switch.
                await self._commit_interruption_candidate(
                    candidate, reason="local_vad_confirmed"
                )
                return

            elapsed = candidate.elapsed_ms(asyncio.get_running_loop().time())
            remaining = max(0.0, fallback_ms - elapsed) / 1000.0
            if candidate.speech_ended_at is None and remaining > 0:
                try:
                    await asyncio.wait_for(
                        candidate.speech_ended.wait(), timeout=remaining
                    )
                except TimeoutError:
                    pass
            if candidate.speech_ended_at is None:
                # Semantic classification cannot complete before ActivityEnd.
                # Sustained speech is therefore treated as takeover locally,
                # keeping the physical barge-in bounded by fallback_ms.
                await self._commit_interruption_candidate(
                    candidate, reason="sustained_speech_takeover"
                )
                return

            decision = await self.director.wait_for_interruption(
                candidate.director_generation, decision_timeout_ms
            )
            if decision is not None:
                candidate.director_decision = decision
                intent = decision.intent
                confidence = decision.confidence
                self._set_resume_policy(candidate, decision)
                if intent == "TAKEOVER" and confidence >= float(
                    semantic.get("takeover_confidence", 0.78)
                ):
                    await self._commit_interruption_candidate(
                        candidate, reason="director_takeover"
                    )
                    return
                if intent in {"BACKCHANNEL", "NOISE"}:
                    threshold = float(
                        semantic.get(
                            "backchannel_confidence"
                            if intent == "BACKCHANNEL"
                            else "noise_confidence",
                            0.82,
                        )
                    )
                    within_backchannel_limit = (
                        intent != "BACKCHANNEL"
                        or candidate.elapsed_ms(candidate.speech_ended_at)
                        <= int(semantic.get("max_backchannel_ms", 650))
                    )
                    if confidence >= threshold and within_backchannel_limit:
                        await self._reject_interruption_candidate(
                            candidate, reason=intent.lower()
                        )
                        return
                if intent == "UNCERTAIN":
                    await asyncio.sleep(
                        max(0, int(semantic.get("uncertain_hold_ms", 180))) / 1000.0
                    )

            final_elapsed = candidate.elapsed_ms(
                candidate.speech_ended_at or asyncio.get_running_loop().time()
            )
            minimum_spoken_ms = max(80, min(confirm_ms, 160))
            if final_elapsed < minimum_spoken_ms:
                await self._reject_interruption_candidate(
                    candidate, reason="brief_unclassified_sound"
                )
            else:
                await self._commit_interruption_candidate(
                    candidate, reason="unclassified_spoken_interruption"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.call.timeline.add(
                "SOFT_INTERRUPTION_ERROR",
                error=f"{type(exc).__name__}: {exc}",
            )
            await self._commit_interruption_candidate(
                candidate, reason="director_error_fallback"
            )

    def _set_resume_policy(
        self, candidate: InterruptionCandidate, decision: Any
    ) -> None:
        resume = self.effects_config.get("interruption_resume", {})
        if not resume.get("enabled"):
            candidate.resume_policy = "DISCARD"
            return
        confidence = float(decision.confidence)
        policy = str(decision.resume_policy or "DISCARD").upper()
        if policy == "RESUME" and confidence >= float(
            resume.get("resume_confidence", 0.82)
        ):
            candidate.resume_policy = "RESUME"
        elif policy == "REFORMULATE" and confidence >= float(
            resume.get("reformulate_confidence", 0.76)
        ):
            candidate.resume_policy = "REFORMULATE"
        else:
            candidate.resume_policy = "DISCARD"

    async def _resolve_resume_policy(self, candidate: InterruptionCandidate) -> None:
        if self.director is None or not candidate.director_generation:
            return
        decision = candidate.director_decision
        if decision is None:
            timeout_ms = int(
                self.effects_config.get("interruption_resume", {}).get(
                    "decision_timeout_ms", 360
                )
            )
            decision = await self.director.wait_for_interruption(
                candidate.director_generation, timeout_ms
            )
            candidate.director_decision = decision
        if decision is not None:
            self._set_resume_policy(candidate, decision)

    async def _reject_interruption_candidate(
        self, candidate: InterruptionCandidate, *, reason: str
    ) -> None:
        async with self._interruption_lock:
            if candidate.done.is_set():
                return
            candidate.resolution = reason
            natural = self.effects_config.get("natural_interruption", {})
            self._set_duck(0.0, int(natural.get("recovery_ms", 120)))
            if candidate.media_paused:
                await self.protocol.command("CONTINUE_MEDIA")
                candidate.media_paused = False
            candidate.done.set()
            self.call.timeline.add(
                "SOFT_INTERRUPTION_REJECTED",
                reason=reason,
                duration_ms=round(
                    candidate.elapsed_ms(asyncio.get_running_loop().time()), 1
                ),
                bytes=len(candidate.audio),
            )

    async def _commit_interruption_candidate(
        self, candidate: InterruptionCandidate, *, reason: str
    ) -> None:
        async with self._interruption_lock:
            if candidate.done.is_set():
                return
            candidate.committed = True
            candidate.resolution = reason
            if self._pending_drain_active:
                await self._cancel_pending_drain()
            self._discard_pending_turns()
            cleared = self.call.gemini.clear_output_nowait()
            self.resampler.reset()
            await self._discard_output_buffer()
            actor_generation = await self.call.gemini.start_activity()
            if candidate.director_generation:
                self._director_generation_for_actor[actor_generation] = (
                    candidate.director_generation
                )
            await self.protocol.command("FLUSH_MEDIA")
            candidate.media_paused = False
            self.echo_guard.clear()
            release_tail = self.effects.release_tail()
            natural = self.effects_config.get("natural_interruption", {})
            if release_tail:
                release_tail = apply_gain_db(
                    release_tail, float(natural.get("duck_db", -9))
                )
            if release_tail:
                wire_tail = release_tail
                if self.background_audio is not None:
                    wire_tail = await self.background_audio.mix_with_voice(release_tail)
                self._voice_submission_active.set()
                try:
                    await self.protocol.send_media(wire_tail)
                finally:
                    self._voice_submission_active.clear()
                self.call.bot_audio.submit(wire_tail)
                self.echo_guard.note_playback(release_tail)
            self._last_output_submission_at = 0.0
            self.call.detector.set_bot_speaking(False)
            self._active_activity_started = True
            self._set_duck(0.0, 0)
            if candidate.audio:
                await self._send_audio_to_gemini(bytes(candidate.audio))
            candidate.done.set()
            self.call.timeline.add(
                "SOFT_INTERRUPTION_COMMITTED",
                reason=reason,
                cleared_gemini_packets=cleared,
                release_tail_ms=round(duration_ms(release_tail), 1),
                caller_bytes=len(candidate.audio),
                actor_generation=actor_generation,
            )

    def _schedule_pending_turn_drain(self) -> None:
        task = self._pending_turn_drain_task
        if task is None or task.done():
            self._pending_turn_drain_task = asyncio.create_task(
                self._drain_pending_turns(),
                name=f"asterisk-pending-turns-{self.call.identity.call_id}",
            )

    async def _cancel_pending_drain(self) -> None:
        task = self._pending_turn_drain_task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._pending_turn_drain_task is task:
            self._pending_turn_drain_task = None

    def _discard_pending_turns(self) -> None:
        dropped = len(self._pending_turns)
        self._pending_turns.clear()
        self._pending_turn_director_generations.clear()
        if dropped:
            self.call.timeline.add(
                "PENDING_TURNS_DROPPED",
                count=dropped,
                reason="caller_barge_in",
            )

    async def _send_audio_to_gemini(self, pcm16: bytes) -> None:
        """Send input in 20–40 ms chunks, including buffered pre-roll."""
        if not pcm16:
            return
        # 40 ms at 16 kHz, mono, signed 16-bit PCM.
        chunk_bytes = 1_280
        for offset in range(0, len(pcm16), chunk_bytes):
            await self.call.gemini.send_audio(pcm16[offset : offset + chunk_bytes])

    async def _drain_pending_turns(self) -> None:
        while self._pending_turns and not self._closed:
            pending_audio = self._pending_turns.popleft()
            pending_director_generations = getattr(
                self, "_pending_turn_director_generations", None
            )
            director_generation = (
                pending_director_generations.popleft()
                if pending_director_generations
                else None
            )
            self._pending_drain_active = True
            self._pending_drain_audio = pending_audio
            sent = False
            activity_started = False
            try:
                try:
                    await self.call.gemini.wait_for_response_idle(timeout=12.0)
                except TimeoutError:
                    # A response that never reaches turnComplete must not
                    # block every later caller turn forever. Advance the
                    # generation once, explicitly, and let Gemini's normal
                    # interruption protocol recover the session.
                    self.call.timeline.add(
                        "GEMINI_RESPONSE_WAIT_TIMEOUT",
                        generation=getattr(
                            self.call.gemini,
                            "response_open_generation",
                            None,
                        ),
                    )
                # Let the playback monitor finish a just-completed response
                # before the queued caller turn opens.
                deadline = asyncio.get_running_loop().time() + 2.0
                while (
                    self.call.detector.bot_speaking
                    and asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(0.02)
                if self.call.detector.bot_speaking:
                    cleared = self.call.gemini.clear_output_nowait()
                    await self.protocol.command("FLUSH_MEDIA")
                    self.echo_guard.clear()
                    self.call.detector.set_bot_speaking(False)
                    self.call.timeline.add(
                        "PENDING_TURN_FLUSH",
                        cleared_gemini_packets=cleared,
                    )
                self.resampler.reset()
                await self._discard_output_buffer()
                actor_generation = await self.call.gemini.start_activity()
                if director_generation is not None:
                    self._director_generation_for_actor[actor_generation] = (
                        director_generation
                    )
                activity_started = True
                await self._send_audio_to_gemini(pending_audio)
                await self.call.gemini.end_activity()
                self._mark_actor_turn_ended(actor_generation, director_generation)
                activity_started = False
                sent = True
                self.call.timeline.add(
                    "PENDING_TURN_SENT",
                    bytes=len(pending_audio),
                    remaining=len(self._pending_turns),
                )
            except asyncio.CancelledError:
                if not sent:
                    self._pending_turns.appendleft(pending_audio)
                    pending_director_generations = getattr(
                        self, "_pending_turn_director_generations", None
                    )
                    if pending_director_generations is not None:
                        pending_director_generations.appendleft(director_generation)
                if activity_started:
                    try:
                        await asyncio.shield(self.call.gemini.end_activity())
                    except Exception:
                        logger.debug(
                            "Unable to close cancelled pending activity",
                            exc_info=True,
                        )
                raise
            except Exception as exc:
                if not sent:
                    self._pending_turns.appendleft(pending_audio)
                    pending_director_generations = getattr(
                        self, "_pending_turn_director_generations", None
                    )
                    if pending_director_generations is not None:
                        pending_director_generations.appendleft(director_generation)
                if activity_started:
                    try:
                        await self.call.gemini.end_activity()
                    except Exception:
                        logger.debug(
                            "Unable to close failed pending activity",
                            exc_info=True,
                        )
                self.call.timeline.add(
                    "PENDING_TURN_ERROR",
                    error=f"{type(exc).__name__}: {exc}",
                    queue_size=len(self._pending_turns),
                )
                await asyncio.sleep(0.25)
            finally:
                self._pending_drain_audio = None
                self._pending_drain_active = False

    async def _prepare_actor_generation(self, generation: int) -> None:
        if generation in self._response_delay_applied:
            return
        if self._director_aux_active.is_set():
            maximum_ms = max(
                int(
                    self.effects_config.get("listener_backchannels", {}).get(
                        "max_audio_ms", 750
                    )
                ),
                int(
                    self.effects_config.get("latency_fillers", {}).get(
                        "max_audio_ms", 1200
                    )
                ),
            )
            deadline = asyncio.get_running_loop().time() + (maximum_ms + 250) / 1000.0
            while (
                self._director_aux_active.is_set()
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.01)
        if generation in self._response_delay_applied:
            return
        self._response_delay_applied.add(generation)
        self.effects.start_generation(generation)
        director_generation = self._director_generation_for_actor.get(generation)
        plan = None
        if (
            self.director is not None
            and not self._director_degraded
            and director_generation is not None
        ):
            delay_config = self.effects_config.get("adaptive_response_delay", {})
            wait_ms = int(delay_config.get("director_wait_ms", 240))
            if any(
                self._effect_enabled(key)
                for key in (
                    "adaptive_response_delay",
                    "pace_matching",
                    "micro_pauses",
                )
            ):
                plan = await self.director.wait_for_turn_plan(
                    director_generation, wait_ms
                )
        delay_config = self.effects_config.get("adaptive_response_delay", {})
        pace_config = self.effects_config.get("pace_matching", {})
        requested_delay: int | None = None
        if plan is not None:
            if plan.confidence >= float(pace_config.get("plan_confidence", 0.65)):
                self.effects.set_pace(plan.pace_percent)
            else:
                self.effects.set_pace(float(pace_config.get("default_percent", 100)))
            self._micro_pause_styles[generation] = plan.micro_pause_style
            self._micro_pause_confidence[generation] = plan.confidence
            if plan.confidence < float(delay_config.get("plan_confidence", 0.65)):
                requested_delay = int(delay_config.get("direct_answer_ms", 180))
            elif plan.user_state in {"THINKING", "UPSET"}:
                requested_delay = int(delay_config.get("thinking_pause_ms", 460))
            else:
                requested_delay = plan.response_delay_ms
        else:
            self.effects.set_pace(float(pace_config.get("default_percent", 100)))
            self._micro_pause_styles[generation] = "NONE"
            self._micro_pause_confidence[generation] = 0.0
            requested_delay = int(delay_config.get("direct_answer_ms", 180))

        self.effects.set_micro_pause_profile(
            self._micro_pause_styles.get(generation, "NONE"),
            self._micro_pause_confidence.get(generation, 0.0),
        )

        requested_seconds = self.effects.response_delay(requested_delay)
        ended_at = self._actor_turn_ended_at.get(generation)
        elapsed = (
            max(0.0, asyncio.get_running_loop().time() - ended_at)
            if ended_at is not None
            else 0.0
        )
        remaining = max(0.0, requested_seconds - elapsed)
        if remaining > 0:
            await asyncio.sleep(remaining)
        self.call.timeline.add(
            "ACTOR_EFFECT_PROFILE",
            generation=generation,
            director_generation=director_generation,
            response_delay_ms=round(requested_seconds * 1000),
            applied_wait_ms=round(remaining * 1000),
            pace_percent=round(self.effects.current_pace_percent, 2),
            micro_pause_style=self._micro_pause_styles.get(generation, "NONE"),
            director_plan=plan is not None,
            director_confidence=(
                round(float(plan.confidence), 3) if plan is not None else None
            ),
        )

    async def _output_loop(self) -> None:
        while True:
            queue_task = asyncio.create_task(self.call.gemini.output_audio.get())
            failure_task = asyncio.create_task(self.call.gemini.receive_failed.wait())
            done, pending = await asyncio.wait(
                {queue_task, failure_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if queue_task in done:
                packet = queue_task.result()
            else:
                queue_task.cancel()
                await asyncio.gather(queue_task, return_exceptions=True)
                error = self.call.gemini.receive_error
                raise RuntimeError(
                    "Gemini Live receiver stopped"
                    + (
                        f": {type(error).__name__}: {error}"
                        if error is not None
                        else ""
                    )
                ) from error
            try:
                if packet.generation != self.call.gemini.generation:
                    continue
                if self.effects_active:
                    await self._prepare_actor_generation(packet.generation)
                pcm16 = self.resampler.convert(packet.pcm24)
                if not pcm16:
                    continue
                if self.effects_active:
                    pcm16 = self.effects.process_actor_audio(pcm16)
                    pcm16 = self._apply_duck(pcm16)
                    inserted_pause_ms = self.effects.take_inserted_pause_ms()
                    if inserted_pause_ms:
                        self.call.timeline.add(
                            "ACTOR_MICRO_PAUSE",
                            generation=packet.generation,
                            duration_ms=inserted_pause_ms,
                            source="acoustic_gap",
                        )
                await self._send_output_audio(
                    pcm16,
                    generation=packet.generation,
                )
            finally:
                self.call.gemini.output_audio.task_done()

    async def _director_output_loop(self) -> None:
        director = self.director
        if director is None:
            return
        while True:
            queue_task = asyncio.create_task(director.output_audio.get())
            failure_task = asyncio.create_task(director.receive_failed.wait())
            done, pending = await asyncio.wait(
                {queue_task, failure_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if failure_task in done and director.receive_failed.is_set():
                error = director.receive_error or RuntimeError(
                    "Gemini Director receiver stopped"
                )
                self._degrade_director(error, operation="receive_loop")
                while not self._closed:
                    await asyncio.sleep(0.25)
                return
            packet = queue_task.result()
            try:
                if packet.utterance_id in self._director_audio_rejected_ids:
                    if packet.final:
                        self._director_audio_rejected_ids.discard(packet.utterance_id)
                        if packet.kind == "backchannel":
                            self._backchannel_opportunities.pop(packet.generation, None)
                    continue
                if packet.utterance_id not in self._director_audio_allowed_ids:
                    if (
                        packet.kind == "backchannel"
                        and not await self._await_backchannel_confirmation(
                            packet.generation
                        )
                    ):
                        self._director_audio_rejected_ids.add(packet.utterance_id)
                        continue
                    if not self._director_audio_allowed(packet):
                        self._director_audio_rejected_ids.add(packet.utterance_id)
                        continue
                    self._director_audio_allowed_ids.add(packet.utterance_id)
                    self._director_aux_active.set()
                    if packet.kind == "backchannel":
                        self._backchannels_this_turn += 1
                    elif packet.kind == "filler":
                        actor_generation = self._active_filler_actor_generation
                        if actor_generation is not None:
                            self._fillers_played_by_generation[actor_generation] = (
                                self._fillers_played_by_generation.get(
                                    actor_generation, 0
                                )
                                + 1
                            )
                if packet.pcm24:
                    pcm16 = self.director_resampler.convert(packet.pcm24)
                    if pcm16:
                        pcm16 = apply_gain_percent(pcm16, packet.volume_percent)
                        await self._send_auxiliary_audio(
                            pcm16, kind=packet.kind, flush=False
                        )
                if packet.final:
                    await self._send_auxiliary_audio(b"", kind=packet.kind, flush=True)
                    self.director_resampler.reset()
                    self._director_audio_allowed_ids.discard(packet.utterance_id)
                    self._director_aux_active.clear()
                    now = asyncio.get_running_loop().time()
                    self._last_backchannel_at = now
                    if packet.kind == "filler":
                        actor_generation = self._active_filler_actor_generation
                        if actor_generation is not None:
                            self._filler_requested.discard(actor_generation)
                        self._active_filler_actor_generation = None
                    elif packet.kind == "backchannel":
                        self._backchannel_opportunities.pop(packet.generation, None)
                    self.call.timeline.add(
                        "DIRECTOR_AUDIO_PLAYED",
                        kind=packet.kind,
                        phrase=packet.phrase,
                        volume_percent=packet.volume_percent,
                        utterance_id=packet.utterance_id,
                    )
            finally:
                director.output_audio.task_done()

    async def _await_backchannel_confirmation(self, generation: int) -> bool:
        opportunity = self._backchannel_opportunities.get(generation)
        if opportunity is None:
            return False
        if not opportunity.decision.is_set():
            timeout_ms = int(
                self.effects_config.get("listener_backchannels", {}).get(
                    "resume_confirmation_ms", 1100
                )
            )
            try:
                await asyncio.wait_for(
                    opportunity.decision.wait(), timeout=timeout_ms / 1000.0
                )
            except TimeoutError:
                self._reject_backchannel_opportunities(
                    reason="audio_confirmation_timeout",
                    generation=generation,
                )
        return opportunity.confirmed and not opportunity.rejected

    def _director_audio_allowed(self, packet: Any) -> bool:
        kind = str(packet.kind)
        now = asyncio.get_running_loop().time()
        if kind == "backchannel":
            config = self.effects_config.get("listener_backchannels", {})
            if not config.get("enabled") or not self.call.detector.turn_open:
                return False
            if (
                self.call.detector.bot_speaking
                or self.call.gemini.bot_audio_active.is_set()
            ):
                return False
            spoken_ms = max(0.0, (now - self._user_speech_started_at) * 1000.0)
            if spoken_ms < int(config.get("min_user_speech_ms", 3200)):
                return False
            if self._backchannels_this_turn >= int(config.get("max_per_turn", 1)):
                return False
            if (
                now - self._last_backchannel_at
                < int(config.get("min_interval_ms", 7000)) / 1000.0
            ):
                return False
            opportunity = self._backchannel_opportunities.get(int(packet.generation))
            if opportunity is None or not opportunity.confirmed or opportunity.rejected:
                return False
            candidate = self._interruption_candidate
            return candidate is None or not candidate.committed
        if kind == "filler":
            config = self.effects_config.get("latency_fillers", {})
            if not config.get("enabled") or self.call.detector.turn_open:
                return False
            if (
                self.call.detector.bot_speaking
                or self.call.gemini.bot_audio_active.is_set()
            ):
                return False
            actor_generation = self._active_filler_actor_generation
            if (
                actor_generation is None
                or actor_generation not in self._filler_requested
                or actor_generation != self.call.gemini.generation
            ):
                return False
            maximum = int(config.get("max_per_turn", 1))
            return self._fillers_played_by_generation.get(actor_generation, 0) < maximum
        return False

    async def _send_auxiliary_audio(
        self, pcm16: bytes, *, kind: str, flush: bool
    ) -> None:
        async with self._director_output_lock:
            if pcm16:
                self._director_output_buffer.extend(pcm16)
            frame_size = max(2, int(self.protocol.info.optimal_frame_size or 640))
            frame_size -= frame_size % 2
            send_size = len(self._director_output_buffer) - (
                len(self._director_output_buffer) % frame_size
            )
            if flush and self._director_output_buffer:
                remainder = len(self._director_output_buffer) % frame_size
                if remainder:
                    self._director_output_buffer.extend(
                        b"\x00" * (frame_size - remainder)
                    )
                send_size = len(self._director_output_buffer)
            if send_size <= 0:
                return
            chunk = bytes(self._director_output_buffer[:send_size])
            del self._director_output_buffer[:send_size]
        wire_chunk = chunk
        if self.background_audio is not None:
            wire_chunk = await self.background_audio.mix_with_voice(chunk)
        self._voice_submission_active.set()
        try:
            sent = await self.protocol.send_media(wire_chunk)
        finally:
            self._voice_submission_active.clear()
        if not sent:
            return
        self.call.bot_audio.submit(wire_chunk)
        self.echo_guard.note_playback(chunk)

    async def _send_output_audio(
        self,
        pcm16: bytes,
        *,
        generation: int | None = None,
        flush: bool = False,
    ) -> None:
        if generation is not None and generation != self.call.gemini.generation:
            return
        async with self._output_buffer_lock:
            if generation is not None and generation != self.call.gemini.generation:
                return
            self._output_buffer.extend(pcm16)
            frame_size = max(2, int(self.protocol.info.optimal_frame_size or 640))
            frame_size -= frame_size % 2
            send_size = len(self._output_buffer) - (
                len(self._output_buffer) % frame_size
            )
            if flush and self._output_buffer:
                send_size = len(self._output_buffer)
                remainder = send_size % frame_size
                if remainder:
                    self._output_buffer.extend(b"\x00" * (frame_size - remainder))
                    send_size = len(self._output_buffer)
            if send_size <= 0:
                return
            chunk = bytes(self._output_buffer[:send_size])
            del self._output_buffer[:send_size]
        # chan_websocket owns the real-time clock and automatically generates
        # silence when the application has no packet to send. Sending a
        # complete-frame batch immediately lets Asterisk keep a small remote
        # jitter buffer, avoids event-loop timer drift, and still preserves
        # barge-in because FLUSH_MEDIA clears that remote buffer.
        if generation is not None and generation != self.call.gemini.generation:
            return
        wire_chunk = chunk
        background_audio = getattr(self, "background_audio", None)
        voice_submission_active = getattr(self, "_voice_submission_active", None)
        if background_audio is not None:
            wire_chunk = await background_audio.mix_with_voice(chunk)
            if voice_submission_active is not None:
                voice_submission_active.set()
        try:
            sent = await self.protocol.send_media(wire_chunk, generation=generation)
            if not sent:
                return
            self.call.detector.set_bot_speaking(True)
        finally:
            if voice_submission_active is not None:
                voice_submission_active.clear()
        if self.call.gemini.generation not in self._first_output_generation:
            self._first_output_generation.add(self.call.gemini.generation)
            self.call.timeline.add(
                "ASTERISK_FIRST_AUDIO_SENT",
                generation=self.call.gemini.generation,
                bytes=len(wire_chunk),
            )
        self.call.bot_audio.submit(wire_chunk)
        echo_guard = getattr(self, "echo_guard", None)
        if echo_guard is not None:
            # Keep the established echo guard trained on the model voice only.
            # The optional office track is never fed into inbound processing.
            echo_guard.note_playback(chunk)
        loop = asyncio.get_running_loop()
        sent_at = loop.time()
        previous_sent_at = getattr(self, "_last_output_submission_at", 0.0)
        previous_generation = getattr(self, "_last_output_submission_generation", None)
        if previous_sent_at and previous_generation == generation:
            actual_gap_ms = (sent_at - previous_sent_at) * 1000.0
            if actual_gap_ms >= 100.0:
                self.call.timeline.add(
                    "ASTERISK_OUTPUT_UNDERRUN",
                    generation=generation,
                    actual_ms=round(actual_gap_ms, 1),
                    bytes=len(chunk),
                )
        self._last_output_submission_at = sent_at
        self._last_output_submission_generation = self.call.gemini.generation

    async def _background_loop(self) -> None:
        """Pace the optional loop only on the Asterisk outbound leg."""
        background = self.background_audio
        if background is None:
            return
        await self.protocol.media_started.wait()
        self.call.timeline.add(
            "BACKGROUND_AUDIO_PLAYBACK_STARTED",
            volume_percent=background.volume_percent,
        )
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while not self._closed:
            ptime_seconds = max(0.01, float(self.protocol.info.ptime or 20) / 1000.0)
            frame_size = max(2, int(self.protocol.info.optimal_frame_size or 640))
            frame_size -= frame_size % 2
            # Voice batches already carry the mixed background. Do not enqueue
            # separate background frames while model playback is active.
            if (
                self._voice_submission_active.is_set()
                or self.call.detector.bot_speaking
                or self.call.gemini.bot_audio_active.is_set()
            ):
                next_tick = loop.time() + ptime_seconds
                await asyncio.sleep(ptime_seconds)
                continue
            frame = await background.background_bytes(frame_size)
            if frame:
                sent = await self.protocol.send_media(frame)
                if sent:
                    self.call.bot_audio.submit(frame)
            next_tick += ptime_seconds
            delay = next_tick - loop.time()
            if delay <= 0:
                next_tick = loop.time()
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(delay)

    async def _discard_output_buffer(self) -> None:
        async with self._output_buffer_lock:
            self._output_buffer.clear()
        if getattr(self, "effects_active", False):
            self.effects.discard_pending_audio()

    async def _playback_monitor(self) -> None:
        handled_generation = -1
        while True:
            generation = await self.call.gemini.turn_complete_queue.get()
            if generation == handled_generation:
                self.call.gemini.turn_complete_queue.task_done()
                continue
            handled_generation = generation
            # A barge-in can produce a late turn-complete notification for the
            # generation that was just interrupted. It must not flush/mark
            # playback or clear the speaking state of the newer response.
            if generation != self.call.gemini.generation:
                self.call.gemini.turn_complete_queue.task_done()
                continue

            # Wait until all Gemini packets already received have been handed
            # to Asterisk, then place a marker behind them in Asterisk's queue.
            try:
                await self.call.gemini.output_audio.join()
                if self.effects_active:
                    pending_audio = self.effects.flush_actor_audio()
                    if pending_audio:
                        pending_audio = self._apply_duck(pending_audio)
                        inserted_pause_ms = self.effects.take_inserted_pause_ms()
                        if inserted_pause_ms:
                            self.call.timeline.add(
                                "ACTOR_MICRO_PAUSE",
                                generation=generation,
                                duration_ms=inserted_pause_ms,
                                source="acoustic_gap_flush",
                            )
                        await self._send_output_audio(
                            pending_audio, generation=generation
                        )
                await self._send_output_audio(b"", generation=generation, flush=True)
                correlation_id = f"elvin-{generation}"
                try:
                    waiter = await self.protocol.mark(correlation_id)
                    await asyncio.wait_for(waiter.wait(), timeout=15.0)
                    self.call.timeline.add(
                        "ASTERISK_PLAYBACK_END",
                        generation=generation,
                        confirmed=True,
                    )
                except TimeoutError:
                    self.call.timeline.add(
                        "ASTERISK_PLAYBACK_END",
                        generation=generation,
                        confirmed=False,
                    )
                self.call.detector.set_bot_speaking(False)
                transcript = self.call.gemini.transcript_for_generation(generation)
                if (
                    self.director is not None
                    and not self._director_degraded
                    and transcript
                ):
                    try:
                        await self.director.send_actor_transcript(transcript)
                    except Exception as exc:
                        self._degrade_director(exc, operation="send_actor_transcript")
                self._actor_turn_ended_at.pop(generation, None)
                self._micro_pause_styles.pop(generation, None)
                self._micro_pause_confidence.pop(generation, None)
                self._response_delay_applied.discard(generation)
                self._filler_requested.discard(generation)
                self._fillers_played_by_generation.pop(generation, None)
                if self._active_filler_actor_generation == generation:
                    self._active_filler_actor_generation = None
            finally:
                self.call.gemini.turn_complete_queue.task_done()
