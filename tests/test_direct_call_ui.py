from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_assignment_card_exposes_both_call_transports() -> None:
    js = (ROOT / "src/elvin/web/static/app.js").read_text(encoding="utf-8")
    store = (
        ROOT / "src/elvin/infrastructure/state_store.py"
    ).read_text(encoding="utf-8")
    dialplan = (
        ROOT / "deploy/server/elvin-direct-calls.conf"
    ).read_text(encoding="utf-8")

    assert 'value="lptracker_api"' in js
    assert 'value="direct_sip"' in js
    assert "{ call_transport: event.target.value }" in js
    assert '"call_transport": LPTRACKER_API' in store
    assert "[elvin-direct-outbound]" in dialplan
    assert "Dial(WebSocket/elvin-media/c(slin16)f(json))" in dialplan
    assert "Playback(" not in dialplan
