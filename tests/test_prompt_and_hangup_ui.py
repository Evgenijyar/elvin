from pathlib import Path


def test_robot_ui_exposes_transparent_prompt_and_end_call_controls() -> None:
    html = Path("src/elvin/web/index.html").read_text(encoding="utf-8")
    javascript = Path("src/elvin/web/static/app.js").read_text(encoding="utf-8")

    for control_id in (
        "robotRole",
        "robotKnowledge",
        "robotCallEndCondition",
        "robotCallEndWaitMs",
        "robotCallEndDelayMs",
        "robotCallEndResolved",
        "robotPromptPreview",
    ):
        assert f'id="{control_id}"' in html
        assert f'$("#{control_id}")' in javascript

    assert 'data-tab="prompt-preview"' in html
    assert 'data-pane="prompt-preview"' in html
    assert 'data-stage-reference="stop_list"' in html
    assert "{{stage:" in javascript
    assert "Python не добавляет" in html
