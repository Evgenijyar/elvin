import asyncio
import sys
from types import ModuleType, SimpleNamespace

from elvin.integrations.gemini_director import (
    GeminiDirectorSession,
    _director_tools,
    build_director_instruction,
)
from elvin.services.conversation_effects import default_effects_config


class _Timeline:
    call_id = "director-test"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def add(self, name: str, **details: object) -> None:
        self.events.append((name, details))


class _FunctionResponse:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class _LiveSession:
    def __init__(self) -> None:
        self.responses: list[object] = []

    async def send_tool_response(self, *, function_responses: list[object]) -> None:
        self.responses.extend(function_responses)


def _install_fake_google() -> None:
    genai_module = ModuleType("google.genai")
    genai_module.types = SimpleNamespace(FunctionResponse=_FunctionResponse)
    google_module = ModuleType("google")
    google_module.genai = genai_module
    sys.modules["google"] = google_module
    sys.modules["google.genai"] = genai_module


def _director() -> GeminiDirectorSession:
    config = default_effects_config(enabled=True)
    director = GeminiDirectorSession(
        api_key="director-key",
        robot={"role_prompt": "Проведи квалификационный звонок", "voice_name": "Kore"},
        effects_config=config,
        timeline=_Timeline(),
    )
    director.session = _LiveSession()
    director._generation = 3
    director._interruption_events[3] = asyncio.Event()
    director._turn_plan_events[3] = asyncio.Event()
    director._turn_complete_events[3] = asyncio.Event()
    return director


def test_all_director_tools_are_declared_for_enabled_effects() -> None:
    config = default_effects_config(enabled=True)
    names = {tool["name"] for tool in _director_tools(config)}
    assert names == {
        "report_interruption_intent",
        "report_turn_plan",
        "request_backchannel",
        "request_latency_filler",
    }
    instruction = build_director_instruction({"role_prompt": "ROLE"}, config)
    assert "Актёр" in instruction
    assert "request_backchannel" in instruction


def test_interruption_tool_produces_structured_decision() -> None:
    _install_fake_google()
    director = _director()

    async def exercise() -> None:
        call = SimpleNamespace(
            name="report_interruption_intent",
            id="tool-1",
            args={
                "intent": "TAKEOVER",
                "confidence": 0.93,
                "resume_policy": "REFORMULATE",
                "evidence": "клиент задал новый вопрос",
            },
        )
        await director._handle_tool_call(SimpleNamespace(function_calls=[call]))
        decision = await director.wait_for_interruption(3, 10)
        assert decision is not None
        assert decision.intent == "TAKEOVER"
        assert decision.resume_policy == "REFORMULATE"
        assert director.session.responses

    asyncio.run(exercise())


def test_approved_backchannel_audio_is_grouped_and_finalized() -> None:
    _install_fake_google()
    director = _director()

    async def exercise() -> None:
        call = SimpleNamespace(
            name="request_backchannel",
            id="tool-2",
            args={"phrase": "угу", "confidence": 0.95},
        )
        await director._handle_tool_call(SimpleNamespace(function_calls=[call]))
        part = SimpleNamespace(inline_data=SimpleNamespace(data=b"voice"))
        await director._handle_response(
            SimpleNamespace(
                server_content=SimpleNamespace(
                    model_turn=SimpleNamespace(parts=[part]),
                    turn_complete=False,
                )
            )
        )
        await director._handle_response(
            SimpleNamespace(server_content=SimpleNamespace(turn_complete=True))
        )
        audio = await director.output_audio.get()
        final = await director.output_audio.get()
        assert audio.pcm24 == b"voice"
        assert not audio.final
        assert final.final
        assert audio.utterance_id == final.utterance_id
        assert director._turn_complete_events[3].is_set()

    asyncio.run(exercise())
