import asyncio
from collections import deque
from types import SimpleNamespace

from elvin.media.asterisk_bridge import (
    AsteriskGeminiBridge,
    AsteriskMediaInfo,
    AsteriskProtocol,
    PendingCallerTurn,
)
from elvin.media.background_audio import LoopingBackgroundAudio
from elvin.media.interruption import InterruptionPolicy
from elvin.media.interruption import LocalInterruptionGate
from elvin.media.turn_detector import TurnDecision


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


def test_flush_reopens_local_media_gate_after_xoff() -> None:
    protocol = AsteriskProtocol(
        _FakeWebSocket(), SimpleNamespace(timeline=_FakeTimeline())
    )

    async def exercise() -> None:
        protocol.handle_event({"event": "MEDIA_XOFF"})
        assert not protocol.media_allowed.is_set()
        await protocol.command("FLUSH_MEDIA")
        assert protocol.media_allowed.is_set()

    asyncio.run(exercise())


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
    bridge._pending_turns = deque(
        [PendingCallerTurn(audio=b"x" * 3_000, speech_ms=800)]
    )
    bridge._closed = False
    bridge._pending_drain_active = False
    bridge._pending_drain_turn = None

    async def exercise() -> None:
        await bridge._drain_pending_turns()

    asyncio.run(exercise())

    assert activity_calls == ["start", "end"]
    assert [len(chunk) for chunk in sent_to_gemini] == [1280, 1280, 440]
    assert bridge._pending_turns == deque()
    assert any(name == "PENDING_TURN_SENT" for name, _ in timeline.events)


def test_pending_drain_coalesces_queue_before_opening_activity() -> None:
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

        async def send_audio(self, pcm: bytes) -> None:
            sent_to_gemini.append(pcm)

        async def end_activity(self) -> None:
            activity_calls.append("end")

    timeline = _FakeTimeline()
    bridge = object.__new__(AsteriskGeminiBridge)
    bridge.call = SimpleNamespace(
        gemini=_FakeGemini(),
        timeline=timeline,
        detector=SimpleNamespace(
            bot_speaking=False,
            set_bot_speaking=lambda _value: None,
        ),
    )
    bridge.protocol = SimpleNamespace(command=_async_noop)
    bridge.echo_guard = SimpleNamespace(clear=lambda: None)
    bridge.resampler = SimpleNamespace(reset=lambda: None)
    bridge._output_buffer = bytearray()
    bridge._output_buffer_lock = asyncio.Lock()
    bridge._pending_turns = deque(
        [
            PendingCallerTurn(audio=b"a" * 1_000, speech_ms=300),
            PendingCallerTurn(audio=b"b" * 1_000, speech_ms=400),
        ]
    )
    bridge._closed = False
    bridge._pending_drain_active = False
    bridge._pending_drain_turn = None

    asyncio.run(bridge._drain_pending_turns())

    assert activity_calls == ["start", "end"]
    assert b"".join(sent_to_gemini) == b"a" * 1_000 + b"b" * 1_000
    assert sum(name == "PENDING_TURN_SENT" for name, _payload in timeline.events) == 1
    assert any(
        name == "PENDING_TURNS_COALESCED" and payload["segments"] == 2
        for name, payload in timeline.events
    )


