from pathlib import Path

import pytest

from elvin.media import runtime
from elvin.media.runtime import PreparedVoiceCall, VoiceCallIdentity
from elvin.services.conversation_effects import default_effects_config


class _Detector:
    def __init__(self, **_kwargs: object) -> None:
        pass


class _Actor:
    def __init__(self, *, api_key: str, **_kwargs: object) -> None:
        self.api_key = api_key


class _Director:
    def __init__(self, *, api_key: str, **_kwargs: object) -> None:
        self.api_key = api_key


def _identity() -> VoiceCallIdentity:
    return VoiceCallIdentity(
        batch_id="batch",
        item_id="item",
        assignment_id="assignment",
        robot_id="robot",
        lead_id=42,
    )


def test_director_is_not_created_when_all_effects_are_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime, "LocalTurnDetector", _Detector)
    monkeypatch.setattr(runtime, "GeminiLiveSession", _Actor)
    monkeypatch.setattr(runtime, "GeminiDirectorSession", _Director)
    robot = {"effects_config": default_effects_config(enabled=False)}
    call = PreparedVoiceCall(
        identity=_identity(),
        robot=robot,
        actor_api_key="actor",
        director_api_key="director",
        recordings_dir=tmp_path,
        trace_enabled=False,
        turn_config=runtime.TurnDetectorConfig(),
    )
    assert call.gemini.api_key == "actor"
    assert call.director is None


def test_enabled_effect_requires_and_uses_director_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime, "LocalTurnDetector", _Detector)
    monkeypatch.setattr(runtime, "GeminiLiveSession", _Actor)
    monkeypatch.setattr(runtime, "GeminiDirectorSession", _Director)
    effects = default_effects_config(enabled=False)
    effects["natural_interruption"]["enabled"] = True
    robot = {"effects_config": effects}
    with pytest.raises(RuntimeError, match="Режиссёр"):
        PreparedVoiceCall(
            identity=_identity(),
            robot=robot,
            actor_api_key="actor",
            director_api_key="",
            recordings_dir=tmp_path,
            trace_enabled=False,
            turn_config=runtime.TurnDetectorConfig(),
        )
    call = PreparedVoiceCall(
        identity=_identity(),
        robot=robot,
        actor_api_key="actor",
        director_api_key="director-other-project",
        recordings_dir=tmp_path,
        trace_enabled=False,
        turn_config=runtime.TurnDetectorConfig(),
    )
    assert call.director is not None
    assert call.director.api_key == "director-other-project"
