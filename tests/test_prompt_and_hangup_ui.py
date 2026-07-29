from pathlib import Path


def test_robot_ui_keeps_only_visible_prompt_and_hangup_controls() -> None:
    html = Path("src/elvin/web/index.html").read_text(encoding="utf-8")
    javascript = Path("src/elvin/web/static/app.js").read_text(encoding="utf-8")

    for control_id in (
        "robotRole",
        "robotKnowledge",
        "robotCallEndCondition",
        "robotPromptPreview",
    ):
        assert f'id="{control_id}"' in html
        assert f'$("#{control_id}")' in javascript

    for removed_id in (
        "robotFirstPhrase",
        "robotCallEndWaitMs",
        "robotCallEndDelayMs",
        "robotCallEndResolved",
        "geminiKeyStatus",
        "geminiModelId",
        "geminiEndpoint",
    ):
        assert f'id="{removed_id}"' not in html
        assert f'$("#{removed_id}")' not in javascript

    assert 'data-tab="prompt-preview"' in html
    assert 'data-pane="prompt-preview"' in html
    assert 'data-stage-reference="stop_list"' in html
    assert "{{stage:" in javascript
    assert 'id="robotCallEndCondition" rows="3"' in html
    assert html.index('data-stage-reference="lead"') < html.index(
        'id="robotCallEndCondition"'
    )

    for unwanted in (
        "Python не добавляет",
        "Backend не дописывает",
        "Условия вызова end_call",
        "Максимальное ожидание",
        "Дополнительная пауза",
        "Итоговое условие end_call",
        "Служебная подпись",
        "Ключ хранится",
        "Ключ показывается полностью",
        "Endpoint и ID модели",
        "mark_call_as_lead",
    ):
        assert unwanted not in html


def test_assignment_ui_has_single_controls_and_custom_delete_confirmation() -> None:
    html = Path("src/elvin/web/index.html").read_text(encoding="utf-8")
    javascript = Path("src/elvin/web/static/app.js").read_text(encoding="utf-8")

    assert html.count('id="openAssignmentModal"') == 1
    assert "data-open-assignment" not in html
    assert "preview-button" not in javascript
    assert "prepare-queue" not in javascript
    assert "remove-assignment" not in javascript
    assert "status-pill" not in javascript
    assert 'class="flat-button show-queue"' in javascript
    assert 'class="primary-button start-stop"' in javascript
    assert 'class="assignment-delete"' in javascript
    assert 'id="confirmModal"' in html
    assert "confirm(" not in javascript
    assert "Модель</span><strong>" not in javascript
    assert "Голос</span><strong>" not in javascript
