import asyncio
from collections import deque
from types import SimpleNamespace

from elvin.media.asterisk_bridge import (
    AsteriskGeminiBridge,
    AsteriskMediaInfo,
    AsteriskProtocol,
    BackchannelOpportunity,
    InterruptionCandidate,
)
from elvin.media.background_audio import LoopingBackgroundAudio
from elvin.services.conversation_effects import default_effects_config


class _FakeWebSocket:
    async def send_bytes(self, _payload: bytes) -> None:
        return None

    async def send_text(self, _payload: str) -> None:
        return None


class _FakeTimeline:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def add(self, name: str, **payload: object) -> None:
        self.events.append((name, payload))


class _FakeWriter:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def submit(self, chunk: bytes) -> None:
        self.chunks.append(chunk)


def _bridge() -> tuple[AsteriskGeminiBridge, _FakeWebSocket, _FakeTimeline]:
    websocket = _FakeWebSocket()
    timeline = _FakeTimeline()
    protocol = SimpleNamespace(
        info=AsteriskMediaInfo(optimal_frame_size=640),
    )
    # Replace the coroutine with a recorder after construction; keeping the
    # protocol object tiny makes this test independent of FastAPI/Starlette.
    sent: list[bytes] = []

    async def send_media(
        pcm: bytes,
        *,
        generation: int | None = None,
    ) -> bool:
        sent.append(pcm)
        return True

    protocol.send_media = send_media
    call = SimpleNamespace(
        timeline=timeline,
        protocol_sent=sent,
        detector=SimpleNamespace(set_bot_speaking=lambda _value: None),
        bot_audio=_FakeWriter(),
        gemini=SimpleNamespace(generation=1),
    )
    bridge = object.__new__(AsteriskGeminiBridge)
    bridge.call = call
    bridge.protocol = protocol
    bridge._output_buffer = bytearray()
    bridge._output_buffer_lock = asyncio.Lock()
    bridge._first_output_generation = set()
    return bridge, websocket, timeline


def test_output_audio_is_framed_and_tail_is_padded() -> None:
    bridge, _websocket, _timeline = _bridge()

    async def exercise() -> None:
        await bridge._send_output_audio(b"a" * 1000)
        assert len(bridge.call.protocol_sent) == 1
        assert len(bridge.call.protocol_sent[0]) == 640
        assert len(bridge._output_buffer) == 360

        await bridge._send_output_audio(b"b" * 280)
        assert len(bridge.call.protocol_sent) == 2
        assert len(bridge.call.protocol_sent[1]) == 640
        assert len(bridge._output_buffer) == 0

        await bridge._send_output_audio(b"c" * 100, flush=True)
        assert len(bridge.call.protocol_sent) == 3
        assert len(bridge.call.protocol_sent[2]) == 640
        assert bridge.call.protocol_sent[2][-540:] == b"\x00" * 540

    asyncio.run(exercise())


def test_no_background_preserves_exact_outbound_pcm() -> None:
    bridge, _websocket, _timeline = _bridge()
    original = bytes(range(256)) * 5

    async def exercise() -> None:
        await bridge._send_output_audio(original)

    asyncio.run(exercise())

    assert bridge.call.protocol_sent == [original]
    assert bridge.call.bot_audio.chunks == [original]


def test_background_is_mixed_only_on_wire_and_not_in_echo_guard() -> None:
    bridge, _websocket, _timeline = _bridge()
    original = b"\x10\x00" * 320
    noted_playback: list[bytes] = []
    bridge.background_audio = LoopingBackgroundAudio(
        b"\x20\x00" * 320,
        volume_percent=100,
    )
    bridge._voice_submission_active = asyncio.Event()
    bridge.echo_guard = SimpleNamespace(note_playback=noted_playback.append)

    async def exercise() -> None:
        await bridge._send_output_audio(original)

    asyncio.run(exercise())

    assert bridge.call.protocol_sent[0] != original
    assert len(bridge.call.protocol_sent[0]) == len(original)
    assert bridge.call.bot_audio.chunks == bridge.call.protocol_sent
    assert noted_playback == [original]


