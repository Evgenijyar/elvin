import asyncio
import sys
from datetime import UTC, datetime
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


def test_local_call_history_persists_transcript_and_filters(tmp_path: Path) -> None:
    settings = Settings(
        ELVIN_DATA_DIR=tmp_path / "data",
        ELVIN_LOG_DIR=tmp_path / "logs",
        ELVIN_RECORDINGS_DIR=tmp_path / "recordings",
    )
    store = StateStore(settings)

    async def exercise() -> None:
        await store.initialize()
        robot = await store.create_robot({"name": "Элвин"})
        assignment = await store.create_assignment(
            {
                "project_id": 10,
                "project_name": "Продажи",
                "robot_id": robot["id"],
            }
        )
        batch = await store.create_call_batch(
            assignment_id=assignment["id"],
            project_id=10,
            robot_id=robot["id"],
            source_stage_id=7,
            items=[
                {
                    "lead_id": 501,
                    "lead_name": "Тестовый лид",
                    "contact_name": "Иван",
                    "phone": "+***4567",
                    "phone_masked": "+***4567",
                    "phone_number": "+7 999 123-45-67",
                },
                {
                    "lead_id": 502,
                    "lead_name": "Второй лид",
                    "contact_name": "Мария",
                    "phone": "+***2211",
                    "phone_masked": "+***2211",
                    "phone_number": "+7 916 555-22-11",
                },
            ],
        )
        items = await store.list_call_items(batch["id"])
        first = next(item for item in items if item["lead_id"] == 501)
        second = next(item for item in items if item["lead_id"] == 502)
        started = datetime(2026, 7, 27, 14, 30, tzinfo=UTC)
        finished = datetime(2026, 7, 27, 14, 33, tzinfo=UTC)
        transcript = "Клиент: Добрый день.\n\nЭлвин: Да, приветствую."
        await store.update_call_item(
            first["id"],
            status="COMPLETED",
            call_started_at=started,
            call_finished_at=finished,
            transcript=transcript,
        )
        await store.save_call_record(first["id"])
        await store.update_call_item(
            second["id"],
            status="COMPLETED",
            call_started_at=datetime(2026, 7, 27, 15, 30, tzinfo=UTC),
            call_finished_at=datetime(2026, 7, 27, 15, 31, tzinfo=UTC),
            transcript="Клиент: Алло.",
        )
        await store.save_call_record(second["id"])

        calls, total = await store.list_calls(
            date_from="2026-07-27",
            date_to="2026-07-27",
            phone="12345",
        )
        assert total == 1
        assert calls[0]["lead_id"] == 501
        assert calls[0]["phone_number"] == "+7 999 123-45-67"
        assert calls[0]["transcript"] == transcript
        assert calls[0]["analysis"] == ""
        assert calls[0]["project_name"] == "Продажи"
        assert calls[0]["robot_name"] == "Элвин"

        page_one, total = await store.list_calls(limit=1, offset=0)
        page_two, second_total = await store.list_calls(limit=1, offset=1)
        assert total == second_total == 2
        assert [page_one[0]["lead_id"], page_two[0]["lead_id"]] == [502, 501]

        # The history is a detached snapshot and must survive later removal
        # of the robot and its assignment.
        assert await store.delete_robot(robot["id"]) is True
        calls, total = await store.list_calls(phone="4567")
        assert total == 1
        assert calls[0]["robot_name"] == "Элвин"
        assert calls[0]["project_name"] == "Продажи"

        calls, total = await store.list_calls(phone="0000")
        assert calls == []
        assert total == 0

        calls, total = await store.list_calls(date_from="2026-07-28")
        assert calls == []
        assert total == 0

    asyncio.run(exercise())


def test_local_call_history_uses_browser_timezone_for_calendar_dates(tmp_path: Path) -> None:
    settings = Settings(
        ELVIN_DATA_DIR=tmp_path / "data",
        ELVIN_LOG_DIR=tmp_path / "logs",
        ELVIN_RECORDINGS_DIR=tmp_path / "recordings",
    )
    store = StateStore(settings)

    async def exercise() -> None:
        await store.initialize()
        robot = await store.create_robot({"name": "Элвин"})
        assignment = await store.create_assignment(
            {
                "project_id": 11,
                "project_name": "Часовой пояс",
                "robot_id": robot["id"],
            }
        )
        batch = await store.create_call_batch(
            assignment_id=assignment["id"],
            project_id=11,
            robot_id=robot["id"],
            source_stage_id=7,
            items=[
                {
                    "lead_id": 601,
                    "lead_name": "Поздний звонок",
                    "contact_name": "Анна",
                    "phone": "+***0001",
                    "phone_masked": "+***0001",
                    "phone_number": "+31 6 0000 0001",
                }
            ],
        )
        item = (await store.list_call_items(batch["id"]))[0]
        # 22:30 UTC is already the next calendar day in Amsterdam in July.
        await store.update_call_item(
            item["id"],
            status="COMPLETED",
            call_started_at=datetime(2026, 7, 27, 22, 30, tzinfo=UTC),
            call_finished_at=datetime(2026, 7, 27, 22, 31, tzinfo=UTC),
            transcript="Клиент: Алло.",
        )
        await store.save_call_record(item["id"])

        calls, total = await store.list_calls(
            date_from="2026-07-28",
            date_to="2026-07-28",
            timezone_name="Europe/Amsterdam",
        )
        assert total == 1
        assert calls[0]["lead_id"] == 601

        calls, total = await store.list_calls(
            date_from="2026-07-27",
            date_to="2026-07-27",
            timezone_name="Europe/Amsterdam",
        )
        assert calls == []
        assert total == 0

    asyncio.run(exercise())
