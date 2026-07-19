import asyncio
from types import SimpleNamespace

from elvin.integrations.gemini_live import GeminiLiveSession


class _Timeline:
    call_id = "test-call"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def add(self, name: str, **details: object) -> None:
        self.events.append((name, details))


def _session_for_response_test() -> GeminiLiveSession:
    session = object.__new__(GeminiLiveSession)
    session._closed = False
    session._generation = 2
    session._pending_server_generation = 1
    session._pending_audio_generation = 1
    session._response_open_generation = 1
    session._first_audio_seen_for_generation = set()
    session._input_transcripts = {1: "старый вопрос", 2: ""}
    session._output_transcripts = {1: "старый ответ", 2: ""}
    session._last_audio_packet_at = {}
    session.output_audio = asyncio.Queue(maxsize=20)
    session.timeline = _Timeline()
    session.turn_complete = asyncio.Event()
    session.turn_complete_generation = -1
    session.turn_complete_queue = asyncio.Queue()
    session.bot_audio_active = asyncio.Event()
    session.input_transcript = ""
    session.output_transcript = ""
    return session


def test_late_interrupted_turn_is_not_relabelled_as_current() -> None:
    session = _session_for_response_test()
    old_audio = SimpleNamespace(
        inline_data=SimpleNamespace(data=b"old-audio")
    )
    current_audio = SimpleNamespace(
        inline_data=SimpleNamespace(data=b"current-audio")
    )

    async def exercise() -> None:
        await session._handle_response(
            SimpleNamespace(
                server_content=SimpleNamespace(
                    interrupted=True,
                    model_turn=SimpleNamespace(parts=[old_audio]),
                )
            )
        )
        await session._handle_response(
            SimpleNamespace(
                server_content=SimpleNamespace(
                    turn_complete=True,
                )
            )
        )
        assert session._pending_server_generation is None
        assert session.turn_complete_generation == 1
        assert await session.turn_complete_queue.get() == 1
        assert session.output_audio.empty()

        await session._handle_response(
            SimpleNamespace(
                server_content=SimpleNamespace(
                    model_turn=SimpleNamespace(parts=[current_audio]),
                )
            )
        )
        packet = await session.output_audio.get()
        assert packet.generation == 2
        assert packet.pcm24 == b"current-audio"

    asyncio.run(exercise())


def test_new_audio_is_not_dropped_when_old_turn_complete_is_late() -> None:
    session = _session_for_response_test()
    # The interruption marker has already arrived; only the previous
    # turn-complete notification is still pending.
    session._pending_audio_generation = None
    current_audio = SimpleNamespace(
        inline_data=SimpleNamespace(data=b"current-audio")
    )

    async def exercise() -> None:
        await session._handle_response(
            SimpleNamespace(
                server_content=SimpleNamespace(
                    model_turn=SimpleNamespace(parts=[current_audio]),
                )
            )
        )
        packet = await session.output_audio.get()
        assert packet.generation == 2
        assert packet.pcm24 == b"current-audio"
        session.output_audio.task_done()
        assert session._pending_server_generation == 1

        await session._handle_response(
            SimpleNamespace(
                server_content=SimpleNamespace(turn_complete=True)
            )
        )
        assert session._pending_server_generation is None
        assert session.turn_complete_generation == 1

    asyncio.run(exercise())