def test_protocol_accepts_json_and_legacy_events() -> None:
    protocol = AsteriskProtocol(
        _FakeWebSocket(), SimpleNamespace(timeline=_FakeTimeline())
    )

    assert (
        protocol.parse_text(
            '{"event":"MEDIA_START","format":"slin16","optimal_frame_size":640}'
        )["event"]
        == "MEDIA_START"
    )
    legacy = protocol.parse_text(
        "MEDIA_START format:slin16 optimal_frame_size:640 ptime:20"
    )
    assert legacy["event"] == "MEDIA_START"
    assert legacy["optimal_frame_size"] == "640"


def test_pending_turn_is_serialized_and_chunked() -> None:
    sent_to_gemini: list[bytes] = []
    activity_calls: list[str] = []

    class _FakeGemini:
        response_open_generation = 7
        generation = 7

        async def wait_for_response_idle(self, *, timeout: float) -> None:
            assert timeout == 12.0
            self.response_open_generation = None

        async def start_activity(self) -> None:
            activity_calls.append("start")
            self.generation += 1

        async def send_audio(self, pcm: bytes) -> None:
            sent_to_gemini.append(pcm)

        async def end_activity(self) -> None:
            activity_calls.append("end")

    timeline = _FakeTimeline()
    gemini = _FakeGemini()
    bridge = object.__new__(AsteriskGeminiBridge)
    bridge.call = SimpleNamespace(
        gemini=gemini,
        timeline=timeline,
        detector=SimpleNamespace(
            bot_speaking=False,
            set_bot_speaking=lambda _value: None,
        ),
    )
    bridge.protocol = SimpleNamespace(
        command=_async_noop,
    )
    bridge.echo_guard = SimpleNamespace(clear=lambda: None)
    bridge.resampler = SimpleNamespace(reset=lambda: None)
    bridge._output_buffer = bytearray()
    bridge._output_buffer_lock = asyncio.Lock()
    bridge._pending_turns = deque([b"x" * 3_000])
    bridge._closed = False
    bridge._pending_drain_active = False
    bridge._pending_drain_audio = None

    async def exercise() -> None:
        await bridge._drain_pending_turns()

    asyncio.run(exercise())

    assert activity_calls == ["start", "end"]
    assert [len(chunk) for chunk in sent_to_gemini] == [1280, 1280, 440]
    assert bridge._pending_turns == deque()
    assert any(name == "PENDING_TURN_SENT" for name, _ in timeline.events)


def test_standalone_soft_interruption_commits_after_local_confirmation() -> None:
    bridge = object.__new__(AsteriskGeminiBridge)
    config = default_effects_config(enabled=False)
    config["natural_interruption"]["enabled"] = True
    config["natural_interruption"]["confirm_ms"] = 40
    bridge.effects_config = config
    bridge.director = None
    bridge.call = SimpleNamespace(timeline=_FakeTimeline())
    resolutions: list[str] = []

    async def commit(candidate: InterruptionCandidate, *, reason: str) -> None:
        resolutions.append(reason)
        candidate.committed = True
        candidate.done.set()

    async def reject(candidate: InterruptionCandidate, *, reason: str) -> None:
        resolutions.append(reason)
        candidate.done.set()

    bridge._commit_interruption_candidate = commit
    bridge._reject_interruption_candidate = reject

    async def exercise() -> None:
        candidate = InterruptionCandidate(
            director_generation=0,
            interrupted_actor_generation=1,
            started_at=asyncio.get_running_loop().time(),
        )
        await bridge._resolve_interruption_candidate(candidate)

    asyncio.run(exercise())
    assert resolutions == ["local_vad_confirmed"]


