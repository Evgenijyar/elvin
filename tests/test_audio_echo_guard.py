import math
import struct

from elvin.media.audio import PlaybackEchoGuard


def _tone(
    *,
    phase: float = 0.0,
    amplitude: float = 0.2,
    frequency: float = 440,
) -> bytes:
    samples = [
        int(
            amplitude
            * 32767
            * math.sin(phase + (2 * math.pi * frequency * index / 16_000))
        )
        for index in range(320)
    ]
    return struct.pack("<320h", *samples)


def test_playback_echo_is_suppressed_but_independent_speech_is_not() -> None:
    guard = PlaybackEchoGuard()
    playback = _tone()
    guard.note_playback(playback)

    assert guard.is_echo(playback, active=True)
    assert not guard.is_echo(_tone(frequency=1200), active=True)
    assert not guard.is_echo(playback, active=False)


def test_echo_guard_tolerates_attenuation() -> None:
    guard = PlaybackEchoGuard()
    playback = _tone(amplitude=0.3)
    guard.note_playback(playback)
    attenuated = bytes(
        value
        for sample in struct.unpack("<320h", playback)
        for value in struct.pack("<h", int(sample * 0.18))
    )

    assert guard.is_echo(attenuated, active=True)
