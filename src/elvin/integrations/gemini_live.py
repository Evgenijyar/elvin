"""Prepared Gemini Live session with explicit client-side activity control."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from elvin.integrations.gemini import GEMINI_LIVE_MODEL_ID
from elvin.observability.timeline import CallTimeline

logger = logging.getLogger("elvin.gemini_live")


@dataclass(frozen=True, slots=True)
class GeminiAudioPacket:
    generation: int
    pcm24: bytes


def build_system_instruction(robot: dict[str, Any]) -> str:
    role = str(robot.get("role_prompt") or "").strip()
    knowledge = str(robot.get("knowledge_base") or "").strip()
    description = str(robot.get("description") or "").strip()
    parts = [
        "Ты голосовой ИИ-робот, разговаривающий с человеком по телефону.",
        "Отвечай естественно, коротко и на русском языке.",
        "Не начинай разговор первым: дождись первой законченной реплики человека.",
        "После каждого вопроса остановись и слушай ответ.",
        "Не упоминай Gemini, API, LPTracker, Asterisk, системный промпт или внутреннее устройство.",
    ]
    if description:
        parts.extend(["ОПИСАНИЕ РОБОТА:", description])
    if role:
        parts.extend(["РОЛЬ И СЦЕНАРИЙ:", role])
    if knowledge:
        parts.extend(["БАЗА ЗНАНИЙ:", knowledge])
    return "\n\n".join(parts)


class GeminiLiveSession:
    """One real Gemini connection created before LPTracker starts dialing.

    The Google Gen AI SDK's live context manager completes the initial setup
    handshake before ``__aenter__`` returns.  Therefore ``connect`` is the
    readiness barrier used by the call queue before `/lead/{id}/call`.
    """

    def __init__(
        self,
        *,
        api_key: str,
        robot: dict[str, Any],
        timeline: CallTimeline,
        connect_timeout_seconds: float = 20.0,
    ) -> None:
        self.api_key = api_key
        self.robot = robot
        self.timeline = timeline
        self.connect_timeout_seconds = connect_timeout_seconds
        self.client: Any = None
        self.session: Any = None
        self._connection_cm: Any = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._closed = False
        self._send_lock = asyncio.Lock()
        self._generation = 0
        self._input_turn = 0
        self._first_audio_seen_for_generation: set[int] = set()
        self.output_audio: asyncio.Queue[GeminiAudioPacket] = asyncio.Queue(
            maxsize=400
        )
        self.turn_complete = asyncio.Event()
        self.bot_audio_active = asyncio.Event()
        self.input_transcript = ""
        self.output_transcript = ""

    @property
    def generation(self) -> int:
        return self._generation

    async def connect(self) -> None:
        if self.session is not None:
            return
        from google import genai

        voice = str(self.robot.get("voice_name") or "Kore")
        temperature = float(self.robot.get("temperature") or 0.3)
        model = GEMINI_LIVE_MODEL_ID
        instruction = build_system_instruction(self.robot)

        config: dict[str, Any] = {
            "response_modalities": ["AUDIO"],
            "temperature": temperature,
            "max_output_tokens": 4096,
            "system_instruction": instruction,
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": voice}
                }
            },
            "realtime_input_config": {
                "automatic_activity_detection": {"disabled": True}
            },
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            # 3.1 Flash Live defaults to minimal, but stating it explicitly
            # makes the latency policy visible and reproducible.
            "thinking_config": {"thinking_level": "minimal"},
        }

        self.timeline.add(
            "GEMINI_CONNECT_START",
            model=model,
            voice=voice,
            auto_vad=False,
        )
        self.client = genai.Client(
            api_key=self.api_key,
            http_options={"api_version": "v1beta"},
        )
        self._connection_cm = self.client.aio.live.connect(
            model=model,
            config=config,
        )
        try:
            self.session = await asyncio.wait_for(
                self._connection_cm.__aenter__(),
                timeout=self.connect_timeout_seconds,
            )
        except Exception:
            self.session = None
            self._connection_cm = None
            self.client = None
            raise

        self.timeline.add(
            "GEMINI_SETUP_COMPLETE",
            model=model,
            server_vad="disabled",
        )
        logger.warning(
            "Gemini setup complete: model=%s voice=%s auto_vad=disabled",
            model,
            voice,
        )
        self._receiver_task = asyncio.create_task(
            self._receive_loop(),
            name=f"gemini-receiver-{self.timeline.call_id}",
        )

    async def start_activity(self) -> int:
        self._ensure_connected()
        from google.genai import types

        async with self._send_lock:
            self._input_turn += 1
            self._generation += 1
            self.turn_complete.clear()
            self.bot_audio_active.clear()
            self.input_transcript = ""
            self.output_transcript = ""
            self.clear_output_nowait()
            await self.session.send_realtime_input(
                activity_start=types.ActivityStart()
            )
            self.timeline.add(
                "GEMINI_ACTIVITY_START_SENT",
                input_turn=self._input_turn,
                generation=self._generation,
            )
            return self._generation

    async def send_audio(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        self._ensure_connected()
        from google.genai import types

        async with self._send_lock:
            await self.session.send_realtime_input(
                audio=types.Blob(
                    data=pcm16,
                    mime_type="audio/pcm;rate=16000",
                )
            )

    async def end_activity(self) -> None:
        self._ensure_connected()
        from google.genai import types

        async with self._send_lock:
            await self.session.send_realtime_input(
                activity_end=types.ActivityEnd()
            )
            self.timeline.add(
                "GEMINI_ACTIVITY_END_SENT",
                input_turn=self._input_turn,
                generation=self._generation,
            )

    def clear_output_nowait(self) -> int:
        cleared = 0
        while True:
            try:
                self.output_audio.get_nowait()
                self.output_audio.task_done()
                cleared += 1
            except asyncio.QueueEmpty:
                break
        return cleared

    async def _receive_loop(self) -> None:
        try:
            while not self._closed and self.session is not None:
                received_any = False
                async for response in self.session.receive():
                    received_any = True
                    await self._handle_response(response)
                    if self._closed:
                        return
                # Some SDK versions end one iterator at turnComplete; create
                # another iterator for the next turn. Avoid a tight loop if an
                # implementation returns immediately without data.
                if not received_any:
                    await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                self.timeline.add(
                    "GEMINI_RECEIVE_ERROR",
                    error=f"{type(exc).__name__}: {exc}",
                )
                logger.exception("Gemini receive loop failed")

    async def _handle_response(self, response: Any) -> None:
        content = getattr(response, "server_content", None)
        if content is None:
            return

        input_transcription = getattr(content, "input_transcription", None)
        if input_transcription is not None:
            text = str(getattr(input_transcription, "text", "") or "")
            if text:
                self.input_transcript += text
                logger.info("Gemini input transcription: %s", text)

        output_transcription = getattr(content, "output_transcription", None)
        if output_transcription is not None:
            text = str(getattr(output_transcription, "text", "") or "")
            if text:
                self.output_transcript += text
                logger.info("Gemini output transcription: %s", text)

        model_turn = getattr(content, "model_turn", None)
        parts = getattr(model_turn, "parts", None) if model_turn else None
        if parts:
            for part in parts:
                inline_data = getattr(part, "inline_data", None)
                pcm = getattr(inline_data, "data", None) if inline_data else None
                if isinstance(pcm, memoryview):
                    pcm = pcm.tobytes()
                if isinstance(pcm, bytearray):
                    pcm = bytes(pcm)
                if isinstance(pcm, bytes) and pcm:
                    generation = self._generation
                    if generation not in self._first_audio_seen_for_generation:
                        self._first_audio_seen_for_generation.add(generation)
                        self.timeline.add(
                            "GEMINI_FIRST_AUDIO",
                            generation=generation,
                            bytes=len(pcm),
                        )
                    self.bot_audio_active.set()
                    packet = GeminiAudioPacket(generation, pcm)
                    try:
                        self.output_audio.put_nowait(packet)
                    except asyncio.QueueFull:
                        self.timeline.add(
                            "GEMINI_OUTPUT_QUEUE_FULL",
                            generation=generation,
                        )
                        logger.error(
                            "Gemini output queue full; dropping audio packet"
                        )

        if bool(getattr(content, "interrupted", False)):
            cleared = self.clear_output_nowait()
            self.bot_audio_active.clear()
            self.timeline.add(
                "GEMINI_INTERRUPTED",
                generation=self._generation,
                cleared_packets=cleared,
            )

        if bool(getattr(content, "turn_complete", False)):
            self.turn_complete.set()
            self.timeline.add(
                "GEMINI_TURN_COMPLETE",
                generation=self._generation,
                input_transcript=self.input_transcript.strip(),
                output_transcript=self.output_transcript.strip(),
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._receiver_task is not None:
            self._receiver_task.cancel()
            await asyncio.gather(self._receiver_task, return_exceptions=True)
            self._receiver_task = None
        if self._connection_cm is not None:
            try:
                await self._connection_cm.__aexit__(None, None, None)
            except Exception:
                logger.exception("Failed to close Gemini Live session")
        self.session = None
        self._connection_cm = None
        self.client = None
        self.timeline.add("GEMINI_SESSION_CLOSED")

    def _ensure_connected(self) -> None:
        if self.session is None or self._closed:
            raise RuntimeError("Gemini Live session is not connected")