def test_semantic_backchannel_resumes_paused_actor_media() -> None:
    commands: list[str] = []
    bridge = object.__new__(AsteriskGeminiBridge)
    config = default_effects_config(enabled=False)
    config["semantic_interruption"]["enabled"] = True
    config["natural_interruption"]["confirm_ms"] = 40
    config["semantic_interruption"]["decision_timeout_ms"] = 100
    bridge.effects_config = config
    bridge.call = SimpleNamespace(timeline=_FakeTimeline())

    async def command(name: str, **_kwargs: object) -> None:
        commands.append(name)

    bridge.protocol = SimpleNamespace(command=command)
    bridge._interruption_lock = asyncio.Lock()
    bridge._duck_current_db = 0.0
    bridge._duck_target_db = 0.0
    bridge._duck_transition_remaining_ms = 0.0
    decision = SimpleNamespace(
        intent="BACKCHANNEL",
        confidence=0.99,
        resume_policy="RESUME",
    )
    bridge.director = SimpleNamespace(wait_for_interruption=_async_result(decision))

    async def commit(candidate: InterruptionCandidate, *, reason: str) -> None:
        raise AssertionError(f"unexpected commit: {reason}")

    bridge._commit_interruption_candidate = commit

    async def exercise() -> None:
        now = asyncio.get_running_loop().time()
        candidate = InterruptionCandidate(
            director_generation=1,
            interrupted_actor_generation=1,
            started_at=now - 0.2,
            speech_ended_at=now,
            media_paused=True,
        )
        candidate.speech_ended.set()
        await bridge._resolve_interruption_candidate(candidate)
        assert candidate.resolution == "backchannel"

    asyncio.run(exercise())
    assert commands == ["CONTINUE_MEDIA"]


def test_backchannel_audio_requires_caller_to_resume() -> None:
    bridge = object.__new__(AsteriskGeminiBridge)
    bridge.effects_config = default_effects_config(enabled=False)
    bridge.effects_config["listener_backchannels"]["enabled"] = True
    bridge._backchannel_opportunities = {}
    bridge.call = SimpleNamespace(timeline=_FakeTimeline())

    async def exercise() -> None:
        opportunity = BackchannelOpportunity(
            generation=7,
            created_at=asyncio.get_running_loop().time(),
        )
        bridge._backchannel_opportunities[7] = opportunity
        wait = asyncio.create_task(bridge._await_backchannel_confirmation(7))
        await asyncio.sleep(0)
        opportunity.confirmed = True
        opportunity.decision.set()
        assert await wait

        rejected = BackchannelOpportunity(
            generation=8,
            created_at=asyncio.get_running_loop().time(),
            rejected=True,
        )
        rejected.decision.set()
        bridge._backchannel_opportunities[8] = rejected
        assert not await bridge._await_backchannel_confirmation(8)

    asyncio.run(exercise())


def test_stale_latency_filler_is_rejected_after_actor_generation_changes() -> None:
    bridge = object.__new__(AsteriskGeminiBridge)
    config = default_effects_config(enabled=False)
    config["latency_fillers"]["enabled"] = True
    bridge.effects_config = config
    bridge._active_filler_actor_generation = 1
    bridge._filler_requested = {1}
    bridge._fillers_played_by_generation = {1: 0}
    bridge.call = SimpleNamespace(
        detector=SimpleNamespace(
            turn_open=False,
            bot_speaking=False,
        ),
        gemini=SimpleNamespace(
            generation=2,
            bot_audio_active=asyncio.Event(),
        ),
    )
    packet = SimpleNamespace(kind="filler")

    async def exercise() -> None:
        assert not bridge._director_audio_allowed(packet)

    asyncio.run(exercise())


async def _async_noop(*_args: object, **_kwargs: object) -> None:
    return None


def _async_result(value: object):
    async def result(*_args: object, **_kwargs: object) -> object:
        return value

    return result
