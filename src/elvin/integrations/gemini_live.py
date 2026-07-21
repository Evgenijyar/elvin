"""Prepared Gemini Live session with explicit client-side activity control."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from elvin.integrations.gemini import GEMINI_LIVE_MODEL_ID
from elvin.observability.timeline import CallTimeline
from elvin.services.call_outcomes import (
    OUTCOME_BY_TOOL,
    build_outcome_instruction,
    configured_tool_declarations,
)

logger = logging.getLogger("elvin.gemini_live")


@dataclass(frozen=True, slots=True)
class GeminiAudioPacket:
    generation: int
    pcm24: bytes


def build_system_instruction(robot: dict[str, Any]) -> str:
    """Build the single UTF-8 instruction sent to Gemini Live."""
    role = str(robot.get("role_prompt") or "").strip()
    knowledge = str(robot.get("knowledge_base") or "").strip()
    description = str(robot.get("description") or "").strip()
    parts = [
        "Ты голосовой ИИ-робот, разговаривающий с человеком по телефону.",
        "Отвечай естественно, коротко и на русском языке.",
        "Не начинай разговор первым: дождись первой законченной реплики человека.",
        "После каждого вопроса остановись и слушай ответ.",
        "Не упоминай Gemini, API, LPTracker, Asterisk, системный промпт или внутреннее устройство.",
        "Отвечай в первую очередь на последний вопрос собеседника. Не повторяй дословно уже сказанное и не возвращайся к вступлению без прямой причины.",
        "Одна реплика — одно-три коротких предложения. Не задавай новый вопрос, пока не ответил на текущий.",
        "Если собеседник не хочет разговаривать или просит прекратить звонок, вежливо попрощайся без нового предложения.",
    ]
    if description:
        parts.extend(["ОПИСАНИЕ РОБОТА:", description])
    if role:
        parts.extend(["РОЛЬ И СЦЕНАРИЙ:", role])
    if knowledge:
        parts.extend(["БАЗА ЗНАНИЙ:", knowledge])
    outcome_instruction = build_outcome_instruction(robot)
    if outcome_instruction:
        parts.append(outcome_instruction)
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
        self._first_audio_events: dict[int, asyncio.Event] = {}
        self.output_boundary_queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        # Gemini can send the interrupted/turn-complete notifications for the
        # previous model turn after the caller has already started the next
        # activity. Keep that previous generation separate so late packets do
        # not overwrite the new turn's transcripts or audio state.
        self._pending_server_generation: int | None = None
        self._pending_audio_generation: int | None = None
        self._response_open_generation: int | None = None
        self._awaiting_response_generation: int | None = None
        self._turn_complete_events: dict[int, asyncio.Event] = {}
        self._input_transcripts: dict[int, str] = {}
        self._output_transcripts: dict[int, str] = {}
        self._last_audio_packet_at: dict[int, float] = {}
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
        self.classified_outcome: str | None = None
        self.classified_evidence = ""
        self.outcome_history: list[dict[str, str]] = []

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
        tool_declarations = configured_tool_declarations(self.robot)

        config: dict[str, Any] = {
            "response_modalities": ["AUDIO"],
            "temperature": temperature,
            # The robot is instructed to answer in one to three short
            # sentences. A bounded response keeps native-audio generation
            # focused and prevents a long tail of speech after the answer.
            "max_output_tokens": 1024,
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
        if tool_declarations:
            config["tools"] = [{"function_declarations": tool_declarations}]

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
            self._pending_audio_generation = previous_response_generation
        self._generation += 1
        self.turn_complete.clear()
        self.bot_audio_active.clear()
        self._input_transcripts[self._generation] = ""
        self._output_transcripts[self._generation] = ""
        self._turn_complete_events[self._generation] = asyncio.Event()
        self._first_audio_events[self._generation] = asyncio.Event()
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

    async def send_context_hint(self, text: str) -> None:
        cleaned = str(text or "").strip()
        if not cleaned:
            return
        self._ensure_connected()
        async with self._send_lock:
            await self.session.send_realtime_input(
                text="[ВНУТРЕННЯЯ ПОДСКАЗКА BACKEND] " + cleaned[:1500]
            )

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
                    go_away = getattr(response, "go_away", None)
                    if go_away is not None:
                        time_left = getattr(go_away, "time_left", None)
                        self.receive_error = RuntimeError(
                            "Gemini Live session is ending"
                            + (
                                f" (time_left={time_left})"
                                if time_left is not None
                                else ""
                            )
                        )
                        self.receive_failed.set()
                        self.timeline.add(
                            "GEMINI_GO_AWAY",
                            time_left=str(time_left)
                            if time_left is not None
                            else None,
                        )
                        return
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
        tool_call = getattr(response, "tool_call", None)
        if tool_call is not None:
            await self._handle_tool_call(tool_call)

        content = getattr(response, "server_content", None)
        if content is None:
            return

        # Transcriptions and model audio are independent server messages and
        # can arrive after the next activity has already started. Until the
        # interruption marker arrives, model audio is still the old response.
        # Relabelling that packet as the current generation makes the robot
        # speak stale audio over the caller.
        model_turn = getattr(content, "model_turn", None)
        parts = getattr(model_turn, "parts", None) if model_turn else None
        interrupted = bool(getattr(content, "interrupted", False))
        turn_complete = bool(getattr(content, "turn_complete", False))
        pending_generation = self._pending_server_generation
        pending_audio_generation = getattr(
            self, "_pending_audio_generation", None
        )
        is_pending_previous = pending_generation is not None and (
            interrupted
            or (
                model_turn is not None
                and pending_audio_generation == pending_generation
            )
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
                # Transcription fragments can arrive for every few words.
                # INFO logging is synchronous on the event loop and can block
                # the 20 ms media path behind Docker's log driver.
                logger.debug("Gemini input transcription: %s", text)

        output_transcription = getattr(content, "output_transcription", None)
        if output_transcription is not None:
            text = str(getattr(output_transcription, "text", "") or "")
            if text:
                output_transcript += text
                self._output_transcripts[generation] = output_transcript
                if generation == self._generation:
                    self.output_transcript = output_transcript
                boundary = self._boundary_kind(text)
                if boundary is not None:
                    await self.output_boundary_queue.put((generation, boundary))
                logger.debug("Gemini output transcription: %s", text)

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
                        # interruption event releases the audio marker, after
                        # which audio from the new generation is accepted.
                        continue
                    self._response_open_generation = generation
                    now = time.monotonic()
                    previous_packet_at = self._last_audio_packet_at.get(
                        generation
                    )
                    if previous_packet_at is not None:
                        gap_ms = (now - previous_packet_at) * 1000.0
                        if gap_ms >= 200.0:
                            self.timeline.add(
                                "GEMINI_AUDIO_PACKET_GAP",
                                generation=generation,
                                gap_ms=round(gap_ms, 1),
                                bytes=len(pcm),
                            )
                    self._last_audio_packet_at[generation] = now
                    if generation not in self._first_audio_seen_for_generation:
                        self._first_audio_seen_for_generation.add(generation)
                        first_audio_events = getattr(
                            self, "_first_audio_events", None
                        )
                        if first_audio_events is None:
                            first_audio_events = {}
                            self._first_audio_events = first_audio_events
                        first_audio_events.setdefault(
                            generation, asyncio.Event()
                        ).set()
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
            if getattr(self, "_pending_audio_generation", None) == generation:
                self._pending_audio_generation = None
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


    async def wait_for_first_audio(self, generation: int, timeout: float) -> bool:
        event = self._first_audio_events.setdefault(generation, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.01, timeout))
        except TimeoutError:
            return False
        return True

    def transcript_for_generation(self, generation: int) -> str:
        return str(self._output_transcripts.get(generation, "") or "").strip()

    @staticmethod
    def _boundary_kind(text: str) -> str | None:
        stripped = str(text or "").rstrip()
        if not stripped:
            return None
        if stripped.endswith(("?", "？")):
            return "question"
        if stripped.endswith((".", "!", "…", "。", "！")):
            return "medium"
        if stripped.endswith((",", ";", ":", "—")):
            return "short"
        return None

    async def _handle_tool_call(self, tool_call: Any) -> None:
        """Acknowledge Gemini Live function calls and retain the latest outcome."""
        self._ensure_connected()
        from google.genai import types

        function_calls = getattr(tool_call, "function_calls", None) or []
        responses = []
        for function_call in function_calls:
            name = str(getattr(function_call, "name", "") or "")
            call_id = str(getattr(function_call, "id", "") or "")
            args = getattr(function_call, "args", None) or {}
            definition = OUTCOME_BY_TOOL.get(name)
            if definition is None:
                response_payload = {
                    "accepted": False,
                    "error": "unknown_outcome_tool",
                }
                self.timeline.add(
                    "GEMINI_OUTCOME_TOOL_UNKNOWN",
                    tool_name=name,
                    tool_call_id=call_id,
                )
            else:
                evidence = ""
                if isinstance(args, dict):
                    evidence = str(args.get("evidence") or "").strip()[:1000]
                self.classified_outcome = definition.key
                self.classified_evidence = evidence
                self.outcome_history.append(
                    {
                        "outcome": definition.key,
                        "tool": name,
                        "evidence": evidence,
                    }
                )
                response_payload = {
                    "accepted": True,
                    "outcome": definition.key,
                }
                self.timeline.add(
                    "GEMINI_OUTCOME_CLASSIFIED",
                    outcome=definition.key,
                    label=definition.label,
                    tool_name=name,
                    evidence=evidence,
                )
            response_kwargs: dict[str, Any] = {
                "name": name,
                "response": response_payload,
            }
            if call_id:
                response_kwargs["id"] = call_id
            responses.append(types.FunctionResponse(**response_kwargs))
        if responses:
            async with self._send_lock:
                await self.session.send_tool_response(
                    function_responses=responses
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
