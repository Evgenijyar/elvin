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
