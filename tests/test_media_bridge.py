import asyncio
from types import SimpleNamespace

from elvin.media.asterisk_bridge import (
    AsteriskGeminiBridge,
    AsteriskMediaInfo,
    AsteriskProtocol,
)


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

    async def send_media(pcm: bytes) -> None:
        sent.append(pcm)

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


def test_protocol_accepts_json_and_legacy_events() -> None:
    protocol = AsteriskProtocol(_FakeWebSocket(), SimpleNamespace(timeline=_FakeTimeline()))

    assert protocol.parse_text(
        '{"event":"MEDIA_START","format":"slin16","optimal_frame_size":640}'
    )["event"] == "MEDIA_START"
    legacy = protocol.parse_text(
        "MEDIA_START format:slin16 optimal_frame_size:640 ptime:20"
    )
    assert legacy["event"] == "MEDIA_START"
    assert legacy["optimal_frame_size"] == "640"
