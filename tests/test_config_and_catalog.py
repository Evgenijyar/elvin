from pathlib import Path

from elvin.config import Settings
from elvin.integrations.gemini import GEMINI_LIVE_MODEL_ID
from elvin.integrations.voices import VOICE_OPTIONS


def test_local_settings_use_file_storage(tmp_path: Path) -> None:
    settings = Settings(
        ELVIN_DATA_DIR=tmp_path / "data",
        ELVIN_LOG_DIR=tmp_path / "logs",
        ELVIN_RECORDINGS_DIR=tmp_path / "recordings",
    )
    assert settings.database_configured is False


def test_fixed_model_and_voice_catalog() -> None:
    assert GEMINI_LIVE_MODEL_ID == "gemini-3.1-flash-live-preview"
    assert len(VOICE_OPTIONS) == 30
    assert {item.name for item in VOICE_OPTIONS} >= {"Kore", "Puck", "Aoede"}


def test_director_key_has_independent_environment_alias(tmp_path: Path) -> None:
    settings = Settings(
        ELVIN_DATA_DIR=tmp_path / "data",
        ELVIN_LOG_DIR=tmp_path / "logs",
        ELVIN_RECORDINGS_DIR=tmp_path / "recordings",
        GEMINI_API_KEY="actor-key",
        GEMINI_DIRECTOR_API_KEY="director-key",
    )
    assert settings.gemini_key_configured is True
    assert settings.gemini_director_key_configured is True
    assert settings.gemini_api_key is not None
    assert settings.gemini_director_api_key is not None
    assert settings.gemini_api_key.get_secret_value() == "actor-key"
    assert settings.gemini_director_api_key.get_secret_value() == "director-key"
