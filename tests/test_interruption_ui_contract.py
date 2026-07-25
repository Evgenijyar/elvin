from pathlib import Path


def test_effects_ui_contains_only_local_interruption_controls() -> None:
    html = Path("src/elvin/web/index.html").read_text(encoding="utf-8")
    javascript = Path("src/elvin/web/static/app.js").read_text(encoding="utf-8")

    for control_id in (
        "robotIgnoreShortInterjections",
        "robotInterjectionMaxSpeechMs",
        "robotDelayedInterruption",
        "robotInterruptionTailMs",
    ):
        assert f'id="{control_id}"' in html
        assert f'$("#{control_id}")' in javascript

    assert 'data-tab="effects"' in html
    assert 'data-pane="effects"' in html
    assert "gemini_director" not in javascript
