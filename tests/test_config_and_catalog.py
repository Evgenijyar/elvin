import asyncio
from pathlib import Path

from elvin.api.routes.robots import RobotPayload
from elvin.config import Settings
from elvin.infrastructure.state_store import StateStore
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
    assert [item.name for item in VOICE_OPTIONS] == [
        "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda",
        "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus",
        "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome",
        "Algenib", "Rasalgethi", "Laomedeia", "Achernar", "Alnilam",
        "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
        "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
    ]


def test_robot_interruption_settings_round_trip_in_local_storage(
    tmp_path: Path,
) -> None:
    settings = Settings(
        ELVIN_DATA_DIR=tmp_path / "data",
        ELVIN_LOG_DIR=tmp_path / "logs",
        ELVIN_RECORDINGS_DIR=tmp_path / "recordings",
    )
    payload = RobotPayload(
        name="Local gate",
        ignore_short_interjections=True,
        interjection_max_speech_ms=700,
        delayed_interruption=True,
        interruption_tail_ms=300,
        interruption_fade_enabled=True,
        interruption_fade_ms=200,
    ).model_dump()

    async def exercise() -> None:
        store = StateStore(settings)
        await store.initialize()
        try:
            created = await store.create_robot(payload)
            loaded = await store.get_robot(created["id"])
            assert loaded is not None
            assert loaded["ignore_short_interjections"] is True
            assert loaded["interjection_max_speech_ms"] == 700
            assert loaded["delayed_interruption"] is True
            assert loaded["interruption_tail_ms"] == 300
            assert loaded["interruption_fade_enabled"] is True
            assert loaded["interruption_fade_ms"] == 200
        finally:
            await store.close()

    asyncio.run(exercise())
