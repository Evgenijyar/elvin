"""Second Gemini Live session acting as a non-authoritative conversation director."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any

from elvin.integrations.gemini import GEMINI_LIVE_MODEL_ID
from elvin.observability.timeline import CallTimeline
from elvin.services.conversation_effects import phrases_from_value

logger = logging.getLogger("elvin.gemini_director")


@dataclass(frozen=True, slots=True)
class DirectorInterruptionDecision:
    generation: int
    intent: str
    confidence: float
    resume_policy: str
    evidence: str = ""


@dataclass(frozen=True, slots=True)
class DirectorTurnPlan:
    generation: int
    response_delay_ms: int
    pace_percent: float
    micro_pause_style: str
    user_state: str
    confidence: float


@dataclass(frozen=True, slots=True)
class DirectorAudioPacket:
    generation: int
    utterance_id: int
    kind: str
    phrase: str
    pcm24: bytes
    volume_percent: int
    max_audio_ms: int
    final: bool = False


def _effect_enabled(config: dict[str, dict[str, Any]], key: str) -> bool:
    return bool(config.get(key, {}).get("enabled"))


def _director_tools(config: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    if _effect_enabled(config, "semantic_interruption") or _effect_enabled(
        config, "interruption_resume"
    ):
        tools.append(
            {
                "name": "report_interruption_intent",
                "description": (
                    "Немедленно классифицировать пересечение речи клиента и Актёра. "
                    "BACKCHANNEL означает короткое подтверждение без захвата реплики; "
                    "TAKEOVER — клиент хочет говорить; NOISE — не речь; UNCERTAIN — "
                    "недостаточно данных."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "intent": {
                            "type": "STRING",
                            "enum": ["BACKCHANNEL", "TAKEOVER", "NOISE", "UNCERTAIN"],
                        },
                        "confidence": {"type": "NUMBER"},
                        "resume_policy": {
                            "type": "STRING",
                            "enum": ["RESUME", "REFORMULATE", "DISCARD"],
                        },
                        "evidence": {"type": "STRING"},
                    },
                    "required": ["intent", "confidence", "resume_policy"],
                },
            }
        )
    if any(
        _effect_enabled(config, key)
        for key in ("adaptive_response_delay", "pace_matching", "micro_pauses")
    ):
        tools.append(
            {
                "name": "report_turn_plan",
                "description": (
                    "После законченной реплики клиента вернуть один план следующего "
                    "ответа: естественную задержку, темп Актёра и интенсивность пауз."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "response_delay_ms": {"type": "INTEGER"},
                        "pace_percent": {"type": "NUMBER"},
                        "micro_pause_style": {
                            "type": "STRING",
                            "enum": ["NONE", "LIGHT", "MEDIUM"],
                        },
                        "user_state": {
                            "type": "STRING",
                            "enum": [
                                "NEUTRAL",
                                "ENGAGED",
                                "THINKING",
                                "IRRITATED",
                                "UPSET",
                            ],
                        },
                        "confidence": {"type": "NUMBER"},
                    },
                    "required": [
                        "response_delay_ms",
                        "pace_percent",
                        "micro_pause_style",
                        "user_state",
                        "confidence",
                    ],
                },
            }
        )
    if _effect_enabled(config, "listener_backchannels"):
        phrases = phrases_from_value(config["listener_backchannels"].get("phrases"))
        tools.append(
            {
                "name": "request_backchannel",
                "description": (
                    "Попросить разрешение тихо произнести одну короткую реакцию "
                    "слушателя во время длинной реплики клиента. Допустимы только: "
                    + ", ".join(phrases)
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "phrase": {"type": "STRING", "enum": phrases or ["угу"]},
                        "confidence": {"type": "NUMBER"},
                        "reason": {"type": "STRING"},
                    },
                    "required": ["phrase", "confidence"],
                },
            }
        )
    if _effect_enabled(config, "latency_fillers"):
        phrases = phrases_from_value(config["latency_fillers"].get("phrases"))
        tools.append(
            {
                "name": "request_latency_filler",
                "description": (
                    "По запросу backend выбрать один короткий нейтральный филлер. "
                    "Допустимы только: " + ", ".join(phrases)
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "phrase": {"type": "STRING", "enum": phrases or ["секунду"]},
                        "confidence": {"type": "NUMBER"},
                        "reason": {"type": "STRING"},
                    },
                    "required": ["phrase", "confidence"],
                },
            }
        )
    return tools


def build_director_instruction(
    robot: dict[str, Any], config: dict[str, dict[str, Any]]
) -> str:
    actor_role = str(robot.get("role_prompt") or "").strip()
    enabled = [key for key, value in config.items() if value.get("enabled")]
    return "\n\n".join(
        [
            "Ты — Режиссёр второго контура телефонного робота Элвин.",
            "Клиент тебя не видит и не должен слышать, кроме явно разрешённых коротких backchannel или latency-filler реплик.",
            "Актёр — отдельная Gemini Live-сессия, которая ведёт разговор и генерирует основной голос.",
            "Твоя задача — в реальном времени анализировать аудио клиента и служебные текстовые сообщения backend, вызывая только объявленные tools.",
            "Не пересказывай разговор и не произноси советы голосом. Для решений всегда используй tools.",
            "При пересечении речи срочно вызови report_interruption_intent. Не считай короткое «угу/ага/да» захватом реплики, если человек не продолжает мысль.",
            "После завершения содержательной реплики клиента вызови report_turn_plan ровно один раз, если этот tool объявлен.",
            "request_backchannel вызывай редко, только в естественной микропаузе длинной реплики и только разрешённой фразой.",
            "request_latency_filler вызывай только после явного служебного запроса backend.",
            "После положительного FunctionResponse для request_backchannel или request_latency_filler произнеси только одобренную фразу и сразу закончи ответ.",
            "Если FunctionResponse отрицательный — ничего не произноси.",
            "Все значения confidence должны быть от 0 до 1.",
            "ВКЛЮЧЁННЫЕ ЭФФЕКТЫ: " + (", ".join(enabled) if enabled else "нет"),
            "СЦЕНАРИЙ АКТЁРА ДЛЯ КОНТЕКСТА:\n" + actor_role[:20_000],
        ]
    )


class GeminiDirectorSession:
    """Advisory Live session. It never changes LPTracker or Actor state directly."""

    def __init__(
        self,
        *,
        api_key: str,
        robot: dict[str, Any],
        effects_config: dict[str, dict[str, Any]],
        timeline: CallTimeline,
        connect_timeout_seconds: float = 20.0,
    ) -> None:
        self.api_key = api_key
        self.robot = robot
        self.effects_config = effects_config
        self.timeline = timeline
        self.connect_timeout_seconds = connect_timeout_seconds
        self.client: Any = None
        self.session: Any = None
        self._connection_cm: Any = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._closed = False
        self._send_lock = asyncio.Lock()
        self._generation = 0
        self._activity_open = False
        self._interruption_events: dict[int, asyncio.Event] = {}
        self._turn_plan_events: dict[int, asyncio.Event] = {}
        self._turn_complete_events: dict[int, asyncio.Event] = {}
        self._interruption_decisions: dict[int, DirectorInterruptionDecision] = {}
        self._turn_plans: dict[int, DirectorTurnPlan] = {}
        self._audio_grant: dict[str, Any] | None = None
        self._audio_grant_bytes = 0
        self._audio_utterance_sequence = 0
        self._pending_actor_transcripts: deque[str] = deque(maxlen=6)
        self.output_audio: asyncio.Queue[DirectorAudioPacket] = asyncio.Queue(
            maxsize=100
        )
        self.receive_error: BaseException | None = None
        self.receive_failed = asyncio.Event()
        self._live_config: dict[str, Any] | None = None
        self._live_model = ""
        self._resumption_handle: str | None = None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def activity_open(self) -> bool:
        return self._activity_open

    async def connect(self) -> None:
        if self.session is not None:
            return
        from google import genai

        voice = str(self.robot.get("voice_name") or "Kore")
        # Director is a deterministic classifier/planner, not a creative
        # conversational persona. Keep its sampling independent from the
        # Actor temperature configured by the operator.
        temperature = 0.1
        tools = _director_tools(self.effects_config)
        config: dict[str, Any] = {
            "response_modalities": ["AUDIO"],
            "temperature": temperature,
            "max_output_tokens": 256,
            "system_instruction": build_director_instruction(
                self.robot, self.effects_config
            ),
            "speech_config": {
                "voice_config": {"prebuilt_voice_config": {"voice_name": voice}}
            },
            "realtime_input_config": {
                "automatic_activity_detection": {"disabled": True},
                "activity_handling": "START_OF_ACTIVITY_INTERRUPTS",
            },
            "thinking_config": {"thinking_level": "minimal"},
            "context_window_compression": {"sliding_window": {}},
            "session_resumption": {},
        }
        if tools:
            config["tools"] = [{"function_declarations": tools}]
        self.timeline.add(
            "DIRECTOR_CONNECT_START",
            model=GEMINI_LIVE_MODEL_ID,
            voice=voice,
            tools=[tool["name"] for tool in tools],
        )
        self.client = genai.Client(
            api_key=self.api_key,
            http_options={"api_version": "v1beta"},
        )
        self._connection_cm = self.client.aio.live.connect(
            model=GEMINI_LIVE_MODEL_ID,
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
        self._live_model = GEMINI_LIVE_MODEL_ID
        self._live_config = config
        self.timeline.add("DIRECTOR_SETUP_COMPLETE", model=GEMINI_LIVE_MODEL_ID)
        self._receiver_task = asyncio.create_task(
            self._receive_loop(),
            name=f"gemini-director-{self.timeline.call_id}",
        )

    async def start_activity(self, *, actor_speaking: bool = False) -> int | None:
        self._ensure_connected()
        from google.genai import types

        if self._activity_open:
            self.timeline.add(
                "DIRECTOR_ACTIVITY_SKIPPED",
                generation=self._generation,
                reason="activity_already_open",
            )
            return None
        previous = self._generation
        previous_complete = self._turn_complete_events.get(previous)
        if (
            previous > 0
            and previous_complete is not None
            and not previous_complete.is_set()
        ):
            # A Gemini 3.1 function call is synchronous. Starting another
            # activity before turnComplete would interrupt the old tool turn
            # and make its unlabelled server messages look like the new
            # generation. Skipping advisory analysis is safer than corrupting
            # Actor timing or playing stale auxiliary audio.
            self.timeline.add(
                "DIRECTOR_ACTIVITY_SKIPPED",
                generation=previous,
                reason="previous_turn_busy",
            )
            return None
        self._generation += 1
        generation = self._generation
        self._activity_open = True
        self._interruption_events[generation] = asyncio.Event()
        self._turn_plan_events[generation] = asyncio.Event()
        self._turn_complete_events[generation] = asyncio.Event()
        self._audio_grant = None
        self._audio_grant_bytes = 0
        async with self._send_lock:
            await self.session.send_realtime_input(activity_start=types.ActivityStart())
            while self._pending_actor_transcripts:
                await self.session.send_realtime_input(
                    text=(
                        "[ТРАНСКРИПЦИЯ ПРЕДЫДУЩЕЙ РЕПЛИКИ АКТЁРА] "
                        + self._pending_actor_transcripts.popleft()
                    )
                )
            if actor_speaking:
                await self.session.send_realtime_input(
                    text=(
                        "[СЛУЖЕБНОЕ СОСТОЯНИЕ] Клиент начал говорить поверх "
                        "текущей реплики Актёра. Срочно классифицируй пересечение."
                    )
                )
        self.timeline.add(
            "DIRECTOR_ACTIVITY_START",
            generation=generation,
            actor_speaking=actor_speaking,
        )
        return generation

    async def send_audio(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        self._ensure_connected()
        from google.genai import types

        async with self._send_lock:
            await self.session.send_realtime_input(
                audio=types.Blob(data=pcm16, mime_type="audio/pcm;rate=16000")
            )

    async def end_activity(self) -> None:
        if not self._activity_open:
            return
        self._ensure_connected()
        from google.genai import types

        generation = self._generation
        async with self._send_lock:
            await self.session.send_realtime_input(activity_end=types.ActivityEnd())
        self._activity_open = False
        self.timeline.add("DIRECTOR_ACTIVITY_END", generation=generation)

    async def send_actor_transcript(self, text: str) -> None:
        cleaned = str(text or "").strip()
        if not cleaned or self.session is None or self._closed:
            return
        # Realtime text sent outside an explicit manual-VAD activity is not a
        # complete turn. Keep it locally and attach it to the next Director
        # activity so it cannot surface many seconds later in an unrelated
        # tool call.
        self._pending_actor_transcripts.append(cleaned[:4000])

    async def mark_midturn_pause(self) -> None:
        if self.session is None or self._closed or not self._activity_open:
            return
        async with self._send_lock:
            await self.session.send_realtime_input(
                text=(
                    "[СЛУЖЕБНОЕ СОСТОЯНИЕ] Обнаружена пауза-кандидат внутри "
                    "длинной реплики клиента. Не вызывай report_turn_plan. "
                    "Вызывай request_backchannel только если по смыслу фраза "
                    "явно не закончена и короткая реакция действительно уместна. "
                    "Backend воспроизведёт её лишь после подтверждения, что "
                    "клиент продолжил говорить."
                )
            )

    async def request_latency_filler(self) -> int | None:
        if not _effect_enabled(self.effects_config, "latency_fillers"):
            return None
        self._ensure_connected()
        phrases = phrases_from_value(
            self.effects_config["latency_fillers"].get("phrases")
        )
        previous = self._generation
        if previous > 0:
            complete = await self.wait_for_turn_complete(previous, 500)
            if not complete:
                self.timeline.add(
                    "DIRECTOR_FILLER_SKIPPED",
                    generation=previous,
                    reason="previous_turn_busy",
                )
                return None
        generation = await self.start_activity(actor_speaking=False)
        if generation is None:
            return None
        try:
            async with self._send_lock:
                await self.session.send_realtime_input(
                    text=(
                        "[СЛУЖЕБНЫЙ ЗАПРОС] Актёр ещё не начал ответ. Если сейчас "
                        "социально уместен короткий филлер, вызови "
                        "request_latency_filler и выбери только из списка: "
                        + ", ".join(phrases)
                        + ". Иначе не вызывай ничего."
                    )
                )
        finally:
            await self.end_activity()
        self.timeline.add(
            "DIRECTOR_FILLER_ACTIVITY",
            generation=generation,
        )
        return generation

    async def wait_for_interruption(
        self, generation: int, timeout_ms: int
    ) -> DirectorInterruptionDecision | None:
        event = self._interruption_events.setdefault(generation, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.01, timeout_ms / 1000))
        except TimeoutError:
            return None
        return self._interruption_decisions.get(generation)

    async def wait_for_turn_complete(self, generation: int, timeout_ms: int) -> bool:
        event = self._turn_complete_events.setdefault(generation, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.01, timeout_ms / 1000))
        except TimeoutError:
            return False
        return True

    async def wait_for_turn_plan(
        self, generation: int, timeout_ms: int
    ) -> DirectorTurnPlan | None:
        event = self._turn_plan_events.setdefault(generation, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.01, timeout_ms / 1000))
        except TimeoutError:
            return None
        return self._turn_plans.get(generation)

    async def _receive_loop(self) -> None:
        try:
            while not self._closed and self.session is not None:
                received_any = False
                resume_requested = False
                async for response in self.session.receive():
                    received_any = True
                    update = getattr(response, "session_resumption_update", None)
                    if update is not None:
                        resumable = bool(getattr(update, "resumable", False))
                        handle = str(getattr(update, "new_handle", "") or "")
                        if resumable and handle:
                            self._resumption_handle = handle
                            self.timeline.add("DIRECTOR_RESUMPTION_TOKEN")
                        continue
                    go_away = getattr(response, "go_away", None)
                    if go_away is not None:
                        time_left = getattr(go_away, "time_left", None)
                        self.timeline.add(
                            "DIRECTOR_GO_AWAY",
                            time_left=(
                                str(time_left) if time_left is not None else None
                            ),
                        )
                        if self._resumption_handle:
                            resume_requested = True
                            break
                        self.receive_error = RuntimeError(
                            "Gemini Director session is ending without a "
                            "resumption handle"
                        )
                        self.receive_failed.set()
                        return
                    await self._handle_response(response)
                    if self._closed:
                        return
                if resume_requested:
                    await self._resume_connection()
                    continue
                if not received_any:
                    await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                self.receive_error = exc
                self.receive_failed.set()
                self.timeline.add(
                    "DIRECTOR_RECEIVE_ERROR",
                    error=f"{type(exc).__name__}: {exc}",
                )
                logger.exception("Gemini Director receive loop failed")

    async def _resume_connection(self) -> None:
        handle = self._resumption_handle
        config = self._live_config
        if not handle or config is None or self.client is None:
            raise RuntimeError("Gemini Director session cannot be resumed")
        resumed_config = {
            **config,
            "session_resumption": {"handle": handle},
        }
        async with self._send_lock:
            old_connection = self._connection_cm
            if old_connection is not None:
                await old_connection.__aexit__(None, None, None)
            connection = self.client.aio.live.connect(
                model=self._live_model,
                config=resumed_config,
            )
            session = await asyncio.wait_for(
                connection.__aenter__(),
                timeout=self.connect_timeout_seconds,
            )
            self._connection_cm = connection
            self.session = session
            self._live_config = resumed_config
        self.timeline.add("DIRECTOR_SESSION_RESUMED")

    async def _handle_response(self, response: Any) -> None:
        tool_call = getattr(response, "tool_call", None)
        if tool_call is not None:
            await self._handle_tool_call(tool_call)
        content = getattr(response, "server_content", None)
        if content is None:
            return
        model_turn = getattr(content, "model_turn", None)
        parts = getattr(model_turn, "parts", None) if model_turn else None
        if parts and self._audio_grant is not None:
            for part in parts:
                inline = getattr(part, "inline_data", None)
                pcm = getattr(inline, "data", None) if inline else None
                if isinstance(pcm, memoryview):
                    pcm = pcm.tobytes()
                if isinstance(pcm, bytearray):
                    pcm = bytes(pcm)
                if not isinstance(pcm, bytes) or not pcm:
                    continue
                grant = self._audio_grant
                max_bytes = int(24_000 * 2 * grant["max_audio_ms"] / 1000)
                remaining = max(0, max_bytes - self._audio_grant_bytes)
                if remaining <= 0:
                    continue
                pcm = pcm[:remaining]
                self._audio_grant_bytes += len(pcm)
                await self.output_audio.put(
                    DirectorAudioPacket(
                        generation=self._generation,
                        utterance_id=int(grant["utterance_id"]),
                        kind=str(grant["kind"]),
                        phrase=str(grant["phrase"]),
                        pcm24=pcm,
                        volume_percent=int(grant["volume_percent"]),
                        max_audio_ms=int(grant["max_audio_ms"]),
                    )
                )
        if bool(getattr(content, "turn_complete", False)):
            generation = self._generation
            grant = self._audio_grant
            if grant is not None:
                await self.output_audio.put(
                    DirectorAudioPacket(
                        generation=self._generation,
                        utterance_id=int(grant["utterance_id"]),
                        kind=str(grant["kind"]),
                        phrase=str(grant["phrase"]),
                        pcm24=b"",
                        volume_percent=int(grant["volume_percent"]),
                        max_audio_ms=int(grant["max_audio_ms"]),
                        final=True,
                    )
                )
            self._audio_grant = None
            self._audio_grant_bytes = 0
            self._turn_complete_events.setdefault(generation, asyncio.Event()).set()

    async def _handle_tool_call(self, tool_call: Any) -> None:
        self._ensure_connected()
        from google.genai import types

        responses = []
        for call in getattr(tool_call, "function_calls", None) or []:
            name = str(getattr(call, "name", "") or "")
            call_id = str(getattr(call, "id", "") or "")
            args = getattr(call, "args", None) or {}
            if not isinstance(args, dict):
                args = {}
            payload: dict[str, Any]
            if name == "report_interruption_intent":
                decision = DirectorInterruptionDecision(
                    generation=self._generation,
                    intent=str(args.get("intent") or "UNCERTAIN").upper(),
                    confidence=max(0.0, min(1.0, float(args.get("confidence") or 0))),
                    resume_policy=str(args.get("resume_policy") or "DISCARD").upper(),
                    evidence=str(args.get("evidence") or "")[:500],
                )
                self._interruption_decisions[self._generation] = decision
                self._interruption_events.setdefault(
                    self._generation, asyncio.Event()
                ).set()
                payload = {
                    "accepted": True,
                    "instruction": "Заверши ответ без аудио.",
                }
                self.timeline.add(
                    "DIRECTOR_INTERRUPTION_DECISION",
                    generation=self._generation,
                    intent=decision.intent,
                    confidence=decision.confidence,
                    resume_policy=decision.resume_policy,
                )
            elif name == "report_turn_plan":
                plan = DirectorTurnPlan(
                    generation=self._generation,
                    response_delay_ms=max(
                        0, min(2500, int(args.get("response_delay_ms") or 0))
                    ),
                    pace_percent=max(
                        70.0, min(140.0, float(args.get("pace_percent") or 100))
                    ),
                    micro_pause_style=str(
                        args.get("micro_pause_style") or "NONE"
                    ).upper(),
                    user_state=str(args.get("user_state") or "NEUTRAL").upper(),
                    confidence=max(0.0, min(1.0, float(args.get("confidence") or 0))),
                )
                self._turn_plans[self._generation] = plan
                self._turn_plan_events.setdefault(
                    self._generation, asyncio.Event()
                ).set()
                payload = {
                    "accepted": True,
                    "instruction": "Заверши ответ без аудио.",
                }
                self.timeline.add(
                    "DIRECTOR_TURN_PLAN",
                    generation=self._generation,
                    response_delay_ms=plan.response_delay_ms,
                    pace_percent=plan.pace_percent,
                    micro_pause_style=plan.micro_pause_style,
                    user_state=plan.user_state,
                    confidence=plan.confidence,
                )
            elif name in {"request_backchannel", "request_latency_filler"}:
                kind = "backchannel" if name == "request_backchannel" else "filler"
                effect_key = (
                    "listener_backchannels"
                    if kind == "backchannel"
                    else "latency_fillers"
                )
                effect = self.effects_config.get(effect_key, {})
                allowed = phrases_from_value(effect.get("phrases"))
                phrase = str(args.get("phrase") or "").strip()
                confidence = max(0.0, min(1.0, float(args.get("confidence") or 0)))
                threshold = float(effect.get("confidence", 0.0))
                accepted = bool(
                    effect.get("enabled")
                    and phrase in allowed
                    and confidence >= threshold
                )
                if accepted:
                    self._audio_utterance_sequence += 1
                    self._audio_grant = {
                        "utterance_id": self._audio_utterance_sequence,
                        "kind": kind,
                        "phrase": phrase,
                        "volume_percent": int(effect.get("volume_percent", 50)),
                        "max_audio_ms": int(effect.get("max_audio_ms", 900)),
                    }
                    self._audio_grant_bytes = 0
                payload = {
                    "accepted": accepted,
                    "approved_phrase": phrase if accepted else "",
                    "instruction": (
                        "Произнеси только approved_phrase и сразу закончи ответ."
                        if accepted
                        else "Ничего не произноси."
                    ),
                }
                self.timeline.add(
                    "DIRECTOR_AUDIO_REQUEST",
                    kind=kind,
                    phrase=phrase,
                    confidence=confidence,
                    accepted=accepted,
                )
            else:
                payload = {"accepted": False, "error": "unknown_director_tool"}
            kwargs: dict[str, Any] = {"name": name, "response": payload}
            if call_id:
                kwargs["id"] = call_id
            responses.append(types.FunctionResponse(**kwargs))
        if responses:
            async with self._send_lock:
                await self.session.send_tool_response(function_responses=responses)

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
                logger.exception("Failed to close Gemini Director session")
        self.session = None
        self._connection_cm = None
        self.client = None
        self.timeline.add("DIRECTOR_SESSION_CLOSED")

    def _ensure_connected(self) -> None:
        if self.session is None or self._closed:
            raise RuntimeError("Gemini Director session is not connected")
        if self.receive_error is not None:
            raise RuntimeError(
                "Gemini Director receiver failed: "
                f"{type(self.receive_error).__name__}: {self.receive_error}"
            ) from self.receive_error
