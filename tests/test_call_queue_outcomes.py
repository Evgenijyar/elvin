import asyncio
from types import SimpleNamespace

from elvin.services.call_queue import CallQueueManager


class _Store:
    def __init__(self) -> None:
        self.item_updates: list[tuple[str, dict[str, object]]] = []
        self.batch_increments: list[tuple[str, dict[str, int]]] = []

    async def update_call_item(self, item_id: str, **fields: object) -> None:
        self.item_updates.append((item_id, fields))

    async def increment_call_batch(self, batch_id: str, **fields: int) -> None:
        self.batch_increments.append((batch_id, fields))


class _LPTracker:
    def __init__(self) -> None:
        self.moves: list[tuple[str, int, int]] = []

    async def move_lead_to_stage(
        self, token: str, lead_id: int, stage_id: int
    ) -> None:
        self.moves.append((token, lead_id, stage_id))


class _Timeline:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def add(self, name: str, **payload: object) -> None:
        self.events.append((name, payload))


def _manager() -> tuple[CallQueueManager, _Store, _LPTracker]:
    store = _Store()
    lptracker = _LPTracker()
    manager = object.__new__(CallQueueManager)
    manager.store = store
    manager.lptracker = lptracker
    return manager, store, lptracker


def test_limits_stop_on_whichever_is_reached_first() -> None:
    assert (
        CallQueueManager._limit_stop_reason(
            {"calls_made": 4, "leads_count": 2},
            call_limit=4,
            lead_limit=3,
        )
        == "call_limit"
    )
    assert (
        CallQueueManager._limit_stop_reason(
            {"calls_made": 3, "leads_count": 2},
            call_limit=4,
            lead_limit=2,
        )
        == "lead_limit"
    )
    assert (
        CallQueueManager._limit_stop_reason(
            {"calls_made": 3, "leads_count": 2},
            call_limit=4,
            lead_limit=0,
        )
        == ""
    )


def test_special_outcome_moves_stage_and_counts_as_lead_when_enabled() -> None:
    manager, store, lptracker = _manager()
    assignment = {
        "special_stage_id": 77,
        "special_stage_name": "Видеовстреча",
        "count_special_as_lead": True,
    }
    item = {"id": "item-1", "batch_id": "batch-1", "lead_id": 123}
    voice_call = SimpleNamespace(timeline=_Timeline())

    async def exercise() -> None:
        error = await manager._record_outcome(
            token="token",
            assignment=assignment,
            item=item,
            outcome="special",
            voice_call=voice_call,
        )
        assert error == ""

    asyncio.run(exercise())

    assert lptracker.moves == [("token", 123, 77)]
    assert store.item_updates == [
        (
            "item-1",
            {
                "outcome": "special",
                "destination_stage_id": 77,
                "destination_stage_name": "Видеовстреча",
                "error_message": "",
            },
        )
    ]
    assert store.batch_increments == [("batch-1", {"leads_count": 1})]


def test_no_answer_moves_stage_without_incrementing_lead_counter() -> None:
    manager, store, lptracker = _manager()
    assignment = {
        "no_answer_stage_id": 88,
        "no_answer_stage_name": "Недозвон",
        "count_special_as_lead": True,
    }
    item = {"id": "item-2", "batch_id": "batch-2", "lead_id": 321}
    voice_call = SimpleNamespace(timeline=_Timeline())

    async def exercise() -> None:
        await manager._record_outcome(
            token="token",
            assignment=assignment,
            item=item,
            outcome="no_answer",
            voice_call=voice_call,
        )

    asyncio.run(exercise())

    assert lptracker.moves == [("token", 321, 88)]
    assert store.item_updates[0][1]["outcome"] == "no_answer"
    assert store.batch_increments == []
