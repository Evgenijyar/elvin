import asyncio
from array import array

from elvin.media.background_audio import LoopingBackgroundAudio


def _pcm(*samples: int) -> bytes:
    values = array("h", samples)
    return values.tobytes()


def _samples(pcm: bytes) -> list[int]:
    values = array("h")
    values.frombytes(pcm)
    return list(values)


def test_background_audio_loops_and_keeps_exact_frame_size() -> None:
    track = LoopingBackgroundAudio(_pcm(1000, -1000), volume_percent=50)

    async def exercise() -> None:
        frame = await track.background_bytes(12)
        assert len(frame) == 12
        assert _samples(frame) == [500, -500, 500, -500, 500, -500]

    asyncio.run(exercise())


def test_background_mix_is_saturating_and_preserves_voice_length() -> None:
    track = LoopingBackgroundAudio(_pcm(20_000, -20_000), volume_percent=100)

    async def exercise() -> None:
        mixed = await track.mix_with_voice(_pcm(20_000, -20_000))
        assert len(mixed) == 4
        assert _samples(mixed) == [32767, -32768]

    asyncio.run(exercise())


def test_zero_volume_disables_background() -> None:
    track = LoopingBackgroundAudio(_pcm(1000), volume_percent=0)
    assert track.enabled is False
