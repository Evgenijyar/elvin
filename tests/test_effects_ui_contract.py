from pathlib import Path


def test_actor_director_and_effects_controls_are_present() -> None:
    html = Path("src/elvin/web/index.html").read_text(encoding="utf-8")
    javascript = Path("src/elvin/web/static/app.js").read_text(encoding="utf-8")
    assert 'data-tab="effects"' in html
    assert 'id="effectsMenu"' in html
    assert 'id="effectSettings"' in html
    assert 'id="robotActorApiKey"' in html
    assert 'id="robotDirectorApiKey"' in html
    assert 'id="geminiDirectorApiKey"' in html
    assert "effects_config" in javascript
    assert "renderEffectsEditor" in javascript
    assert "director_api_key" in javascript
