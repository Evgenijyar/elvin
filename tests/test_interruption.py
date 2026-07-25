from elvin.media.interruption import (
    InterruptionAction,
    InterruptionPolicy,
    LocalInterruptionGate,
)


def test_short_interjection_is_ignored_without_reaching_gemini() -> None:
    gate = LocalInterruptionGate(
        InterruptionPolicy(
            ignore_short_interjections=True,
            interjection_max_speech_ms=650,
        )
    )
    gate.begin()

    waiting = gate.observe(
        audio=b"short-",
        speech_ms=320,
        speech_ended=False,
        now=0.3,
    )
    ignored = gate.observe(
        audio=b"feedback",
        speech_ms=480,
        speech_ended=True,
        now=0.7,
    )

    assert waiting.action == InterruptionAction.WAIT
    assert ignored.action == InterruptionAction.IGNORE
    assert ignored.audio == b""
    assert ignored.audio_bytes == len(b"short-feedback")
    assert ignored.speech_ms == 480
    assert not gate.active


def test_full_phrase_waits_for_configured_tail_before_commit() -> None:
    gate = LocalInterruptionGate(
        InterruptionPolicy(
            ignore_short_interjections=True,
            interjection_max_speech_ms=650,
            delayed_interruption=True,
            interruption_tail_ms=250,
        )
    )
    gate.begin()

    confirmed = gate.observe(
        audio=b"first-",
        speech_ms=660,
        speech_ended=False,
        now=0.50,
    )
    still_waiting = gate.observe(
        audio=b"second-",
        speech_ms=780,
        speech_ended=True,
        now=0.70,
    )
    committed = gate.observe(
        audio=b"",
        speech_ms=0,
        speech_ended=False,
        now=0.75,
    )

    assert confirmed.action == InterruptionAction.WAIT
    assert confirmed.confirmed_now
    assert still_waiting.action == InterruptionAction.WAIT
    assert committed.action == InterruptionAction.COMMIT
    assert committed.audio == b"first-second-"
    assert committed.speech_ended
    assert not gate.active


def test_delay_effect_works_without_interjection_filter() -> None:
    gate = LocalInterruptionGate(
        InterruptionPolicy(
            ignore_short_interjections=False,
            delayed_interruption=True,
            interruption_tail_ms=300,
        )
    )
    gate.begin()

    confirmed = gate.observe(
        audio=b"a",
        speech_ms=80,
        speech_ended=False,
        now=10.0,
    )
    waiting = gate.observe(
        audio=b"b",
        speech_ms=160,
        speech_ended=False,
        now=10.29,
    )
    committed = gate.observe(
        audio=b"c",
        speech_ms=180,
        speech_ended=False,
        now=10.30,
    )

    assert confirmed.confirmed_now
    assert confirmed.action == InterruptionAction.WAIT
    assert waiting.action == InterruptionAction.WAIT
    assert committed.action == InterruptionAction.COMMIT
    assert committed.audio == b"abc"


def test_robot_policy_values_are_bounded() -> None:
    policy = InterruptionPolicy.from_robot(
        {
            "ignore_short_interjections": True,
            "interjection_max_speech_ms": 99_999,
            "delayed_interruption": True,
            "interruption_tail_ms": -5,
            "interruption_fade_enabled": True,
            "interruption_fade_ms": 99_999,
        }
    )

    assert policy.interjection_max_speech_ms == 2_000
    assert policy.interruption_tail_ms == 0
    assert policy.interruption_fade_ms == 2_000
    assert policy.effective_fade_ms == 0
    assert policy.enabled


def test_voice_fade_is_clamped_to_the_interruption_tail() -> None:
    policy = InterruptionPolicy(
        delayed_interruption=True,
        interruption_tail_ms=300,
        interruption_fade_enabled=True,
        interruption_fade_ms=500,
    )

    assert policy.effective_tail_ms == 300
    assert policy.effective_fade_ms == 300
