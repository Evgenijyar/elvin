from array import array

from elvin.media.audio import PreRollBuffer, measure_pcm16


def pcm(samples: list[int]) -> bytes:
    return array("h", samples).tobytes()


def test_silence_levels_are_low() -> None:
    levels = measure_pcm16(pcm([0] * 320))
    assert levels.rms == 0.0
    assert levels.peak == 0.0
    assert levels.dbfs <= -90.0


def test_pcm_level_measurement() -> None:
    levels = measure_pcm16(pcm([16384, -16384] * 160))
    assert 0.49 <= levels.rms <= 0.51
    assert 0.49 <= levels.peak <= 0.51
    assert -6.2 <= levels.dbfs <= -5.8


def test_pre_roll_is_bounded() -> None:
    buffer = PreRollBuffer(milliseconds=40, sample_rate=16_000)
    frame = pcm([100] * 320)  # 20 ms, 640 bytes
    buffer.append(frame)
    buffer.append(frame)
    buffer.append(frame)
    assert len(buffer.snapshot()) <= 1_280
