"""Persistent Elvin state backed by PostgreSQL or a local JSON file."""

import asyncio
import json
import ssl
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import asyncpg
from asyncpg import Pool

from elvin.config import Settings


DEFAULT_STATE: dict[str, Any] = {
    "settings": {},
    "robots": [],
    "assignments": [],
    "call_batches": [],
    "call_queue_items": [],
    "webhooks": [],
}

ROBOT_DEFAULTS: dict[str, Any] = {
    "lead_condition": "",
    "special_condition": "",
    "refusal_condition": "",
    "callback_condition": "",
    "stop_list_condition": "",
    "answering_machine_condition": "",
}

ASSIGNMENT_DEFAULTS: dict[str, Any] = {
    "source_stage_id": None,
    "source_stage_name": "",
    "call_limit": 50,
    "lead_limit": 0,
    "max_call_minutes": 5,
    "lead_stage_id": None,
    "lead_stage_name": "",
    "special_stage_id": None,
    "special_stage_name": "",
    "refusal_stage_id": None,
    "refusal_stage_name": "",
    "callback_stage_id": None,
    "callback_stage_name": "",
    "stop_list_stage_id": None,
    "stop_list_stage_name": "",
    "answering_machine_stage_id": None,
    "answering_machine_stage_name": "",
    "no_answer_stage_id": None,
    "no_answer_stage_name": "",
    "count_special_as_lead": False,
    "background_audio_filename": "",
    "background_audio_volume": 15,
}

CALL_BATCH_DEFAULTS: dict[str, Any] = {
    "calls_made": 0,
    "leads_count": 0,
    "stop_reason": "",
}

CALL_ITEM_DEFAULTS: dict[str, Any] = {
    "outcome": "",
    "destination_stage_id": None,
    "destination_stage_name": "",
}


