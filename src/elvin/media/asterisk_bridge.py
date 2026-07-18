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
        buffering_enabled = False
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
                    if (
                        str(event.get("event")) == "MEDIA_START"
                        and not buffering_enabled
                    ):
                        if self.protocol.info.format != "slin16":
                            raise RuntimeError(
                                "Asterisk media format must be slin16; got "
                                f"{self.protocol.info.format}"
                            )
                        await self.protocol.command("START_MEDIA_BUFFERING")
                        buffering_enabled = True
                        logger.warning(
                            "Asterisk PCM input started: sample_rate=16000 "
                            "channels=1 frame_bytes=%s ptime=%sms",
                            self.protocol.info.optimal_frame_size,
                            self.protocol.info.ptime,
                        )
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
                    if decision.interrupted_bot:
                        cleared = self.call.gemini.clear_output_nowait()
                        await self.protocol.command("FLUSH_MEDIA")
                        self.resampler.reset()
                        self.call.detector.set_bot_speaking(False)
                        self.call.timeline.add(
                            "BARGE_IN_FLUSH",
                            cleared_gemini_packets=cleared,
                        )
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
            packet = await self.call.gemini.output_audio.get()
            try:
                # Once a new user activity starts, old queued model audio must
                # not leak into the next turn even if it races with the server.
                if packet.generation != self.call.gemini.generation:
                    continue
                pcm16 = self.resampler.convert(packet.pcm24)
                if not pcm16:
                    continue
                if packet.generation not in self._first_output_generation:
                    self._first_output_generation.add(packet.generation)
                    self.call.detector.set_bot_speaking(True)
                    self.call.timeline.add(
                        "ASTERISK_FIRST_AUDIO_SENT",
                        generation=packet.generation,
                        bytes=len(pcm16),
                    )
                self.call.bot_audio.submit(pcm16)
                await self.protocol.send_media(pcm16)
            finally:
                self.call.gemini.output_audio.task_done()

    async def _playback_monitor(self) -> None:
        handled_generation = -1
        while True:
            await self.call.gemini.turn_complete.wait()
            generation = self.call.gemini.generation
            self.call.gemini.turn_complete.clear()
            if generation == handled_generation:
                continue
            handled_generation = generation

            # Wait until all Gemini packets already received have been handed
            # to Asterisk, then place a marker behind them in Asterisk's queue.
            await self.call.gemini.output_audio.join()
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