def test_pending_turn_promotes_on_first_model_audio() -> None:
    activity_calls: list[str] = []
    commands: list[str] = []
    sent_audio: list[bytes] = []

    class _FakeGemini:
        generation = 8

        def clear_output_nowait(self) -> int:
            return 2

        async def start_activity(self) -> None:
            activity_calls.append("start")
            self.generation += 1

        async def send_audio(self, pcm: bytes) -> None:
            sent_audio.append(pcm)

        async def end_activity(self) -> None:
            activity_calls.append("end")

    async def command(name: str) -> None:
        commands.append(name)

    timeline = _FakeTimeline()
    bridge = object.__new__(AsteriskGeminiBridge)
    bridge.call = SimpleNamespace(
        gemini=_FakeGemini(),
        timeline=timeline,
        detector=SimpleNamespace(set_bot_speaking=lambda _value: None),
    )
    bridge.protocol = SimpleNamespace(command=command)
    bridge.echo_guard = SimpleNamespace(clear=lambda: None)
    bridge.resampler = SimpleNamespace(reset=lambda: None)
    bridge._output_buffer = bytearray()
    bridge._output_buffer_lock = asyncio.Lock()
    bridge._pending_turns = deque(
        [PendingCallerTurn(audio=b"x" * 2_000, speech_ms=800)]
    )
    bridge._pending_turn_audio = None
    bridge._pending_turn_speech_ms = 0.0
    bridge._pending_drain_active = False
    bridge._pending_turn_drain_task = None
    bridge._last_output_submission_at = 1.0
    bridge._active_activity_started = False
    bridge.interruption_policy = InterruptionPolicy()

    async def exercise() -> bool:
        return await bridge._promote_pending_turn_on_model_audio()

    assert asyncio.run(exercise())
    assert activity_calls == ["start", "end"]
    assert commands == ["FLUSH_MEDIA"]
    assert [len(chunk) for chunk in sent_audio] == [1280, 720]
    assert bridge._pending_turns == deque()
    assert any(
        name == "PENDING_TURN_PROMOTED" and payload["segments"] == 1
        for name, payload in timeline.events
    )
    assert any(
        name == "BARGE_IN_FLUSH" and payload["reason"] == "pending_turn_promoted"
        for name, payload in timeline.events
    )


def test_multiple_pending_turns_are_coalesced_in_chronological_order() -> None:
    bridge = object.__new__(AsteriskGeminiBridge)
    bridge._pending_turns = deque(
        [
            PendingCallerTurn(audio=b"first", speech_ms=300),
            PendingCallerTurn(audio=b"second", speech_ms=500),
        ]
    )
    bridge._pending_turn_audio = bytearray(b"active")
    bridge._pending_turn_speech_ms = 700
    bridge._pending_drain_active = False

    async def exercise() -> PendingCallerTurn | None:
        return await bridge._take_pending_caller_turn()

    pending = asyncio.run(exercise())

    assert pending is not None
    assert pending.audio == b"firstsecondactive"
    assert pending.speech_ms == 700
    assert not pending.speech_ended
    assert pending.segments == 3
    assert bridge._pending_turns == deque()
    assert bridge._pending_turn_audio is None


def test_short_pending_interjection_does_not_cancel_model_audio() -> None:
    timeline = _FakeTimeline()
    policy = InterruptionPolicy(
        ignore_short_interjections=True,
        interjection_max_speech_ms=650,
    )
    bridge = object.__new__(AsteriskGeminiBridge)
    bridge.call = SimpleNamespace(timeline=timeline)
    bridge._pending_turns = deque([PendingCallerTurn(audio=b"short", speech_ms=240)])
    bridge._pending_turn_audio = None
    bridge._pending_turn_speech_ms = 0.0
    bridge._pending_drain_active = False
    bridge._pending_turn_drain_task = None
    bridge.interruption_policy = policy
    bridge.interruption_gate = LocalInterruptionGate(policy)

    async def exercise() -> bool:
        return await bridge._promote_pending_turn_on_model_audio()

    assert not asyncio.run(exercise())
    assert bridge._pending_turns == deque()
    assert any(
        name == "LOCAL_INTERJECTION_IGNORED" and payload["origin"] == "pending_turn"
        for name, payload in timeline.events
    )


