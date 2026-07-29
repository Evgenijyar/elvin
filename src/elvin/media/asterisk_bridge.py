"""Bidirectional Asterisk chan_websocket ↔ prepared Gemini bridge."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from elvin.integrations.asterisk_ami import AsteriskAmiClient
from elvin.media.audio import Pcm24To16Resampler, PlaybackEchoGuard
from elvin.media.interruption import (
    InterruptionAction,
    InterruptionPolicy,
    LocalInterruptionGate,
)
from elvin.media.runtime import PreparedVoiceCall

logger = logging.getLogger("elvin.asterisk")


@dataclass(slots=True)
class AsteriskMediaInfo:
    format: str = "slin16"
    optimal_frame_size: int = 640
    ptime: int = 20
    channel_id: str = ""
    channel: str = ""


@dataclass(slots=True)
class PendingCallerTurn:
    audio: bytes
    speech_ms: float
    speech_ended: bool = True
    segments: int = 1


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
        # chan_websocket documents FLUSH_MEDIA as also resetting its paused
        # state. Mirror command semantics locally: otherwise a preceding
        # MEDIA_XOFF can leave send_media waiting forever for an XON event
        # that is no longer required after the flush.
        if command == "PAUSE_MEDIA":
            self.media_allowed.clear()
        elif command in {"CONTINUE_MEDIA", "FLUSH_MEDIA"}:
            self.media_allowed.set()

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
        self._voice_submission_active = asyncio.Event()
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
        self._pending_turn_speech_ms = 0.0
        self._pending_turns: deque[PendingCallerTurn] = deque()
        self._pending_turn_drain_task: asyncio.Task[None] | None = None
        self._pending_drain_active = False
        self._pending_drain_turn: PendingCallerTurn | None = None
        self.interruption_policy = InterruptionPolicy.from_robot(call.robot)
        self.interruption_gate = LocalInterruptionGate(self.interruption_policy)
        self.asterisk_ami = self._create_asterisk_ami_client()
        self._voice_fade_task: asyncio.Task[None] | None = None
        self._voice_gain_reset_task: asyncio.Task[None] | None = None
        self._playback_completed_generation = -1
        self._playback_completion_event = asyncio.Event()
        self._robot_hangup_in_progress = False

    def _create_asterisk_ami_client(self) -> AsteriskAmiClient | None:
        needs_fade = self.interruption_policy.effective_fade_ms > 0
        needs_hangup = bool(
            str(self.call.robot.get("call_end_condition") or "").strip()
        )
        if not needs_fade and not needs_hangup:
            return None
        app = getattr(self.websocket, "app", None)
        state = getattr(app, "state", None)
        settings = getattr(state, "settings", None)
        if settings is None or not settings.asterisk_ami_configured:
            return None
        password = settings.asterisk_ami_password
        return AsteriskAmiClient(
            host=settings.asterisk_ami_host,
            port=settings.asterisk_ami_port,
            username=settings.asterisk_ami_username,
            password=password.get_secret_value(),
        )

    async def run(self) -> str:
        self.call.media_attached = True
        self.call.timeline.add("ASTERISK_WEBSOCKET_ATTACHED")
        self.call.timeline.add(
            "LOCAL_INTERRUPTION_POLICY",
            ignore_short_interjections=(
                self.interruption_policy.ignore_short_interjections
            ),
            interjection_max_speech_ms=(
                self.interruption_policy.interjection_max_speech_ms
            ),
            delayed_interruption=self.interruption_policy.delayed_interruption,
            interruption_tail_ms=self.interruption_policy.effective_tail_ms,
            interruption_fade_enabled=(
                self.interruption_policy.interruption_fade_enabled
            ),
            interruption_fade_ms=self.interruption_policy.effective_fade_ms,
            interruption_fade_available=self.asterisk_ami is not None,
        )
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
        end_call_task: asyncio.Task[str] | None = None
        if str(self.call.robot.get("call_end_condition") or "").strip():
            end_call_task = asyncio.create_task(
                self._end_call_monitor(),
                name=f"asterisk-end-call-{self.call.identity.call_id}",
            )
            tasks.add(end_call_task)
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
            if end_call_task is not None and end_call_task in done:
                result = end_call_task.result()
            elif self._robot_hangup_in_progress:
                result = "robot_hangup"
            elif input_task in done:
                result = input_task.result()
            else:
                result = "media_task_finished"
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return result
        finally:
            self._closed = True
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
            self.interruption_gate.reset()
            await self._wait_voice_gain_reset()
            await self._stop_voice_fade(reset_gain=True)
            if self.asterisk_ami is not None:
                await self.asterisk_ami.close()
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
                        self.call.detector.bot_speaking
                        and not self.call.detector.turn_open
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
                now = asyncio.get_running_loop().time()

                if decision.speech_started:
                    bot_playback_active = (
                        decision.interrupted_bot or self.call.detector.bot_speaking
                    )
                    response_audio_active = (
                        bot_playback_active
                        or self.call.gemini.bot_audio_active.is_set()
                    )
                    if bot_playback_active and self.interruption_policy.enabled:
                        self.interruption_gate.begin()
                        self.call.timeline.add(
                            "LOCAL_INTERRUPTION_CANDIDATE",
                            interjection_filter=(
                                self.interruption_policy.ignore_short_interjections
                            ),
                            speech_threshold_ms=(
                                self.interruption_policy.interjection_max_speech_ms
                            ),
                            tail_ms=self.interruption_policy.effective_tail_ms,
                        )
                    else:
                        await self._start_caller_activity(
                            decision=decision,
                            response_audio_active=response_audio_active,
                        )

                if self.interruption_gate.active:
                    gated = self.interruption_gate.observe(
                        audio=decision.audio_to_gemini,
                        speech_ms=decision.speech_ms,
                        speech_ended=decision.speech_ended,
                        now=now,
                    )
                    if gated.confirmed_now:
                        self.call.timeline.add(
                            "LOCAL_INTERRUPTION_CONFIRMED",
                            speech_ms=round(gated.speech_ms, 1),
                            buffered_bytes=gated.audio_bytes,
                            tail_ms=self.interruption_policy.effective_tail_ms,
                        )
                        self._start_voice_fade()
                    if gated.action == InterruptionAction.IGNORE:
                        self.call.timeline.add(
                            "LOCAL_INTERJECTION_IGNORED",
                            speech_ms=round(gated.speech_ms, 1),
                            buffered_bytes=gated.audio_bytes,
                        )
                    elif gated.action == InterruptionAction.COMMIT:
                        await self._commit_local_interruption(
                            gated.audio,
                            speech_ms=gated.speech_ms,
                            speech_ended=gated.speech_ended,
                        )
                    # Tentative audio is owned exclusively by the local gate.
                    # It must never leak into Gemini before commit.
                    continue

                if decision.audio_to_gemini:
                    if self._pending_turn_audio is not None:
                        self._pending_turn_audio.extend(decision.audio_to_gemini)
                        self._pending_turn_speech_ms = max(
                            self._pending_turn_speech_ms,
                            decision.speech_ms,
                        )
                    elif self._active_activity_started:
                        await self._send_audio_to_gemini(decision.audio_to_gemini)

                if decision.speech_ended:
                    if self._pending_turn_audio is not None:
                        pending_audio = bytes(self._pending_turn_audio)
                        self._pending_turn_audio = None
                        pending_speech_ms = self._pending_turn_speech_ms
                        self._pending_turn_speech_ms = 0.0
                        if pending_audio:
                            self._pending_turns.append(
                                PendingCallerTurn(
                                    audio=pending_audio,
                                    speech_ms=pending_speech_ms,
                                )
                            )
                            self.call.timeline.add(
                                "PENDING_TURN_QUEUED",
                                bytes=len(pending_audio),
                                queue_size=len(self._pending_turns),
                            )
                            self._schedule_pending_turn_drain()
                    elif self._active_activity_started:
                        await self.call.gemini.end_activity()
                        self._active_activity_started = False
        except WebSocketDisconnect:
            return "caller_hangup"

    async def _start_caller_activity(
        self,
        *,
        decision: Any,
        response_audio_active: bool,
    ) -> None:
        """Preserve the stable immediate/serialized activity behavior."""
        # A PCM remainder from a previous Gemini generation must never be
        # concatenated with the new response.
        self.resampler.reset()
        await self._discard_output_buffer()
        response_open_generation = getattr(
            self.call.gemini,
            "response_open_generation",
            None,
        )
        pending_prefix = b""
        if response_audio_active and self._pending_drain_active:
            await self._cancel_pending_drain()
        if response_audio_active:
            self._discard_pending_turns()
        if response_audio_active and self._pending_turn_audio is not None:
            pending_prefix = bytes(self._pending_turn_audio)
            self._pending_turn_audio = None
            self._pending_turn_speech_ms = 0.0

        if (
            (response_open_generation is not None and not response_audio_active)
            or (self._pending_drain_active and not response_audio_active)
            or (self._active_activity_started and not response_audio_active)
        ):
            self._pending_turn_audio = bytearray()
            self._pending_turn_speech_ms = 0.0
            self.call.timeline.add(
                "PENDING_TURN_STARTED",
                waiting_for_generation=response_open_generation,
            )
        elif response_audio_active:
            cleared = self.call.gemini.clear_output_nowait()
            await self.call.gemini.start_activity()
            await self.protocol.command("FLUSH_MEDIA")
            self.echo_guard.clear()
            self._last_output_submission_at = 0.0
            self.call.detector.set_bot_speaking(False)
            self._active_activity_started = True
            self.call.timeline.add(
                "BARGE_IN_FLUSH",
                cleared_gemini_packets=cleared,
                reason=(
                    "bot_audio" if decision.interrupted_bot else "pending_response"
                ),
            )
            if pending_prefix:
                await self._send_audio_to_gemini(pending_prefix)
        else:
            await self.call.gemini.start_activity()
            self._active_activity_started = True
            if pending_prefix:
                await self._send_audio_to_gemini(pending_prefix)

    async def _commit_local_interruption(
        self,
        audio: bytes,
        *,
        speech_ms: float,
        speech_ended: bool,
        reason: str = "local_interruption_policy",
    ) -> None:
        """Commit buffered caller audio at the single barge-in boundary."""
        await self._stop_voice_fade(reset_gain=False)
        if self._pending_drain_active:
            await self._cancel_pending_drain()
        self._discard_pending_turns()
        pending_prefix = b""
        if self._pending_turn_audio is not None:
            pending_prefix = bytes(self._pending_turn_audio)
            self._pending_turn_audio = None
            self._pending_turn_speech_ms = 0.0

        self.resampler.reset()
        await self._discard_output_buffer()
        cleared = self.call.gemini.clear_output_nowait()
        # Advance Gemini's generation before flushing Asterisk so a stale
        # packet already dequeued by the output task cannot cross the boundary.
        await self.call.gemini.start_activity()
        await self.protocol.command("FLUSH_MEDIA")
        self._schedule_voice_gain_reset()
        self.echo_guard.clear()
        self._last_output_submission_at = 0.0
        self.call.detector.set_bot_speaking(False)
        self._active_activity_started = True

        if pending_prefix:
            await self._send_audio_to_gemini(pending_prefix)
        if audio:
            await self._send_audio_to_gemini(audio)
        if speech_ended:
            await self.call.gemini.end_activity()
            self._active_activity_started = False

        self.call.timeline.add(
            "BARGE_IN_FLUSH",
            cleared_gemini_packets=cleared,
            reason=reason,
            speech_ms=round(speech_ms, 1),
            buffered_bytes=len(audio),
            speech_ended=speech_ended,
            tail_ms=self.interruption_policy.effective_tail_ms,
            fade_ms=self.interruption_policy.effective_fade_ms,
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
        if dropped:
            self.call.timeline.add(
                "PENDING_TURNS_DROPPED",
                count=dropped,
                reason="caller_barge_in",
            )

    async def _take_pending_caller_turn(self) -> PendingCallerTurn | None:
        """Atomically transfer all pending caller input to one Gemini turn."""
        if self._pending_drain_active:
            await self._cancel_pending_drain()

        turns = list(self._pending_turns)
        self._pending_turns.clear()
        if self._pending_turn_audio is not None:
            audio = bytes(self._pending_turn_audio)
            self._pending_turn_audio = None
            turns.append(
                PendingCallerTurn(
                    audio=audio,
                    speech_ms=self._pending_turn_speech_ms,
                    speech_ended=False,
                )
            )
            self._pending_turn_speech_ms = 0.0
        if not turns:
            return None

        return self._coalesce_pending_turns(turns)

    @staticmethod
    def _coalesce_pending_turns(
        turns: list[PendingCallerTurn],
    ) -> PendingCallerTurn:
        return PendingCallerTurn(
            audio=b"".join(turn.audio for turn in turns),
            # Several short listener acknowledgements must not accidentally
            # become a full interruption merely because they were queued.
            speech_ms=max(turn.speech_ms for turn in turns),
            speech_ended=all(turn.speech_ended for turn in turns),
            segments=sum(turn.segments for turn in turns),
        )

    async def _promote_pending_turn_on_model_audio(self) -> bool:
        """Make caller input supersede a response as soon as it becomes audible.

        Returns True when the current model packet became stale and must not be
        sent to Asterisk.
        """
        if (
            self._pending_turn_audio is None
            and not self._pending_turns
            and not self._pending_drain_active
        ):
            return False

        pending = await self._take_pending_caller_turn()
        if pending is None or not pending.audio:
            return False
        self.call.timeline.add(
            "PENDING_TURN_PROMOTED",
            bytes=len(pending.audio),
            speech_ms=round(pending.speech_ms, 1),
            speech_ended=pending.speech_ended,
            segments=pending.segments,
        )

        if not self.interruption_policy.enabled:
            await self._commit_local_interruption(
                pending.audio,
                speech_ms=pending.speech_ms,
                speech_ended=pending.speech_ended,
                reason="pending_turn_promoted",
            )
            return True

        self.interruption_gate.begin()
        gated = self.interruption_gate.observe(
            audio=pending.audio,
            speech_ms=pending.speech_ms,
            speech_ended=pending.speech_ended,
            now=asyncio.get_running_loop().time(),
        )
        if gated.confirmed_now:
            self.call.timeline.add(
                "LOCAL_INTERRUPTION_CONFIRMED",
                speech_ms=round(gated.speech_ms, 1),
                buffered_bytes=gated.audio_bytes,
                tail_ms=self.interruption_policy.effective_tail_ms,
                origin="pending_turn",
            )
            self._start_voice_fade()
        if gated.action == InterruptionAction.IGNORE:
            self.call.timeline.add(
                "LOCAL_INTERJECTION_IGNORED",
                speech_ms=round(gated.speech_ms, 1),
                buffered_bytes=gated.audio_bytes,
                origin="pending_turn",
            )
            return False
        if gated.action == InterruptionAction.COMMIT:
            await self._commit_local_interruption(
                gated.audio,
                speech_ms=gated.speech_ms,
                speech_ended=gated.speech_ended,
                reason="pending_turn_promoted",
            )
            return True
        return False

    def _start_voice_fade(self) -> None:
        fade_ms = self.interruption_policy.effective_fade_ms
        tail_ms = self.interruption_policy.effective_tail_ms
        if fade_ms <= 0:
            return
        if self.asterisk_ami is None or not self.protocol.info.channel:
            self.call.timeline.add(
                "INTERRUPTION_VOICE_FADE_UNAVAILABLE",
                fade_ms=fade_ms,
                ami_configured=self.asterisk_ami is not None,
                channel_available=bool(self.protocol.info.channel),
            )
            return
        task = self._voice_fade_task
        if task is not None and not task.done():
            return
        self._voice_fade_task = asyncio.create_task(
            self._run_voice_fade(tail_ms=tail_ms, fade_ms=fade_ms),
            name=f"asterisk-voice-fade-{self.call.identity.call_id}",
        )
        self.call.timeline.add(
            "INTERRUPTION_VOICE_FADE_SCHEDULED",
            tail_ms=tail_ms,
            fade_ms=fade_ms,
            starts_after_ms=tail_ms - fade_ms,
        )

    async def _run_voice_fade(self, *, tail_ms: int, fade_ms: int) -> None:
        ami = self.asterisk_ami
        channel = self.protocol.info.channel
        if ami is None or not channel:
            return
        loop = asyncio.get_running_loop()
        fade_starts_at = loop.time() + (tail_ms - fade_ms) / 1000.0
        try:
            delay = fade_starts_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self.call.timeline.add(
                "INTERRUPTION_VOICE_FADE_STARTED",
                fade_ms=fade_ms,
            )
            step_count = max(2, min(40, math.ceil(fade_ms / 20)))
            duration_seconds = fade_ms / 1000.0
            for step in range(1, step_count + 1):
                target = fade_starts_at + duration_seconds * (step / step_count)
                delay = target - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                gain = max(0.001, 1.0 - step / step_count)
                await ami.set_channel_rx_gain(channel, gain)
            self.call.timeline.add(
                "INTERRUPTION_VOICE_FADE_COMPLETED",
                fade_ms=fade_ms,
                steps=step_count,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.call.timeline.add(
                "INTERRUPTION_VOICE_FADE_ERROR",
                error=f"{type(exc).__name__}: {exc}",
            )
            await self._reset_voice_gain()

    async def _stop_voice_fade(self, *, reset_gain: bool) -> None:
        task = getattr(self, "_voice_fade_task", None)
        self._voice_fade_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if reset_gain:
            await self._reset_voice_gain()

    def _schedule_voice_gain_reset(self) -> None:
        if getattr(self, "asterisk_ami", None) is None:
            return
        task = getattr(self, "_voice_gain_reset_task", None)
        if task is not None and not task.done():
            return
        self._voice_gain_reset_task = asyncio.create_task(
            self._reset_voice_gain(),
            name=f"asterisk-voice-gain-reset-{self.call.identity.call_id}",
        )

    async def _wait_voice_gain_reset(self) -> None:
        task = getattr(self, "_voice_gain_reset_task", None)
        if task is None:
            return
        await asyncio.gather(task, return_exceptions=True)
        if self._voice_gain_reset_task is task:
            self._voice_gain_reset_task = None

    async def _reset_voice_gain(self) -> None:
        ami = getattr(self, "asterisk_ami", None)
        channel = self.protocol.info.channel
        if ami is None or not channel:
            return
        try:
            await ami.set_channel_rx_gain(channel, 1.0)
            self.call.timeline.add("INTERRUPTION_VOICE_GAIN_RESET")
        except Exception as exc:
            self.call.timeline.add(
                "INTERRUPTION_VOICE_GAIN_RESET_ERROR",
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _end_call_monitor(self) -> str:
        """Wait for Gemini's visible ``end_call`` tool and hang up safely."""
        await self.call.gemini.end_call_requested.wait()
        requested_generation = self.call.gemini.end_call_generation
        if requested_generation is None:
            requested_generation = self.call.gemini.generation
        configured_wait_ms = self.call.robot.get("call_end_wait_ms")
        configured_delay_ms = self.call.robot.get("call_end_delay_ms")
        wait_ms = max(
            0,
            min(
                int(8000 if configured_wait_ms is None else configured_wait_ms),
                30_000,
            ),
        )
        delay_ms = max(
            0,
            min(
                int(250 if configured_delay_ms is None else configured_delay_ms),
                5000,
            ),
        )
        self.call.timeline.add(
            "ROBOT_END_CALL_REQUESTED",
            generation=requested_generation,
            reason=self.call.gemini.end_call_reason,
            playback_wait_ms=wait_ms,
            delay_ms=delay_ms,
        )

        if wait_ms > 0:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_ms / 1000.0
            while (
                self._playback_completed_generation < requested_generation
                and loop.time() < deadline
            ):
                self._playback_completion_event.clear()
                if self._playback_completed_generation >= requested_generation:
                    break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(
                        self._playback_completion_event.wait(),
                        timeout=remaining,
                    )
                except TimeoutError:
                    break

        playback_confirmed = (
            self._playback_completed_generation >= requested_generation
        )
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        await self._hangup_current_channel(
            reason=self.call.gemini.end_call_reason,
            playback_confirmed=playback_confirmed,
        )
        return "robot_hangup"

    async def _hangup_current_channel(
        self,
        *,
        reason: str,
        playback_confirmed: bool,
    ) -> None:
        """Ask chan_websocket to hang up, with AMI and socket fallbacks."""
        self._robot_hangup_in_progress = True
        channel = self.protocol.info.channel
        websocket_command_success = False
        ami_success = False

        try:
            # This is the native chan_websocket control command: Asterisk
            # hangs up this media channel and closes the WebSocket itself.
            await self.protocol.command("HANGUP")
            websocket_command_success = True
        except Exception as exc:
            self.call.timeline.add(
                "ROBOT_END_CALL_WEBSOCKET_COMMAND_ERROR",
                channel=channel,
                error=f"{type(exc).__name__}: {exc}",
            )
            ami = self.asterisk_ami
            if ami is not None and channel:
                try:
                    await ami.hangup_channel(channel, cause=16)
                    ami_success = True
                except Exception as ami_exc:
                    self.call.timeline.add(
                        "ROBOT_END_CALL_AMI_ERROR",
                        channel=channel,
                        error=f"{type(ami_exc).__name__}: {ami_exc}",
                    )
                    logger.exception(
                        "Unable to hang up Asterisk channel through AMI"
                    )

        self.call.timeline.add(
            "ROBOT_END_CALL_EXECUTED",
            channel=channel,
            reason=reason,
            playback_confirmed=playback_confirmed,
            websocket_command_success=websocket_command_success,
            ami_success=ami_success,
        )
        if self.websocket.application_state != WebSocketState.DISCONNECTED:
            try:
                await self.websocket.close(code=1000)
            except RuntimeError:
                pass

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
            pending_turn = self._pending_turns.popleft()
            pending_audio = pending_turn.audio
            self._pending_drain_active = True
            self._pending_drain_turn = pending_turn
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
                if self._pending_turns:
                    queued = [pending_turn, *self._pending_turns]
                    self._pending_turns.clear()
                    pending_turn = self._coalesce_pending_turns(queued)
                    pending_audio = pending_turn.audio
                    self._pending_drain_turn = pending_turn
                    self.call.timeline.add(
                        "PENDING_TURNS_COALESCED",
                        segments=pending_turn.segments,
                        bytes=len(pending_audio),
                    )
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
                await self.call.gemini.start_activity()
                activity_started = True
                await self._send_audio_to_gemini(pending_audio)
                await self.call.gemini.end_activity()
                activity_started = False
                sent = True
                self.call.timeline.add(
                    "PENDING_TURN_SENT",
                    bytes=len(pending_audio),
                    remaining=len(self._pending_turns),
                    segments=pending_turn.segments,
                )
            except asyncio.CancelledError:
                if not sent:
                    self._pending_turns.appendleft(pending_turn)
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
                    self._pending_turns.appendleft(pending_turn)
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
                self._pending_drain_turn = None
                self._pending_drain_active = False

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
                # Once a new user activity starts, old queued model audio must
                # not leak into the next turn even if it races with the server.
                if packet.generation != self.call.gemini.generation:
                    continue
                if await self._promote_pending_turn_on_model_audio():
                    continue
                await self._wait_voice_gain_reset()
                pcm16 = self.resampler.convert(packet.pcm24)
                if not pcm16:
                    continue
                await self._send_output_audio(
                    pcm16,
                    generation=packet.generation,
                )
            finally:
                self.call.gemini.output_audio.task_done()

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
                self._playback_completed_generation = max(
                    self._playback_completed_generation, generation
                )
                self._playback_completion_event.set()
            finally:
                self.call.gemini.turn_complete_queue.task_done()
