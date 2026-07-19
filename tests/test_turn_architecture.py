from pathlib import Path


def test_call_queue_prepares_gemini_before_lptracker() -> None:
    text = Path("src/elvin/services/call_queue.py").read_text(encoding="utf-8")
    prepare_at = text.index("voice_runtime.prepare_call")
    publish_at = text.index("_publish_pending_media(context)", prepare_at)
    dial_at = text.index("lptracker.call_lead", publish_at)
    assert prepare_at < publish_at < dial_at


def test_server_vad_is_disabled() -> None:
    text = Path("src/elvin/integrations/gemini_live.py").read_text(encoding="utf-8")
    assert '"automatic_activity_detection": {"disabled": True}' in text
    assert "activity_start=types.ActivityStart()" in text
    assert "activity_end=types.ActivityEnd()" in text


def test_short_speech_fragments_are_not_closed_as_turns() -> None:
    text = Path("src/elvin/media/turn_detector.py").read_text(encoding="utf-8")
    assert "min_turn_duration_ms: int = 450" in text
    assert "turn_age_ms >= self.config.min_turn_duration_ms" in text
    assert "turn_merge_grace_ms: int = 300" in text
    assert "echo_suppressed" in text


def test_gemini_instruction_is_valid_russian_utf8() -> None:
    from elvin.integrations.gemini_live import build_system_instruction

    instruction = build_system_instruction(
        {
            "description": "описание",
            "role_prompt": "роль",
            "knowledge_base": "знания",
        }
    )
    assert "Ты голосовой ИИ-робот" in instruction
    assert "ОПИСАНИЕ РОБОТА:" in instruction
    assert "РўС‹" not in instruction
