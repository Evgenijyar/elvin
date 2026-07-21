import math
from array import array

from elvin.media.conversation_audio import (
    ConversationAudioEffects,
    build_release_tail,
    duration_ms,
)
from elvin.services.conversation_effects import default_effects_config


def _tone(milliseconds: int = 500, frequency: float = 210.0) -> bytes:
    samples = array(
        "h",
        (
            int(9000 * math.sin(2 * math.pi * frequency * index / 16000))
            for index in range(int(16000 * milliseconds / 1000))
        ),
    )
    return samples.tobytes()


def test_disabled_effect_stack_preserves_pcm_exactly() -> None:
    effects = ConversationAudioEffects(default_effects_config(enabled=False))
    source = _tone(160)
    effects.start_generation(1)
    assert effects.process_actor_audio(source) == source
    assert effects.take_pause() == b""
    assert effects.release_tail() == b""
    assert effects.response_delay(500) == 0.0


def test_release_tail_has_configured_length_and_fades_to_silence() -> None:
    tail = build_release_tail(
        _tone(100),
        release_ms=280,
        slowdown_percent=12,
        fade_start_percent=62,
        natural_cut={
            "search_window_ms": 70,
            "energy_window_ms": 8,
            "zero_cross_threshold": 420,
            "max_trim_ms": 55,
        },
    )
    assert round(duration_ms(tail)) == 280
    samples = array("h")
    samples.frombytes(tail)
    assert abs(samples[-1]) <= 2


def test_pace_pause_and_mastering_are_configurable() -> None:
    config = default_effects_config(enabled=False)
    config["pace_matching"]["enabled"] = True
    config["pace_matching"]["min_percent"] = 90
    config["pace_matching"]["max_percent"] = 110
    config["pace_matching"]["default_percent"] = 100
    config["pace_matching"]["smoothing_percent"] = 0
    config["pace_matching"]["block_ms"] = 60
    config["micro_pauses"]["enabled"] = True
    config["micro_pauses"]["min_audio_before_pause_ms"] = 0
    config["micro_pauses"]["short_pause_ms"] = 55
    config["voice_mastering"]["enabled"] = True
    effects = ConversationAudioEffects(config)
    effects.start_generation(1)
    effects.set_pace(108)
    source = _tone(600)
    processed = effects.process_actor_audio(source)
    assert 0 < len(processed) < len(source)
    effects.schedule_pause("short")
    assert round(duration_ms(effects.take_pause())) == 55


def test_response_delay_uses_configured_bounds_without_jitter() -> None:
    config = default_effects_config(enabled=False)
    config["adaptive_response_delay"]["enabled"] = True
    config["adaptive_response_delay"]["min_ms"] = 140
    config["adaptive_response_delay"]["max_ms"] = 650
    config["adaptive_response_delay"]["jitter_ms"] = 0
    effects = ConversationAudioEffects(config)
    assert effects.response_delay(10) == 0.14
    assert effects.response_delay(9999) == 0.65
