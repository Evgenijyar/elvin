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


class FakeRobotPool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query: str, *values: object) -> dict[str, object]:
        self.calls.append((query, values))
        now = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
        return {
            "id": UUID(str(values[0])),
            "name": values[1],
            "description": values[2],
            "model_id": values[3],
            "voice_name": values[4],
            "temperature": values[5],
            "role_prompt": values[6],
            "knowledge_base": values[7],
            "first_phrase": values[8],
            "lead_condition": values[9],
            "special_condition": values[10],
            "refusal_condition": values[11],
            "callback_condition": values[12],
            "stop_list_condition": values[13],
            "answering_machine_condition": values[14],
            "call_end_condition": values[15],
            "call_end_wait_ms": values[16],
            "call_end_delay_ms": values[17],
            "ignore_short_interjections": values[18],
            "interjection_max_speech_ms": values[19],
            "delayed_interruption": values[20],
            "interruption_tail_ms": values[21],
            "interruption_fade_enabled": values[22],
            "interruption_fade_ms": values[23],
            "active": values[24],
            "created_at": now,
            "updated_at": now,
        }


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        ELVIN_DATA_DIR=tmp_path / "data",
        ELVIN_LOG_DIR=tmp_path / "logs",
        ELVIN_RECORDINGS_DIR=tmp_path / "recordings",
    )


def test_postgres_robot_create_persists_visible_hangup_configuration(
    tmp_path: Path,
) -> None:
    store = StateStore(_settings(tmp_path))
    pool = FakeRobotPool()
    store.mode = "postgres"
    store.pool = pool  # type: ignore[assignment]

    async def exercise() -> None:
        robot = await store.create_robot(
            {
                "name": "Элвин",
                "role_prompt": "Только этот системный prompt",
                "knowledge_base": "Только эта база знаний",
                "stop_list_condition": "Человек потребовал больше не звонить",
                "call_end_condition": "После {{stage:stop_list}} вызови end_call",
                "call_end_wait_ms": 9300,
                "call_end_delay_ms": 450,
            }
        )
        assert robot["call_end_condition"] == (
            "После {{stage:stop_list}} вызови end_call"
        )
        assert robot["call_end_wait_ms"] == 9300
        assert robot["call_end_delay_ms"] == 450

    asyncio.run(exercise())

    query, values = pool.calls[-1]
    assert "call_end_condition" in query
    assert "$25" in query
    assert len(values) == 25
    assert values[15] == "После {{stage:stop_list}} вызови end_call"
    assert values[16:18] == (9300, 450)


def test_postgres_robot_update_uses_matching_placeholder_order(
    tmp_path: Path,
) -> None:
    store = StateStore(_settings(tmp_path))
    pool = FakeRobotPool()
    store.mode = "postgres"
    store.pool = pool  # type: ignore[assignment]
    robot_id = "11111111-1111-1111-1111-111111111111"

    async def exercise() -> None:
        robot = await store.update_robot(
            robot_id,
            {
                "name": "Элвин",
                "role_prompt": "Видимый prompt",
                "knowledge_base": "Видимая база",
                "call_end_condition": "Логическое завершение разговора",
                "call_end_wait_ms": 7000,
                "call_end_delay_ms": 200,
            },
        )
        assert robot is not None
        assert robot["call_end_condition"] == "Логическое завершение разговора"

    asyncio.run(exercise())

    query, values = pool.calls[-1]
    assert "active=$25" in query
    assert len(values) == 25
    assert values[0] == robot_id
    assert values[15] == "Логическое завершение разговора"
    assert values[16:18] == (7000, 200)