def test_confirmed_interruption_fades_only_until_gain_reset() -> None:
    gains: list[float] = []

    class _FakeAmi:
        async def set_channel_rx_gain(
            self,
            channel: str,
            gain: float,
        ) -> None:
            assert channel == "WebSocket/elvin/1"
            gains.append(gain)

    timeline = _FakeTimeline()
    bridge = object.__new__(AsteriskGeminiBridge)
    bridge.call = SimpleNamespace(
        timeline=timeline,
        identity=SimpleNamespace(call_id="fade-test"),
    )
    bridge.protocol = SimpleNamespace(
        info=AsteriskMediaInfo(channel="WebSocket/elvin/1")
    )
    bridge.interruption_policy = InterruptionPolicy(
        delayed_interruption=True,
        interruption_tail_ms=100,
        interruption_fade_enabled=True,
        interruption_fade_ms=100,
    )
    bridge.asterisk_ami = _FakeAmi()
    bridge._voice_fade_task = None
    bridge._voice_gain_reset_task = None

    async def exercise() -> None:
        bridge._start_voice_fade()
        await bridge._voice_fade_task
        await bridge._stop_voice_fade(reset_gain=True)

    asyncio.run(exercise())

    assert gains
    assert gains[-1] == 1.0
    assert all(left > right for left, right in zip(gains[:-2], gains[1:-1]))
    assert gains[-2] == 0.001
    assert any(
        name == "INTERRUPTION_VOICE_FADE_COMPLETED"
        for name, _payload in timeline.events
    )
    assert any(
        name == "INTERRUPTION_VOICE_GAIN_RESET" for name, _payload in timeline.events
    )


def test_committed_local_interruption_uses_single_flush_boundary() -> None:
    activity_calls: list[str] = []
    commands: list[str] = []
    sent_audio: list[bytes] = []

    class _FakeGemini:
        generation = 4

        def clear_output_nowait(self) -> int:
            return 3

        async def start_activity(self) -> None:
            activity_calls.append("start")
            self.generation += 1

        async def send_audio(self, pcm: bytes) -> None:
            sent_audio.append(pcm)

        async def end_activity(self) -> None:
            activity_calls.append("end")

    async def command(name: str) -> None:
        commands.append(name)

    timeline = _FakeTimeline()
    bridge = object.__new__(AsteriskGeminiBridge)
    bridge.call = SimpleNamespace(
        gemini=_FakeGemini(),
        timeline=timeline,
        detector=SimpleNamespace(set_bot_speaking=lambda _value: None),
    )
    bridge.protocol = SimpleNamespace(command=command)
    bridge.echo_guard = SimpleNamespace(clear=lambda: None)
    bridge.resampler = SimpleNamespace(reset=lambda: None)
    bridge._output_buffer = bytearray()
    bridge._output_buffer_lock = asyncio.Lock()
    bridge._pending_turns = deque()
    bridge._pending_turn_audio = None
    bridge._pending_drain_active = False
    bridge._last_output_submission_at = 1.0
    bridge._active_activity_started = False
    bridge.interruption_policy = InterruptionPolicy(
        delayed_interruption=True,
        interruption_tail_ms=250,
    )

    async def exercise() -> None:
        await bridge._commit_local_interruption(
            b"x" * 2_000,
            speech_ms=800,
            speech_ended=True,
        )

    asyncio.run(exercise())

    assert activity_calls == ["start", "end"]
    assert commands == ["FLUSH_MEDIA"]
    assert [len(chunk) for chunk in sent_audio] == [1280, 720]
    assert any(
        name == "BARGE_IN_FLUSH" and payload["reason"] == "local_interruption_policy"
        for name, payload in timeline.events
    )