class StateStore:
    """Application state storage with a real PostgreSQL production mode."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool: Pool | None = None
        self.mode = "initializing"
        self.last_error: str | None = None
        self.local_path = settings.data_dir / "elvin-state.json"
        self._local_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        self.settings.recordings_dir.mkdir(parents=True, exist_ok=True)

        if not self.settings.database_configured:
            await self._ensure_local_file()
            self.mode = "local_file"
            return

        try:
            self.pool = await asyncpg.create_pool(
                host=self.settings.db_host,
                port=self.settings.db_port,
                database=self.settings.db_name,
                user=self.settings.db_user,
                password=self.settings.db_password.get_secret_value()
                if self.settings.db_password
                else None,
                ssl=self._ssl_argument(),
                min_size=self.settings.db_pool_min_size,
                max_size=self.settings.db_pool_max_size,
                timeout=self.settings.db_connect_timeout_seconds,
                command_timeout=self.settings.db_command_timeout_seconds,
                server_settings={"application_name": "elvin-backend"},
            )
            await self._initialize_schema()
            self.mode = "postgres"
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.mode = "unavailable"

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    def _ssl_argument(self) -> bool | ssl.SSLContext:
        mode = self.settings.db_sslmode
        if mode == "disable":
            return False
        context = ssl.create_default_context()
        if mode in {"allow", "prefer", "require"}:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    async def _initialize_schema(self) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as connection:
            await connection.execute("CREATE SCHEMA IF NOT EXISTS app")
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app.elvin_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app.robot_profiles (
                    id UUID PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    model_id TEXT NOT NULL,
                    voice_name TEXT NOT NULL,
                    temperature DOUBLE PRECISION NOT NULL DEFAULT 0.3,
                    role_prompt TEXT NOT NULL DEFAULT '',
                    knowledge_base TEXT NOT NULL DEFAULT '',
                    first_phrase TEXT NOT NULL DEFAULT '',
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app.project_robot_assignments (
                    id UUID PRIMARY KEY,
                    project_id BIGINT NOT NULL,
                    project_name TEXT NOT NULL,
                    robot_id UUID NOT NULL REFERENCES app.robot_profiles(id)
                        ON DELETE CASCADE,
                    source_stage_id BIGINT,
                    source_stage_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'STOPPED',
                    webhook_registered BOOLEAN NOT NULL DEFAULT FALSE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(project_id, robot_id)
                )
                """
            )
            await connection.execute(
                """
                ALTER TABLE app.project_robot_assignments
                ADD COLUMN IF NOT EXISTS call_limit INTEGER NOT NULL DEFAULT 50
                """
            )
            await connection.execute(
                """
                ALTER TABLE app.project_robot_assignments
                ADD COLUMN IF NOT EXISTS max_call_minutes INTEGER NOT NULL DEFAULT 5
                """
            )
            for statement in (
                "ALTER TABLE app.robot_profiles ADD COLUMN IF NOT EXISTS lead_condition TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.robot_profiles ADD COLUMN IF NOT EXISTS special_condition TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.robot_profiles ADD COLUMN IF NOT EXISTS refusal_condition TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.robot_profiles ADD COLUMN IF NOT EXISTS callback_condition TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.robot_profiles ADD COLUMN IF NOT EXISTS stop_list_condition TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.robot_profiles ADD COLUMN IF NOT EXISTS answering_machine_condition TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS lead_stage_id BIGINT",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS lead_stage_name TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS special_stage_id BIGINT",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS special_stage_name TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS refusal_stage_id BIGINT",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS refusal_stage_name TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS callback_stage_id BIGINT",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS callback_stage_name TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS stop_list_stage_id BIGINT",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS stop_list_stage_name TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS answering_machine_stage_id BIGINT",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS answering_machine_stage_name TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS no_answer_stage_id BIGINT",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS no_answer_stage_name TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS count_special_as_lead BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS lead_limit INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS background_audio_filename TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.project_robot_assignments ADD COLUMN IF NOT EXISTS background_audio_volume INTEGER NOT NULL DEFAULT 15",
            ):
                await connection.execute(statement)
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app.call_batches (
                    id UUID PRIMARY KEY,
                    assignment_id UUID NOT NULL REFERENCES app.project_robot_assignments(id)
                        ON DELETE CASCADE,
                    project_id BIGINT NOT NULL,
                    robot_id UUID NOT NULL REFERENCES app.robot_profiles(id)
                        ON DELETE CASCADE,
                    source_stage_id BIGINT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'QUEUE_READY',
                    total INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    current_position INTEGER,
                    current_lead_id BIGINT,
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app.call_queue_items (
                    id UUID PRIMARY KEY,
                    batch_id UUID NOT NULL REFERENCES app.call_batches(id)
                        ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    lead_id BIGINT NOT NULL,
                    lead_name TEXT NOT NULL DEFAULT '',
                    contact_name TEXT NOT NULL DEFAULT '',
                    phone_masked TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    result TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(batch_id, position),
                    UNIQUE(batch_id, lead_id)
                )
                """
            )
            for statement in (
                "ALTER TABLE app.call_batches ADD COLUMN IF NOT EXISTS calls_made INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE app.call_batches ADD COLUMN IF NOT EXISTS leads_count INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE app.call_batches ADD COLUMN IF NOT EXISTS stop_reason TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.call_queue_items ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE app.call_queue_items ADD COLUMN IF NOT EXISTS destination_stage_id BIGINT",
                "ALTER TABLE app.call_queue_items ADD COLUMN IF NOT EXISTS destination_stage_name TEXT NOT NULL DEFAULT ''",
            ):
                await connection.execute(statement)

            await connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_call_batches_assignment_created
                ON app.call_batches(assignment_id, created_at DESC)
                """
            )
            await connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_call_queue_items_batch_status_position
                ON app.call_queue_items(batch_id, status, position)
                """
            )
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app.lptracker_webhook_events (
                    id BIGSERIAL PRIMARY KEY,
                    project_id BIGINT,
                    http_method TEXT NOT NULL,
                    content_type TEXT NOT NULL DEFAULT '',
                    payload JSONB NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

    async def ping(self) -> bool:
        if self.mode == "local_file":
            return self.local_path.exists()
        if self.mode != "postgres" or self.pool is None:
            return False
        return await self.pool.fetchval("SELECT 1") == 1

    async def get_setting(self, key: str) -> str | None:
        if self.mode == "postgres" and self.pool is not None:
            return await self.pool.fetchval(
                "SELECT value FROM app.elvin_settings WHERE key=$1",
                key,
            )
        state = await self._read_local()
        value = state["settings"].get(key)
        return str(value) if value is not None else None

    async def set_setting(self, key: str, value: str) -> None:
        if self.mode == "postgres" and self.pool is not None:
            await self.pool.execute(
                """
                INSERT INTO app.elvin_settings(key, value, updated_at)
                VALUES($1, $2, NOW())
                ON CONFLICT(key) DO UPDATE
                SET value=EXCLUDED.value, updated_at=NOW()
                """,
                key,
                value,
            )
            return
        async with self._local_lock:
            state = await self._read_local_unlocked()
            state["settings"][key] = value
            await self._write_local_unlocked(state)

    async def delete_settings(self, keys: list[str]) -> None:
        if self.mode == "postgres" and self.pool is not None:
            await self.pool.execute(
                "DELETE FROM app.elvin_settings WHERE key = ANY($1::text[])",
                keys,
            )
            return
        async with self._local_lock:
            state = await self._read_local_unlocked()
            for key in keys:
                state["settings"].pop(key, None)
            await self._write_local_unlocked(state)

    async def list_robots(self) -> list[dict[str, Any]]:
        if self.mode == "postgres" and self.pool is not None:
            rows = await self.pool.fetch(
                """
                SELECT id, name, description, model_id, voice_name,
                       temperature, role_prompt, knowledge_base,
                       first_phrase, lead_condition, special_condition,
                       refusal_condition, callback_condition,
                       stop_list_condition, answering_machine_condition,
                       active, created_at, updated_at
                FROM app.robot_profiles
                ORDER BY updated_at DESC, name ASC
                """
            )
            return [self._robot_row(row) for row in rows]
        state = await self._read_local()
        robots = []
        for stored in state["robots"]:
            item = deepcopy(stored)
            for key, default in ROBOT_DEFAULTS.items():
                item.setdefault(key, deepcopy(default))
            robots.append(item)
        return sorted(
            robots,
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )

    async def get_robot(self, robot_id: str) -> dict[str, Any] | None:
        robots = await self.list_robots()
        return next((item for item in robots if item["id"] == robot_id), None)

    async def create_robot(self, payload: dict[str, Any]) -> dict[str, Any]:
        robot_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        item = {
            "id": robot_id,
            "name": payload["name"],
            "description": payload.get("description", ""),
            "model_id": payload.get(
                "model_id",
                "gemini-3.1-flash-live-preview",
            ),
            "voice_name": payload.get("voice_name", "Kore"),
            "temperature": float(payload.get("temperature", 0.3)),
            "role_prompt": payload.get("role_prompt", ""),
            "knowledge_base": payload.get("knowledge_base", ""),
            "first_phrase": payload.get("first_phrase", ""),
            "lead_condition": payload.get("lead_condition", ""),
            "special_condition": payload.get("special_condition", ""),
            "refusal_condition": payload.get("refusal_condition", ""),
            "callback_condition": payload.get("callback_condition", ""),
            "stop_list_condition": payload.get("stop_list_condition", ""),
            "answering_machine_condition": payload.get(
                "answering_machine_condition", ""
            ),
            "active": bool(payload.get("active", True)),
            "created_at": now,
            "updated_at": now,
        }
        if self.mode == "postgres" and self.pool is not None:
            row = await self.pool.fetchrow(
                """
                INSERT INTO app.robot_profiles(
                    id, name, description, model_id, voice_name,
                    temperature, role_prompt, knowledge_base,
                    first_phrase, lead_condition, special_condition,
                    refusal_condition, callback_condition,
                    stop_list_condition, answering_machine_condition, active
                ) VALUES(
                    $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15, $16
                )
                RETURNING id, name, description, model_id, voice_name,
                          temperature, role_prompt, knowledge_base,
                          first_phrase, lead_condition, special_condition,
                          refusal_condition, callback_condition,
                          stop_list_condition, answering_machine_condition,
                          active, created_at, updated_at
                """,
                robot_id,
                item["name"],
                item["description"],
                item["model_id"],
                item["voice_name"],
                item["temperature"],
                item["role_prompt"],
                item["knowledge_base"],
                item["first_phrase"],
                item["lead_condition"],
                item["special_condition"],
                item["refusal_condition"],
                item["callback_condition"],
                item["stop_list_condition"],
                item["answering_machine_condition"],
                item["active"],
            )
            return self._robot_row(row)
        async with self._local_lock:
            state = await self._read_local_unlocked()
            state["robots"].append(item)
            await self._write_local_unlocked(state)
        return item

    async def update_robot(
        self,
        robot_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self.mode == "postgres" and self.pool is not None:
            row = await self.pool.fetchrow(
                """
                UPDATE app.robot_profiles
                SET name=$2, description=$3, model_id=$4, voice_name=$5,
                    temperature=$6, role_prompt=$7, knowledge_base=$8,
                    first_phrase=$9, lead_condition=$10, special_condition=$11,
                    refusal_condition=$12, callback_condition=$13,
                    stop_list_condition=$14, answering_machine_condition=$15,
                    active=$16, updated_at=NOW()
                WHERE id=$1::uuid
                RETURNING id, name, description, model_id, voice_name,
                          temperature, role_prompt, knowledge_base,
                          first_phrase, lead_condition, special_condition,
                          refusal_condition, callback_condition,
                          stop_list_condition, answering_machine_condition,
                          active, created_at, updated_at
                """,
                robot_id,
                payload["name"],
                payload.get("description", ""),
                payload.get("model_id", "gemini-3.1-flash-live-preview"),
                payload.get("voice_name", "Kore"),
                float(payload.get("temperature", 0.3)),
                payload.get("role_prompt", ""),
                payload.get("knowledge_base", ""),
                payload.get("first_phrase", ""),
                payload.get("lead_condition", ""),
                payload.get("special_condition", ""),
                payload.get("refusal_condition", ""),
                payload.get("callback_condition", ""),
                payload.get("stop_list_condition", ""),
                payload.get("answering_machine_condition", ""),
                bool(payload.get("active", True)),
            )
            return self._robot_row(row) if row else None
        async with self._local_lock:
            state = await self._read_local_unlocked()
            item = next(
                (robot for robot in state["robots"] if robot["id"] == robot_id),
                None,
            )
            if item is None:
                return None
            item.update(
                {
                    "name": payload["name"],
                    "description": payload.get("description", ""),
                    "model_id": payload.get(
                        "model_id",
                        "gemini-3.1-flash-live-preview",
                    ),
                    "voice_name": payload.get("voice_name", "Kore"),
                    "temperature": float(payload.get("temperature", 0.3)),
                    "role_prompt": payload.get("role_prompt", ""),
                    "knowledge_base": payload.get("knowledge_base", ""),
                    "first_phrase": payload.get("first_phrase", ""),
                    "lead_condition": payload.get("lead_condition", ""),
                    "special_condition": payload.get("special_condition", ""),
                    "refusal_condition": payload.get("refusal_condition", ""),
                    "callback_condition": payload.get("callback_condition", ""),
                    "stop_list_condition": payload.get("stop_list_condition", ""),
                    "answering_machine_condition": payload.get(
                        "answering_machine_condition", ""
                    ),
                    "active": bool(payload.get("active", True)),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            await self._write_local_unlocked(state)
            return deepcopy(item)

    async def delete_robot(self, robot_id: str) -> bool:
        if self.mode == "postgres" and self.pool is not None:
            result = await self.pool.execute(
                "DELETE FROM app.robot_profiles WHERE id=$1::uuid",
                robot_id,
            )
            return result.endswith("1")
        async with self._local_lock:
            state = await self._read_local_unlocked()
            before = len(state["robots"])
            state["robots"] = [
                item for item in state["robots"] if item["id"] != robot_id
            ]
            state["assignments"] = [
                item for item in state["assignments"] if item["robot_id"] != robot_id
            ]
            await self._write_local_unlocked(state)
            return len(state["robots"]) != before

    async def list_assignments(self) -> list[dict[str, Any]]:
        if self.mode == "postgres" and self.pool is not None:
            rows = await self.pool.fetch(
                """
                SELECT a.id, a.project_id, a.project_name, a.robot_id,
                       a.source_stage_id, a.source_stage_name, a.status,
                       a.webhook_registered, a.sort_order,
                       a.call_limit, a.lead_limit, a.max_call_minutes,
                       a.lead_stage_id, a.lead_stage_name,
                       a.special_stage_id, a.special_stage_name,
                       a.refusal_stage_id, a.refusal_stage_name,
                       a.callback_stage_id, a.callback_stage_name,
                       a.stop_list_stage_id, a.stop_list_stage_name,
                       a.answering_machine_stage_id, a.answering_machine_stage_name,
                       a.no_answer_stage_id, a.no_answer_stage_name,
                       a.count_special_as_lead,
                       a.background_audio_filename, a.background_audio_volume,
                       a.created_at, a.updated_at,
                       r.name AS robot_name, r.description AS robot_description,
                       r.model_id, r.voice_name
                FROM app.project_robot_assignments a
                JOIN app.robot_profiles r ON r.id = a.robot_id
                ORDER BY a.sort_order ASC, a.created_at ASC
                """
            )
            return [self._assignment_row(row) for row in rows]
        state = await self._read_local()
        robots = {item["id"]: item for item in state["robots"]}
        result: list[dict[str, Any]] = []
        for assignment in state["assignments"]:
            robot = robots.get(assignment["robot_id"])
            if robot is None:
                continue
            merged = deepcopy(assignment)
            for key, default in ASSIGNMENT_DEFAULTS.items():
                merged.setdefault(key, deepcopy(default))
            merged.update(
                {
                    "robot_name": robot["name"],
                    "robot_description": robot.get("description", ""),
                    "model_id": robot["model_id"],
                    "voice_name": robot["voice_name"],
                }
            )
            result.append(merged)
        return sorted(result, key=lambda item: item.get("sort_order", 0))

    async def get_assignment(self, assignment_id: str) -> dict[str, Any] | None:
        assignments = await self.list_assignments()
        return next(
            (item for item in assignments if item["id"] == assignment_id),
            None,
        )

    async def create_assignment(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        assignment_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        item = {
            "id": assignment_id,
            "project_id": int(payload["project_id"]),
            "project_name": payload["project_name"],
            "robot_id": payload["robot_id"],
            "source_stage_id": None,
            "source_stage_name": "",
            "status": "STOPPED",
            "webhook_registered": False,
            "sort_order": int(payload.get("sort_order", 0)),
            "call_limit": int(payload.get("call_limit", 50)),
            "lead_limit": int(payload.get("lead_limit", 0)),
            "max_call_minutes": int(payload.get("max_call_minutes", 5)),
            "lead_stage_id": None,
            "lead_stage_name": "",
            "special_stage_id": None,
            "special_stage_name": "",
            "refusal_stage_id": None,
            "refusal_stage_name": "",
            "callback_stage_id": None,
            "callback_stage_name": "",
            "stop_list_stage_id": None,
            "stop_list_stage_name": "",
            "answering_machine_stage_id": None,
            "answering_machine_stage_name": "",
            "no_answer_stage_id": None,
            "no_answer_stage_name": "",
            "count_special_as_lead": False,
            "background_audio_filename": "",
            "background_audio_volume": 15,
            "created_at": now,
            "updated_at": now,
        }
        if self.mode == "postgres" and self.pool is not None:
            row = await self.pool.fetchrow(
                """
                INSERT INTO app.project_robot_assignments(
                    id, project_id, project_name, robot_id, sort_order,
                    call_limit, lead_limit, max_call_minutes
                ) VALUES($1::uuid, $2, $3, $4::uuid, $5, $6, $7, $8)
                RETURNING *
                """,
                assignment_id,
                item["project_id"],
                item["project_name"],
                item["robot_id"],
                item["sort_order"],
                item["call_limit"],
                item["lead_limit"],
                item["max_call_minutes"],
            )
            return self._assignment_row(row)
        async with self._local_lock:
            state = await self._read_local_unlocked()
            duplicate = next(
                (
                    existing
                    for existing in state["assignments"]
                    if existing["project_id"] == item["project_id"]
                    and existing["robot_id"] == item["robot_id"]
                ),
                None,
            )
            if duplicate:
                raise ValueError("Этот робот уже добавлен в выбранный проект.")
            state["assignments"].append(item)
            await self._write_local_unlocked(state)
        return item

    async def update_assignment(
        self,
        assignment_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        allowed = {
            "source_stage_id",
            "source_stage_name",
            "status",
            "webhook_registered",
            "call_limit",
            "lead_limit",
            "max_call_minutes",
            "lead_stage_id",
            "lead_stage_name",
            "special_stage_id",
            "special_stage_name",
            "refusal_stage_id",
            "refusal_stage_name",
            "callback_stage_id",
            "callback_stage_name",
            "stop_list_stage_id",
            "stop_list_stage_name",
            "answering_machine_stage_id",
            "answering_machine_stage_name",
            "no_answer_stage_id",
            "no_answer_stage_name",
            "count_special_as_lead",
            "background_audio_filename",
            "background_audio_volume",
        }
        updates = {key: value for key, value in payload.items() if key in allowed}
        if not updates:
            return await self.get_assignment(assignment_id)
        if self.mode == "postgres" and self.pool is not None:
            assignments: list[str] = []
            values: list[Any] = [assignment_id]
            for index, (key, value) in enumerate(updates.items(), start=2):
                assignments.append(f"{key}=${index}")
                values.append(value)
            assignments.append("updated_at=NOW()")
            row = await self.pool.fetchrow(
                f"UPDATE app.project_robot_assignments "
                f"SET {', '.join(assignments)} WHERE id=$1::uuid RETURNING *",
                *values,
            )
            return self._assignment_row(row) if row else None
        async with self._local_lock:
            state = await self._read_local_unlocked()
            item = next(
                (
                    assignment
                    for assignment in state["assignments"]
                    if assignment["id"] == assignment_id
                ),
                None,
            )
            if item is None:
                return None
            item.update(updates)
            item["updated_at"] = datetime.now(UTC).isoformat()
            await self._write_local_unlocked(state)
            return deepcopy(item)

    async def delete_assignment(self, assignment_id: str) -> bool:
        if self.mode == "postgres" and self.pool is not None:
            result = await self.pool.execute(
                "DELETE FROM app.project_robot_assignments WHERE id=$1::uuid",
                assignment_id,
            )
            return result.endswith("1")
        async with self._local_lock:
            state = await self._read_local_unlocked()
            before = len(state["assignments"])
            state["assignments"] = [
                item for item in state["assignments"] if item["id"] != assignment_id
            ]
            await self._write_local_unlocked(state)
            return len(state["assignments"]) != before

    async def create_call_batch(
        self,
        *,
        assignment_id: str,
        project_id: int,
        robot_id: str,
        source_stage_id: int,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        batch_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        batch = {
            "id": batch_id,
            "assignment_id": assignment_id,
            "project_id": project_id,
            "robot_id": robot_id,
            "source_stage_id": source_stage_id,
            "status": "QUEUE_READY",
            "total": len(items),
            "completed": 0,
            "failed": 0,
            "calls_made": 0,
            "leads_count": 0,
            "stop_reason": "",
            "current_position": None,
            "current_lead_id": None,
            "error_message": "",
            "created_at": now,
            "updated_at": now,
        }
        queue_items = [
            {
                "id": str(uuid4()),
                "batch_id": batch_id,
                "position": index,
                "lead_id": int(item["lead_id"]),
                "lead_name": str(item.get("lead_name") or ""),
                "contact_name": str(item.get("contact_name") or ""),
                "phone_masked": str(item.get("phone") or ""),
                "status": "PENDING",
                "result": "",
                "outcome": "",
                "destination_stage_id": None,
                "destination_stage_name": "",
                "error_message": "",
                "created_at": now,
                "updated_at": now,
            }
            for index, item in enumerate(items, start=1)
        ]

        if self.mode == "postgres" and self.pool is not None:
            async with self.pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        """
                        UPDATE app.call_batches
                        SET status='STOPPED', updated_at=NOW()
                        WHERE assignment_id=$1::uuid
                          AND status IN (
                              'QUEUE_READY', 'RUNNING', 'CALL_REQUESTING',
                              'WAITING_FOR_MEDIA', 'IN_CALL', 'STOPPING'
                          )
                        """,
                        assignment_id,
                    )
                    row = await connection.fetchrow(
                        """
                        INSERT INTO app.call_batches(
                            id, assignment_id, project_id, robot_id,
                            source_stage_id, status, total
                        ) VALUES(
                            $1::uuid, $2::uuid, $3, $4::uuid, $5, $6, $7
                        )
                        RETURNING *
                        """,
                        batch_id,
                        assignment_id,
                        project_id,
                        robot_id,
                        source_stage_id,
                        batch["status"],
                        batch["total"],
                    )
                    await connection.executemany(
                        """
                        INSERT INTO app.call_queue_items(
                            id, batch_id, position, lead_id, lead_name,
                            contact_name, phone_masked, status
                        ) VALUES(
                            $1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8
                        )
                        """,
                        [
                            (
                                item["id"],
                                item["batch_id"],
                                item["position"],
                                item["lead_id"],
                                item["lead_name"],
                                item["contact_name"],
                                item["phone_masked"],
                                item["status"],
                            )
                            for item in queue_items
                        ],
                    )
            return self._call_batch_row(row)

        async with self._local_lock:
            state = await self._read_local_unlocked()
            for existing in state["call_batches"]:
                if existing["assignment_id"] == assignment_id and existing[
                    "status"
                ] in {
                    "QUEUE_READY",
                    "RUNNING",
                    "CALL_REQUESTING",
                    "WAITING_FOR_MEDIA",
                    "IN_CALL",
                    "STOPPING",
                }:
                    existing["status"] = "STOPPED"
                    existing["updated_at"] = now
            state["call_batches"].append(batch)
            state["call_queue_items"].extend(queue_items)
            await self._write_local_unlocked(state)
        return deepcopy(batch)

    async def get_latest_call_batch(
        self,
        assignment_id: str,
    ) -> dict[str, Any] | None:
        if self.mode == "postgres" and self.pool is not None:
            row = await self.pool.fetchrow(
                """
                SELECT * FROM app.call_batches
                WHERE assignment_id=$1::uuid
                ORDER BY created_at DESC
                LIMIT 1
                """,
                assignment_id,
            )
            return self._call_batch_row(row) if row else None
        state = await self._read_local()
        rows = [
            item
            for item in state["call_batches"]
            if item["assignment_id"] == assignment_id
        ]
        if not rows:
            return None
        item = deepcopy(
            sorted(rows, key=lambda item: item["created_at"], reverse=True)[0]
        )
        for key, default in CALL_BATCH_DEFAULTS.items():
            item.setdefault(key, deepcopy(default))
        return item

    async def get_call_batch(self, batch_id: str) -> dict[str, Any] | None:
        if self.mode == "postgres" and self.pool is not None:
            row = await self.pool.fetchrow(
                "SELECT * FROM app.call_batches WHERE id=$1::uuid",
                batch_id,
            )
            return self._call_batch_row(row) if row else None
        state = await self._read_local()
        item = next(
            (row for row in state["call_batches"] if row["id"] == batch_id),
            None,
        )
        if item is None:
            return None
        result = deepcopy(item)
        for key, default in CALL_BATCH_DEFAULTS.items():
            result.setdefault(key, deepcopy(default))
        return result

    async def list_call_items(self, batch_id: str) -> list[dict[str, Any]]:
        if self.mode == "postgres" and self.pool is not None:
            rows = await self.pool.fetch(
                """
                SELECT * FROM app.call_queue_items
                WHERE batch_id=$1::uuid
                ORDER BY position ASC
                """,
                batch_id,
            )
            return [self._call_item_row(row) for row in rows]
        state = await self._read_local()
        rows = []
        for stored in state["call_queue_items"]:
            if stored["batch_id"] != batch_id:
                continue
            item = deepcopy(stored)
            for key, default in CALL_ITEM_DEFAULTS.items():
                item.setdefault(key, deepcopy(default))
            rows.append(item)
        return sorted(rows, key=lambda item: int(item["position"]))

    async def next_pending_call_item(
        self,
        batch_id: str,
    ) -> dict[str, Any] | None:
        if self.mode == "postgres" and self.pool is not None:
            row = await self.pool.fetchrow(
                """
                SELECT * FROM app.call_queue_items
                WHERE batch_id=$1::uuid AND status='PENDING'
                ORDER BY position ASC
                LIMIT 1
                """,
                batch_id,
            )
            return self._call_item_row(row) if row else None
        items = await self.list_call_items(batch_id)
        return next((item for item in items if item["status"] == "PENDING"), None)

    async def update_call_batch(self, batch_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "current_position",
            "current_lead_id",
            "error_message",
            "stop_reason",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        if self.mode == "postgres" and self.pool is not None:
            assignments = []
            values: list[Any] = [batch_id]
            for index, (key, value) in enumerate(updates.items(), start=2):
                assignments.append(f"{key}=${index}")
                values.append(value)
            assignments.append("updated_at=NOW()")
            await self.pool.execute(
                f"UPDATE app.call_batches SET {', '.join(assignments)} "
                "WHERE id=$1::uuid",
                *values,
            )
            return
        async with self._local_lock:
            state = await self._read_local_unlocked()
            item = next(
                (row for row in state["call_batches"] if row["id"] == batch_id),
                None,
            )
            if item is not None:
                item.update(updates)
                item["updated_at"] = datetime.now(UTC).isoformat()
                await self._write_local_unlocked(state)

    async def increment_call_batch(
        self,
        batch_id: str,
        *,
        completed: int = 0,
        failed: int = 0,
        calls_made: int = 0,
        leads_count: int = 0,
    ) -> None:
        if self.mode == "postgres" and self.pool is not None:
            await self.pool.execute(
                """
                UPDATE app.call_batches
                SET completed=completed+$2,
                    failed=failed+$3,
                    calls_made=calls_made+$4,
                    leads_count=leads_count+$5,
                    updated_at=NOW()
                WHERE id=$1::uuid
                """,
                batch_id,
                completed,
                failed,
                calls_made,
                leads_count,
            )
            return
        async with self._local_lock:
            state = await self._read_local_unlocked()
            item = next(
                (row for row in state["call_batches"] if row["id"] == batch_id),
                None,
            )
            if item is not None:
                item["completed"] = int(item.get("completed", 0)) + completed
                item["failed"] = int(item.get("failed", 0)) + failed
                item["calls_made"] = int(item.get("calls_made", 0)) + calls_made
                item["leads_count"] = int(item.get("leads_count", 0)) + leads_count
                item["updated_at"] = datetime.now(UTC).isoformat()
                await self._write_local_unlocked(state)

    async def update_call_item(self, item_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "result",
            "error_message",
            "outcome",
            "destination_stage_id",
            "destination_stage_name",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        if self.mode == "postgres" and self.pool is not None:
            assignments = []
            values: list[Any] = [item_id]
            for index, (key, value) in enumerate(updates.items(), start=2):
                assignments.append(f"{key}=${index}")
                values.append(value)
            assignments.append("updated_at=NOW()")
            await self.pool.execute(
                f"UPDATE app.call_queue_items SET {', '.join(assignments)} "
                "WHERE id=$1::uuid",
                *values,
            )
            return
        async with self._local_lock:
            state = await self._read_local_unlocked()
            item = next(
                (row for row in state["call_queue_items"] if row["id"] == item_id),
                None,
            )
            if item is not None:
                item.update(updates)
                item["updated_at"] = datetime.now(UTC).isoformat()
                await self._write_local_unlocked(state)

    async def save_webhook_event(
        self,
        project_id: int | None,
        method: str,
        content_type: str,
        payload: dict[str, Any],
    ) -> None:
        if self.mode == "postgres" and self.pool is not None:
            await self.pool.execute(
                """
                INSERT INTO app.lptracker_webhook_events(
                    project_id, http_method, content_type, payload
                ) VALUES($1, $2, $3, $4::jsonb)
                """,
                project_id,
                method,
                content_type,
                json.dumps(payload, ensure_ascii=False),
            )
            return
        async with self._local_lock:
            state = await self._read_local_unlocked()
            state["webhooks"].append(
                {
                    "project_id": project_id,
                    "method": method,
                    "content_type": content_type,
                    "payload": payload,
                    "received_at": datetime.now(UTC).isoformat(),
                }
            )
            state["webhooks"] = state["webhooks"][-500:]
            await self._write_local_unlocked(state)

    async def _ensure_local_file(self) -> None:
        if not self.local_path.exists():
            await asyncio.to_thread(
                self.local_path.write_text,
                json.dumps(DEFAULT_STATE, ensure_ascii=False, indent=2),
                "utf-8",
            )

    async def _read_local(self) -> dict[str, Any]:
        async with self._local_lock:
            return await self._read_local_unlocked()

    async def _read_local_unlocked(self) -> dict[str, Any]:
        await self._ensure_local_file()
        text = await asyncio.to_thread(self.local_path.read_text, "utf-8")
        try:
            state = json.loads(text)
        except json.JSONDecodeError:
            state = deepcopy(DEFAULT_STATE)
        for key, default in DEFAULT_STATE.items():
            state.setdefault(key, deepcopy(default))
        return state

    async def _write_local_unlocked(self, state: dict[str, Any]) -> None:
        temp_path = self.local_path.with_suffix(".tmp")
        text = json.dumps(state, ensure_ascii=False, indent=2)
        await asyncio.to_thread(temp_path.write_text, text, "utf-8")
        await asyncio.to_thread(temp_path.replace, self.local_path)

    def _robot_row(self, row: asyncpg.Record) -> dict[str, Any]:
        item = dict(row)
        item["id"] = str(item["id"])
        item["created_at"] = item["created_at"].isoformat()
        item["updated_at"] = item["updated_at"].isoformat()
        for key, default in ROBOT_DEFAULTS.items():
            item.setdefault(key, deepcopy(default))
        return item

    def _call_batch_row(self, row: asyncpg.Record) -> dict[str, Any]:
        item = dict(row)
        item["id"] = str(item["id"])
        item["assignment_id"] = str(item["assignment_id"])
        item["robot_id"] = str(item["robot_id"])
        item["created_at"] = item["created_at"].isoformat()
        item["updated_at"] = item["updated_at"].isoformat()
        for key, default in CALL_BATCH_DEFAULTS.items():
            item.setdefault(key, deepcopy(default))
        return item

    def _call_item_row(self, row: asyncpg.Record) -> dict[str, Any]:
        item = dict(row)
        item["id"] = str(item["id"])
        item["batch_id"] = str(item["batch_id"])
        item["created_at"] = item["created_at"].isoformat()
        item["updated_at"] = item["updated_at"].isoformat()
        for key, default in CALL_ITEM_DEFAULTS.items():
            item.setdefault(key, deepcopy(default))
        return item

    def _assignment_row(self, row: asyncpg.Record) -> dict[str, Any]:
        item = dict(row)
        item["id"] = str(item["id"])
        item["robot_id"] = str(item["robot_id"])
        item["created_at"] = item["created_at"].isoformat()
        item["updated_at"] = item["updated_at"].isoformat()
        for key, default in ASSIGNMENT_DEFAULTS.items():
            item.setdefault(key, deepcopy(default))
        return item
