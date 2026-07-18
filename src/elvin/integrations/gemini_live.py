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
    # Keep the live-model instruction as real UTF-8 Russian text. The
    # historical literals above were mojibake and made the model receive a
    # corrupted system prompt.
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
    # Rebuild optional sections as well; historical literals in this function
    # contain mojibake labels and must never reach Gemini.
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
    parts = [
        "\u0422\u044b \u0433\u043e\u043b\u043e\u0441\u043e\u0432\u043e\u0439 \u0418\u0418-\u0440\u043e\u0431\u043e\u0442, \u0440\u0430\u0437\u0433\u043e\u0432\u0430\u0440\u0438\u0432\u0430\u044e\u0449\u0438\u0439 \u0441 \u0447\u0435\u043b\u043e\u0432\u0435\u043a\u043e\u043c \u043f\u043e \u0442\u0435\u043b\u0435\u0444\u043e\u043d\u0443.",
        "\u041e\u0442\u0432\u0435\u0447\u0430\u0439 \u0435\u0441\u0442\u0435\u0441\u0442\u0432\u0435\u043d\u043d\u043e, \u043a\u043e\u0440\u043e\u0442\u043a\u043e \u0438 \u043d\u0430 \u0440\u0443\u0441\u0441\u043a\u043e\u043c \u044f\u0437\u044b\u043a\u0435.",
        "\u041d\u0435 \u043d\u0430\u0447\u0438\u043d\u0430\u0439 \u0440\u0430\u0437\u0433\u043e\u0432\u043e\u0440 \u043f\u0435\u0440\u0432\u044b\u043c: \u0434\u043e\u0436\u0434\u0438\u0441\u044c \u043f\u0435\u0440\u0432\u043e\u0439 \u0437\u0430\u043a\u043e\u043d\u0447\u0435\u043d\u043d\u043e\u0439 \u0440\u0435\u043f\u043b\u0438\u043a\u0438 \u0447\u0435\u043b\u043e\u0432\u0435\u043a\u0430.",
        "\u041f\u043e\u0441\u043b\u0435 \u043a\u0430\u0436\u0434\u043e\u0433\u043e \u0432\u043e\u043f\u0440\u043e\u0441\u0430 \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0441\u044c \u0438 \u0441\u043b\u0443\u0448\u0430\u0439 \u043e\u0442\u0432\u0435\u0442.",
        "\u041d\u0435 \u0443\u043f\u043e\u043c\u0438\u043d\u0430\u0439 Gemini, API, LPTracker, Asterisk, \u0441\u0438\u0441\u0442\u0435\u043c\u043d\u044b\u0439 \u043f\u0440\u043e\u043c\u043f\u0442 \u0438\u043b\u0438 \u0432\u043d\u0443\u0442\u0440\u0435\u043d\u043d\u0435\u0435 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u043e.",
        "\u041e\u0442\u0432\u0435\u0447\u0430\u0439 \u0432 \u043f\u0435\u0440\u0432\u0443\u044e \u043e\u0447\u0435\u0440\u0435\u0434\u044c \u043d\u0430 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0439 \u0432\u043e\u043f\u0440\u043e\u0441 \u0441\u043e\u0431\u0435\u0441\u0435\u0434\u043d\u0438\u043a\u0430. \u041d\u0435 \u043f\u043e\u0432\u0442\u043e\u0440\u044f\u0439 \u0434\u043e\u0441\u043b\u043e\u0432\u043d\u043e \u0443\u0436\u0435 \u0441\u043a\u0430\u0437\u0430\u043d\u043d\u043e\u0435 \u0438 \u043d\u0435 \u0432\u043e\u0437\u0432\u0440\u0430\u0449\u0430\u0439\u0441\u044f \u043a \u0432\u0441\u0442\u0443\u043f\u043b\u0435\u043d\u0438\u044e \u0431\u0435\u0437 \u043f\u0440\u044f\u043c\u043e\u0439 \u043f\u0440\u0438\u0447\u0438\u043d\u044b.",
        "\u041e\u0434\u043d\u0430 \u0440\u0435\u043f\u043b\u0438\u043a\u0430 \u2014 \u043e\u0434\u0438\u043d\u0430-\u0442\u0440\u0438 \u043a\u043e\u0440\u043e\u0442\u043a\u0438\u0445 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u044f. \u041d\u0435 \u0437\u0430\u0434\u0430\u0432\u0430\u0439 \u043d\u043e\u0432\u044b\u0439 \u0432\u043e\u043f\u0440\u043e\u0441, \u043f\u043e\u043a\u0430 \u043d\u0435 \u043e\u0442\u0432\u0435\u0442\u0438\u043b \u043d\u0430 \u0442\u0435\u043a\u0443\u0449\u0438\u0439.",
        "\u0415\u0441\u043b\u0438 \u0441\u043e\u0431\u0435\u0441\u0435\u0434\u043d\u0438\u043a \u043d\u0435 \u0445\u043e\u0447\u0435\u0442 \u0440\u0430\u0437\u0433\u043e\u0432\u0430\u0440\u0438\u0432\u0430\u0442\u044c \u0438\u043b\u0438 \u043f\u0440\u043e\u0441\u0438\u0442 \u043f\u0440\u0435\u043a\u0440\u0430\u0442\u0438\u0442\u044c \u0437\u0432\u043e\u043d\u043e\u043a, \u0432\u0435\u0436\u043b\u0438\u0432\u043e \u043f\u043e\u043f\u0440\u043e\u0449\u0430\u0439\u0441\u044f \u0431\u0435\u0437 \u043d\u043e\u0432\u043e\u0433\u043e \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u044f.",
    ]
    if description:
        parts.extend(["\u041e\u041f\u0418\u0421\u0410\u041d\u0418\u0415 \u0420\u041e\u0411\u041e\u0422\u0410:", description])
    if role:
        parts.extend(["\u0420\u041e\u041b\u042c \u0418 \u0421\u0426\u0415\u041d\u0410\u0420\u0418\u0419:", role])
    if knowledge:
        parts.extend(["\u0411\u0410\u0417\u0410 \u0417\u041d\u0410\u041d\u0418\u0419:", knowledge])
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
        # Gemini can send the interrupted/turn-complete notifications for the
        # previous model turn after the caller has already started the next
        # activity. Keep that previous generation separate so late packets do
        # not overwrite the new turn's transcripts or audio state.
        self._pending_server_generation: int | None = None
        self._response_open_generation: int | None = None
        self._awaiting_response_generation: int | None = None
        self._turn_complete_events: dict[int, asyncio.Event] = {}
        self._input_transcripts: dict[int, str] = {}
        self._output_transcripts: dict[int, str] = {}
        self.output_audio: asyncio.Queue[GeminiAudioPacket] = asyncio.Queue(
            maxsize=400
        )
        # A receiver failure must wake the media bridge immediately.  Without
        # this event the output task can wait forever on an empty queue and the
        # call is only marked failed after the full call timeout.
        self.receive_error: BaseException | None = None
        self.receive_failed = asyncio.Event()
        self.turn_complete = asyncio.Event()
        self.turn_complete_generation = -1
        self.turn_complete_queue: asyncio.Queue[int] = asyncio.Queue()
        self.bot_audio_active = asyncio.Event()
        self.input_transcript = ""
        self.output_transcript = ""

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def response_open_generation(self) -> int | None:
        """Generation whose server response has not completed yet.

        The server may take a few hundred milliseconds to send its first
        response packet.  ``ActivityEnd`` is therefore tracked separately so
        the bridge can still serialize a new caller turn during that gap.
        """
        return (
            self._response_open_generation
            if self._response_open_generation is not None
            else self._awaiting_response_generation
        )

    async def wait_for_response_idle(self, timeout: float = 8.0) -> None:
        """Wait until all server output belonging to the current response ends.

        Manual activity control allows a caller to start talking while Gemini
        is still preparing its first audio packet.  Opening another activity
        in that window can cancel the pending response before it ever reaches
        the audio queue.  The bridge serializes those non-barge-in turns and
        uses this wait before sending them.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.1, timeout)
        while True:
            generation = self.response_open_generation
            if generation is None:
                return
            event = self._turn_complete_events.setdefault(
                generation, asyncio.Event()
            )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Gemini response generation {generation} did not complete"
                )
            await asyncio.wait_for(event.wait(), timeout=remaining)

    async def connect(self) -> None:
        if self.session is not None:
            return
        from google import genai

        voice = str(self.robot.get("voice_name") or "Kore")
        configured_temperature = self.robot.get("temperature")
        temperature = float(
            0.3 if configured_temperature is None else configured_temperature
        )
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

        # Advance the local generation before waiting on the network send
        # lock. This lets the media bridge discard any packet already dequeued
        # from the previous model turn as soon as a barge-in is detected.
        self._input_turn += 1
        previous_generation = self._generation
        previous_response_generation = self.response_open_generation
        if previous_response_generation is not None:
            # An explicit activity start is also our barge-in boundary.  The
            # interrupted response can still deliver late notifications, so
            # keep its generation until the server's turn_complete arrives.
            self._pending_server_generation = previous_response_generation
        self._generation += 1
        self.turn_complete.clear()
        self.bot_audio_active.clear()
        self._input_transcripts[self._generation] = ""
        self._output_transcripts[self._generation] = ""
        self._turn_complete_events[self._generation] = asyncio.Event()
        self.input_transcript = ""
        self.output_transcript = ""
        self.clear_output_nowait(generation=previous_generation)
        generation = self._generation
        async with self._send_lock:
            await self.session.send_realtime_input(
                activity_start=types.ActivityStart()
            )
            self.timeline.add(
                "GEMINI_ACTIVITY_START_SENT",
                input_turn=self._input_turn,
                generation=generation,
            )
            return generation

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

        generation = self._generation
        # Set this before the network await: a very fast server can deliver
        # the first response packet immediately after ActivityEnd returns.
        self._awaiting_response_generation = generation
        async with self._send_lock:
            await self.session.send_realtime_input(
                activity_end=types.ActivityEnd()
            )
            self.timeline.add(
                "GEMINI_ACTIVITY_END_SENT",
                input_turn=self._input_turn,
                generation=generation,
            )

    def clear_output_nowait(self, generation: int | None = None) -> int:
        cleared = 0
        retained: list[GeminiAudioPacket] = []
        while True:
            try:
                packet = self.output_audio.get_nowait()
                self.output_audio.task_done()
                if generation is None or packet.generation == generation:
                    cleared += 1
                else:
                    retained.append(packet)
            except asyncio.QueueEmpty:
                break
        for packet in retained:
            self.output_audio.put_nowait(packet)
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
                self.receive_error = exc
                self.receive_failed.set()
                self.timeline.add(
                    "GEMINI_RECEIVE_ERROR",
                    error=f"{type(exc).__name__}: {exc}",
                )
                logger.exception("Gemini receive loop failed")

    async def _handle_response(self, response: Any) -> None:
        content = getattr(response, "server_content", None)
        if content is None:
            return

        # Transcriptions are independent server messages and can arrive after
        # the next activity has already started.  An interrupted/standalone
        # turn-complete message belongs to the previous generation; a model
        # audio turn without that control marker is the new response and must
        # never be discarded just because the old turn-complete is late.
        model_turn = getattr(content, "model_turn", None)
        parts = getattr(model_turn, "parts", None) if model_turn else None
        interrupted = bool(getattr(content, "interrupted", False))
        turn_complete = bool(getattr(content, "turn_complete", False))
        pending_generation = self._pending_server_generation
        is_pending_previous = pending_generation is not None and (
            interrupted
            or (turn_complete and model_turn is None)
            or (
                model_turn is None
                and (
                    getattr(content, "input_transcription", None) is not None
                    or getattr(content, "output_transcription", None) is not None
                )
            )
        )
        generation = (
            pending_generation if is_pending_previous else self._generation
        )
        input_transcript = self._input_transcripts.setdefault(generation, "")
        output_transcript = self._output_transcripts.setdefault(generation, "")

        input_transcription = getattr(content, "input_transcription", None)
        if input_transcription is not None:
            text = str(getattr(input_transcription, "text", "") or "")
            if text:
                input_transcript += text
                self._input_transcripts[generation] = input_transcript
                if generation == self._generation:
                    self.input_transcript = input_transcript
                logger.info("Gemini input transcription: %s", text)

        output_transcription = getattr(content, "output_transcription", None)
        if output_transcription is not None:
            text = str(getattr(output_transcription, "text", "") or "")
            if text:
                output_transcript += text
                self._output_transcripts[generation] = output_transcript
                if generation == self._generation:
                    self.output_transcript = output_transcript
                logger.info("Gemini output transcription: %s", text)

        if not is_pending_previous and (
            input_transcription is not None
            or output_transcription is not None
            or model_turn is not None
        ):
            self._response_open_generation = generation
        if parts:
            for part in parts:
                inline_data = getattr(part, "inline_data", None)
                pcm = getattr(inline_data, "data", None) if inline_data else None
                if isinstance(pcm, memoryview):
                    pcm = pcm.tobytes()
                if isinstance(pcm, bytearray):
                    pcm = bytes(pcm)
                if isinstance(pcm, bytes) and pcm:
                    if is_pending_previous:
                        # Audio for the interrupted turn is stale. The
                        # following turn-complete event will release the
                        # pending marker, after which new audio is accepted.
                        continue
                    self._response_open_generation = generation
                    if generation not in self._first_audio_seen_for_generation:
                        self._first_audio_seen_for_generation.add(generation)
                        self.timeline.add(
                            "GEMINI_FIRST_AUDIO",
                            generation=generation,
                            bytes=len(pcm),
                        )
                    self.bot_audio_active.set()
                    packet = GeminiAudioPacket(generation, pcm)
                    # Preserve every audio packet.  The bridge applies
                    # backpressure to Asterisk's flow-control event, so
                    # dropping packets here would create audible gaps.
                    await self.output_audio.put(packet)

        if interrupted:
            cleared = self.clear_output_nowait(generation=generation)
            self.bot_audio_active.clear()
            self.timeline.add(
                "GEMINI_INTERRUPTED",
                generation=generation,
                cleared_packets=cleared,
            )

        if turn_complete:
            if is_pending_previous:
                self._pending_server_generation = None
            if self._response_open_generation == generation:
                self._response_open_generation = None
            if getattr(self, "_awaiting_response_generation", None) == generation:
                self._awaiting_response_generation = None
            turn_complete_events = getattr(
                self, "_turn_complete_events", None
            )
            if turn_complete_events is None:
                turn_complete_events = {}
                self._turn_complete_events = turn_complete_events
            turn_complete_events.setdefault(
                generation, asyncio.Event()
            ).set()
            self.turn_complete_generation = generation
            self.turn_complete.set()
            await self.turn_complete_queue.put(generation)
            self.timeline.add(
                "GEMINI_TURN_COMPLETE",
                generation=generation,
                input_transcript=input_transcript.strip(),
                output_transcript=output_transcript.strip(),
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
        if self.receive_error is not None:
            raise RuntimeError(
                "Gemini Live receiver failed: "
                f"{type(self.receive_error).__name__}: {self.receive_error}"
            ) from self.receive_error