def test_stable_immediate_barge_in_path_is_preserved_when_effects_are_off() -> None:
    activity_calls: list[str] = []
    commands: list[str] = []

    class _FakeGemini:
        response_open_generation = 2

        def clear_output_nowait(self) -> int:
            return 1

        async def start_activity(self) -> None:
            activity_calls.append("start")

    async def command(name: str) -> None:
        commands.append(name)

    timeline = _FakeTimeline()
    bridge = object.__new__(AsteriskGeminiBridge)
    bridge.call = SimpleNamespace(
        gemini=_FakeGemini(),
        timeline=timeline,
        detector=SimpleNamespace(set_bot_speaking=lambda _value: None),
    )
    bridge.protocol = SimpleNamespace(command=command)
    bridge.echo_guard = SimpleNamespace(clear=lambda: None)
    bridge.resampler = SimpleNamespace(reset=lambda: None)
    bridge._output_buffer = bytearray()
    bridge._output_buffer_lock = asyncio.Lock()
    bridge._pending_turns = deque()
    bridge._pending_turn_audio = None
    bridge._pending_drain_active = False
    bridge._last_output_submission_at = 1.0
    bridge._active_activity_started = False

    async def exercise() -> None:
        await bridge._start_caller_activity(
            decision=TurnDecision(
                frame_sequence=1,
                interrupted_bot=True,
            ),
            response_audio_active=True,
        )

    asyncio.run(exercise())

    assert activity_calls == ["start"]
    assert commands == ["FLUSH_MEDIA"]
    assert bridge._active_activity_started
    assert any(
        name == "BARGE_IN_FLUSH" and payload["reason"] == "bot_audio"
        for name, payload in timeline.events
    )


def test_short_interjection_during_playback_never_reaches_gemini() -> None:
    class _InputWebSocket:
        def __init__(self) -> None:
            self.messages = deque(
                [
                    {"type": "websocket.receive", "bytes": b"a" * 640},
                    {"type": "websocket.receive", "bytes": b"b" * 640},
                    {"type": "websocket.disconnect"},
                ]
            )

        async def receive(self) -> dict[str, object]:
            return self.messages.popleft()

    class _Detector:
        bot_speaking = True
        turn_open = False

        def __init__(self) -> None:
            self.decisions = deque(
                [
                    TurnDecision(
                        frame_sequence=1,
                        audio_to_gemini=b"short-",
                        speech_started=True,
                        interrupted_bot=True,
                        speech_ms=240,
                    ),
                    TurnDecision(
                        frame_sequence=2,
                        audio_to_gemini=b"feedback",
                        speech_ended=True,
                        speech_ms=480,
                    ),
                ]
            )

        async def process(
            self,
            _pcm: bytes,
            *,
            echo_suppressed: bool,
        ) -> TurnDecision:
            assert not echo_suppressed
            return self.decisions.popleft()

    class _Gemini:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.bot_audio_active = asyncio.Event()

        async def start_activity(self) -> None:
            self.calls.append("start")

        async def send_audio(self, _pcm: bytes) -> None:
            self.calls.append("audio")

        async def end_activity(self) -> None:
            self.calls.append("end")

    timeline = _FakeTimeline()
    detector = _Detector()
    gemini = _Gemini()
    policy = InterruptionPolicy(
        ignore_short_interjections=True,
        interjection_max_speech_ms=650,
    )
    bridge = object.__new__(AsteriskGeminiBridge)
    bridge.websocket = _InputWebSocket()
    bridge.call = SimpleNamespace(
        caller_audio=_FakeWriter(),
        detector=detector,
        gemini=gemini,
        timeline=timeline,
    )
    bridge.protocol = SimpleNamespace()
    bridge.echo_guard = SimpleNamespace(is_echo=lambda *_args, **_kwargs: False)
    bridge._first_input = False
    bridge._last_echo_event_at = 0.0
    bridge.interruption_policy = policy
    bridge.interruption_gate = LocalInterruptionGate(policy)
    bridge._active_activity_started = False
    bridge._pending_turn_audio = None

    result = asyncio.run(bridge._input_loop())

    assert result == "caller_hangup"
    assert gemini.calls == []
    assert any(
        name == "LOCAL_INTERJECTION_IGNORED" for name, _payload in timeline.events
    )


async def _async_noop(*_args: object, **_kwargs: object) -> None:
    return None
