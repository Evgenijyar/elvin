"""Bidirectional Asterisk chan_websocket ↔ prepared Gemini bridge."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from elvin.media.audio import Pcm24To16Resampler
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

    async def send_media(self, pcm: bytes) -> None:
        if not pcm:
            return
        await self.media_allowed.wait()
        # Asterisk's underlying websocket layer rejects messages > 65500.
        for offset in range(0, len(pcm), 64_000):
            chunk = pcm[offset : offset + 64_000]
            async with self.send_lock:
                await self.websocket.send_bytes(chunk)

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
        self._closed = False
        self._first_input = True
        self._first_output_generation: set[int] = set()
        # chan_websocket re-times media most reliably when every binary frame
        # is an exact multiple of MEDIA_START.optimal_frame_size. Gemini
        # packets are arbitrary chunks, so retain only the small remainder
        # between packets and flush it with silence at turn end.
        self._output_buffer = bytearray()
        self._output_buffer_lock = asyncio.Lock()

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
                decision = await self.call.detector.process(pcm)

                if decision.speech_started:
                    # A PCM remainder from a previous Gemini generation must
                    # never be concatenated with the new response. This can
                    # happen when the model emits less than one Asterisk
                    # frame before the caller speaks again.
                    self.resampler.reset()
                    await self._discard_output_buffer()
                    if decision.interrupted_bot:
                        cleared = self.call.gemini.clear_output_nowait()
                        # Advance Gemini's generation before flushing Asterisk
                        # so an output chunk that was already dequeued cannot
                        # be sent after the barge-in boundary.
                        await self.call.gemini.start_activity()
                        await self.protocol.command("FLUSH_MEDIA")
                        self.call.detector.set_bot_speaking(False)
                        self.call.timeline.add(
                            "BARGE_IN_FLUSH",
                            cleared_gemini_packets=cleared,
                        )
                    else:
                        await self.call.gemini.start_activity()

                if decision.audio_to_gemini:
                    await self.call.gemini.send_audio(
                        decision.audio_to_gemini
                    )

                if decision.speech_ended:
                    await self.call.gemini.end_activity()
        except WebSocketDisconnect:
            return "caller_hangup"

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
            self.call.detector.set_bot_speaking(True)
            if self.call.gemini.generation not in self._first_output_generation:
                self._first_output_generation.add(self.call.gemini.generation)
                self.call.timeline.add(
                    "ASTERISK_FIRST_AUDIO_SENT",
                    generation=self.call.gemini.generation,
                    bytes=len(chunk),
                )
            self.call.bot_audio.submit(chunk)
        await self.protocol.send_media(chunk)

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
