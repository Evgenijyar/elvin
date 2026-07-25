"""Bidirectional Asterisk chan_websocket ↔ prepared Gemini bridge."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from elvin.media.audio import Pcm24To16Resampler, PlaybackEchoGuard
from elvin.media.runtime import PreparedVoiceCall

logger = logging.getLogger("elvin.asterisk")


@dataclass(slots=True)
class AsteriskMediaInfo:
    format: str = "slin16"
    optimal_frame_size: int = 640
    ptime: int = 20
    channel_id: str = ""
    channel: str = ""


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
            if (
                generation is not None
                and generation != self.call.gemini.generation
            ):
                return False
            async with self.send_lock:
                if (
                    generation is not None
                    and generation != self.call.gemini.generation
                ):
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
            self.call.timeline.add(
                "ASTERISK_DTMF", digit=str(event.get("digit") or "")
            )
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
                    and asyncio.get_running_loop().time()
                    - self._last_echo_event_at
                    >= 0.25
                ):
                    self._last_echo_event_at = (
                        asyncio.get_running_loop().time()
                    )
                    self.call.timeline.add(
                        "PLAYBACK_ECHO_SUPPRESSED",
                        bytes=len(pcm),
                    )
                decision = await self.call.detector.process(
                    pcm,
                    echo_suppressed=echo_suppressed,
                )

                if decision.speech_started:
                    # A PCM remainder from a previous Gemini generation must
                    # never be concatenated with the new response. This can
                    # happen when the model emits less than one Asterisk
                    # frame before the caller speaks again.
                    self.resampler.reset()
                    await self._discard_output_buffer()
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
                    # A queued turn may be waiting for the previous response
                    # to finish. If that response starts speaking while the
                    # caller is still talking, turn it into a real barge-in:
                    # cancel the sender, flush the far end, and preserve the
                    # caller's already buffered pre-roll.
                    pending_prefix = b""
                    if response_audio_active and self._pending_drain_active:
                        await self._cancel_pending_drain()
                    if response_audio_active:
                        self._discard_pending_turns()
                    if response_audio_active and self._pending_turn_audio is not None:
                        pending_prefix = bytes(self._pending_turn_audio)
                        self._pending_turn_audio = None

                    if (
                        response_open_generation is not None
                        and not response_audio_active
                    ) or (
                        self._pending_drain_active
                        and not response_audio_active
                    ) or (
                        self._active_activity_started
                        and not response_audio_active
                    ):
                        # The model has not started speaking yet. Queue this
                        # utterance instead of opening a competing activity;
                        # otherwise Gemini can cancel both the pending old
                        # response and this new one.
                        self._pending_turn_audio = bytearray()
                        self.call.timeline.add(
                            "PENDING_TURN_STARTED",
                            waiting_for_generation=response_open_generation,
                        )
                    elif response_audio_active:
                        cleared = self.call.gemini.clear_output_nowait()
                        # Advance Gemini's generation before flushing Asterisk
                        # so an output chunk that was already dequeued cannot
                        # be sent after the barge-in boundary.
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
                                "bot_audio"
                                if decision.interrupted_bot
                                else "pending_response"
                            ),
                        )
                        if pending_prefix:
                            await self._send_audio_to_gemini(pending_prefix)
                    else:
                        await self.call.gemini.start_activity()
                        self._active_activity_started = True
                        if pending_prefix:
                            await self._send_audio_to_gemini(pending_prefix)

                if decision.audio_to_gemini:
                    if self._pending_turn_audio is not None:
                        self._pending_turn_audio.extend(
                            decision.audio_to_gemini
                        )
                    elif self._active_activity_started:
                        await self._send_audio_to_gemini(
                            decision.audio_to_gemini
                        )

                if decision.speech_ended:
                    if self._pending_turn_audio is not None:
                        pending_audio = bytes(self._pending_turn_audio)
                        self._pending_turn_audio = None
                        if pending_audio:
                            self._pending_turns.append(pending_audio)
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

    async def _send_audio_to_gemini(self, pcm16: bytes) -> None:
        """Send input in 20–40 ms chunks, including buffered pre-roll."""
        if not pcm16:
            return
        # 40 ms at 16 kHz, mono, signed 16-bit PCM.
        chunk_bytes = 1_280
        for offset in range(0, len(pcm16), chunk_bytes):
            await self.call.gemini.send_audio(
                pcm16[offset : offset + chunk_bytes]
            )

    async def _drain_pending_turns(self) -> None:
        while self._pending_turns and not self._closed:
            pending_audio = self._pending_turns.popleft()
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
                )
            except asyncio.CancelledError:
                if not sent:
                    self._pending_turns.appendleft(pending_audio)
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
        voice_submission_active = getattr(
            self, "_voice_submission_active", None
        )
        if background_audio is not None:
            wire_chunk = await background_audio.mix_with_voice(chunk)
            if voice_submission_active is not None:
                voice_submission_active.set()
        try:
            sent = await self.protocol.send_media(
                wire_chunk, generation=generation
            )
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
        previous_generation = getattr(
            self, "_last_output_submission_generation", None
        )
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
        self._last_output_submission_generation = (
            self.call.gemini.generation
        )

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
            ptime_seconds = max(
                0.01, float(self.protocol.info.ptime or 20) / 1000.0
            )
            frame_size = max(
                2, int(self.protocol.info.optimal_frame_size or 640)
            )
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
                await self._send_output_audio(
                    b"", generation=generation, flush=True
                )
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
            finally:
                self.call.gemini.turn_complete_queue.task_done()
