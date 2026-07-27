import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from uuid import UUID

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


class FakePool:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.fetched: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *values: object) -> str:
        self.executed.append((query, values))
        return "INSERT 0 1"

    async def fetchval(self, query: str, *values: object) -> int:
        self.fetched.append((query, values))
        return 1

    async def fetch(self, query: str, *values: object) -> list[dict[str, object]]:
        self.fetched.append((query, values))
        timestamp = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        return [
            {
                "id": UUID("11111111-1111-1111-1111-111111111111"),
                "call_item_id": UUID("11111111-1111-1111-1111-111111111111"),
                "batch_id": UUID("22222222-2222-2222-2222-222222222222"),
                "assignment_id": UUID("33333333-3333-3333-3333-333333333333"),
                "project_id": 10,
                "project_name": "Продажи",
                "robot_id": UUID("44444444-4444-4444-4444-444444444444"),
                "robot_name": "Элвин",
                "lead_id": 501,
                "lead_name": "Лид",
                "contact_name": "Иван",
                "phone_number": "+7 999 123-45-67",
                "phone_masked": "+***4567",
                "status": "COMPLETED",
                "result": "completed",
                "outcome": "lead",
                "destination_stage_id": 7,
                "destination_stage_name": "Лид",
                "call_started_at": timestamp,
                "call_finished_at": timestamp,
                "transcript": "Клиент: Алло.",
                "analysis": "",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        ]


def test_postgres_call_snapshot_and_filtered_listing(tmp_path: Path) -> None:
    settings = Settings(
        ELVIN_DATA_DIR=tmp_path / "data",
        ELVIN_LOG_DIR=tmp_path / "logs",
        ELVIN_RECORDINGS_DIR=tmp_path / "recordings",
    )
    store = StateStore(settings)
    pool = FakePool()
    store.mode = "postgres"
    store.pool = pool  # type: ignore[assignment]

    async def exercise() -> None:
        item_id = "11111111-1111-1111-1111-111111111111"
        await store.save_call_record(item_id)
        query, values = pool.executed[-1]
        assert "INSERT INTO app.call_records" in query
        assert "ON CONFLICT(call_item_id) DO UPDATE" in query
        assert values == (item_id,)

        calls, total = await store.list_calls(
            date_from="2026-07-01",
            date_to="2026-07-31",
            phone="123-45-67",
            limit=25,
            offset=50,
        )
        assert total == 1
        assert calls[0]["id"] == item_id
        assert calls[0]["call_started_at"] == "2026-07-27T12:00:00+00:00"
        count_query, count_values = pool.fetched[0]
        page_query, page_values = pool.fetched[1]
        assert "regexp_replace" in count_query
        assert count_values == ("2026-07-01", "2026-07-31", "%1234567%")
        assert "ORDER BY call_started_at DESC" in page_query
        assert page_values == (
            "2026-07-01",
            "2026-07-31",
            "%1234567%",
            25,
            50,
        )

    asyncio.run(exercise())
