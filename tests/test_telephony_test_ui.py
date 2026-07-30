from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_telephony_test_page_is_isolated_and_visible_in_navigation() -> None:
    html = (ROOT / "src/elvin/web/index.html").read_text(encoding="utf-8")
    js = (ROOT / "src/elvin/web/static/app.js").read_text(encoding="utf-8")
    dialplan = (
        ROOT / "deploy/server/elvin-telephony-test.conf"
    ).read_text(encoding="utf-8")

    assert 'data-page="telephonyTest"' in html
    assert 'id="telephonyTestPage"' in html
    assert 'id="telephonyTestPhone"' in html
    assert 'id="telephonyTestAudio"' in html
    assert 'id="startTelephonyTestButton"' in html
    assert 'api("/api/telephony-test/calls"' in js
    assert "/api/telephony-test/calls/${state.telephonyTestId}" in js
    assert "[elvin-telephony-test]" in dialplan
    assert "Playback(/var/lib/asterisk/sounds/elvin-telephony-test/" in dialplan
    assert "Dial(WebSocket" not in dialplan
    assert "Goto(elvin-ai" not in dialplan
