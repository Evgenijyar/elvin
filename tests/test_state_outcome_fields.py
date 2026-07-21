import asyncio
import sys
from pathlib import Path
from types import ModuleType

try:
    import asyncpg  # noqa: F401
except ModuleNotFoundError:
    asyncpg_stub = ModuleType("asyncpg")
    asyncpg_stub.Pool = object
    asyncpg_stub.Record = dict
    asyncpg_stub.UniqueViolationError = RuntimeError
    sys.modules["asyncpg"] = asyncpg_stub

from elvin.config import Settings
from elvin.infrastructure.state_store import StateStore


def test_local_store_persists_assignment_outcome_configuration(tmp_path: Path) -> None:
    settings = Settings(
        ELVIN_DATA_DIR=tmp_path / "data",
        ELVIN_LOG_DIR=tmp_path / "logs",
        ELVIN_RECORDINGS_DIR=tmp_path / "recordings",
    )
    store = StateStore(settings)

    async def exercise() -> None:
        await store.initialize()
        robot = await store.create_robot(
            {
                "name": "Тест",
                "model_id": "gemini-3.1-flash-live-preview",
                "voice_name": "Kore",
            }
        )
        assignment = await store.create_assignment(
            {
                "project_id": 1,
                "project_name": "Проект",
                "robot_id": robot["id"],
            }
        )
        updated = await store.update_assignment(
            assignment["id"],
            {
                "lead_stage_id": 11,
                "lead_stage_name": "Лид",
                "lead_limit": 3,
                "count_special_as_lead": True,
                "background_audio_filename": "office.mp3",
                "background_audio_volume": 12,
            },
        )
        assert updated is not None
        loaded = await store.get_assignment(assignment["id"])
        assert loaded is not None
        assert loaded["lead_stage_id"] == 11
        assert loaded["lead_limit"] == 3
        assert loaded["count_special_as_lead"] is True
        assert loaded["background_audio_filename"] == "office.mp3"
        assert loaded["background_audio_volume"] == 12

        cleared = await store.update_assignment(
            assignment["id"],
            {"lead_stage_id": None, "lead_stage_name": ""},
        )
        assert cleared is not None
        assert cleared["lead_stage_id"] is None

    asyncio.run(exercise())


def test_local_store_persists_robot_effect_profile(tmp_path: Path) -> None:
    settings = Settings(
        ELVIN_DATA_DIR=tmp_path / "data",
        ELVIN_LOG_DIR=tmp_path / "logs",
        ELVIN_RECORDINGS_DIR=tmp_path / "recordings",
    )
    store = StateStore(settings)

    async def exercise() -> None:
        await store.initialize()
        robot = await store.create_robot(
            {
                "name": "Effects",
                "model_id": "gemini-3.1-flash-live-preview",
                "voice_name": "Kore",
                "effects_config": {
                    "natural_interruption": {
                        "enabled": True,
                        "release_ms": 410,
                    }
                },
            }
        )
        assert robot["effects_config"]["natural_interruption"]["enabled"] is True
        assert robot["effects_config"]["natural_interruption"]["release_ms"] == 410
        loaded = await store.get_robot(robot["id"])
        assert loaded is not None
        assert loaded["effects_config"]["natural_interruption"]["release_ms"] == 410
        assert loaded["effects_config"]["semantic_interruption"]["enabled"] is False

    asyncio.run(exercise())


def test_postgres_list_robots_returns_saved_effect_profile(tmp_path: Path) -> None:
    settings = Settings(
        ELVIN_DATA_DIR=tmp_path / "data",
        ELVIN_LOG_DIR=tmp_path / "logs",
        ELVIN_RECORDINGS_DIR=tmp_path / "recordings",
    )
    store = StateStore(settings)

    class FakePool:
        async def fetch(self, query: str):  # type: ignore[no-untyped-def]
            assert "effects_config" in query
            from datetime import UTC, datetime

            return [
                {
                    "id": "8f348f6f-81ff-45c1-9c29-1ebef122f8ea",
                    "name": "Effects",
                    "description": "",
                    "model_id": "gemini-3.1-flash-live-preview",
                    "voice_name": "Kore",
                    "temperature": 0.3,
                    "role_prompt": "",
                    "knowledge_base": "",
                    "first_phrase": "",
                    "lead_condition": "",
                    "special_condition": "",
                    "refusal_condition": "",
                    "callback_condition": "",
                    "stop_list_condition": "",
                    "answering_machine_condition": "",
                    "effects_config": '{"natural_interruption":{"enabled":true,"release_ms":410}}',
                    "active": True,
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                }
            ]

    async def exercise() -> None:
        store.mode = "postgres"
        store.pool = FakePool()  # type: ignore[assignment]
        robots = await store.list_robots()
        assert robots[0]["effects_config"]["natural_interruption"]["enabled"] is True
        assert robots[0]["effects_config"]["natural_interruption"]["release_ms"] == 410

    asyncio.run(exercise())
