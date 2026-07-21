"""Persistent LPTracker call queue with AI preparation before dialing."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from elvin.config import Settings
from elvin.infrastructure.state_store import StateStore
from elvin.integrations.lptracker import LPTrackerClient
from elvin.media.runtime import (
    PreparedVoiceCall,
    VoiceCallIdentity,
    VoiceRuntime,
)
from elvin.services.conversation_effects import any_effect_enabled
from elvin.services.call_outcomes import (
    NO_ANSWER_KEY,
    destination_for_outcome,
    outcome_counts_as_lead,
)

logger = logging.getLogger("elvin.calls")


class CallQueueError(RuntimeError):
    pass


@dataclass(slots=True)
class MediaCallContext:
    batch_id: str
    item_id: str
    assignment_id: str
    robot_id: str
    lead_id: int
    voice_call: PreparedVoiceCall


MediaTerminator = Callable[[], Awaitable[None]]


class CallQueueManager:
    """Build queues and run one LPTracker call at a time.

    Critical ordering for every item:
    1. Build local VAD/Smart Turn state.
    2. Establish the actual Gemini Live session and wait for setup complete.
    3. Publish the prepared media context.
    4. Only then call LPTracker `/lead/{lead_id}/call`.
    """

    def __init__(
        self,
        store: StateStore,
        lptracker: LPTrackerClient,
        voice_runtime: VoiceRuntime,
        settings: Settings,
        *,
        calls_enabled: bool,
        media_ready: bool,
        media_connect_timeout_seconds: float = 900.0,
    ) -> None:
        self.store = store
        self.lptracker = lptracker
        self.voice_runtime = voice_runtime
        self.settings = settings
        self.calls_enabled = calls_enabled
        self.media_ready = media_ready
        self.media_connect_timeout_seconds = max(
            60.0, min(float(media_connect_timeout_seconds), 3600.0)
        )
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._media_started_events: dict[str, asyncio.Event] = {}
        self._completion_events: dict[str, asyncio.Event] = {}
        self._completion_results: dict[str, str] = {}
        self._pending_media: MediaCallContext | None = None
        self._active_media: MediaCallContext | None = None
        self._media_terminators: dict[str, MediaTerminator] = {}
        self._lock = asyncio.Lock()
        self._media_condition = asyncio.Condition()

    async def close(self) -> None:
        for event in self._stop_events.values():
            event.set()
        for batch_id in list(self._media_terminators):
            await self._terminate_media(batch_id)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._clear_all_media()

    async def prepare(self, assignment: dict[str, Any], token: str) -> dict[str, Any]:
        stage_id = assignment.get("source_stage_id")
        if not stage_id:
            raise CallQueueError("Сначала выберите стадию-источник лидов.")
        call_limit = max(1, min(int(assignment.get("call_limit") or 50), 1000))
        leads = await self.lptracker.leads_for_stage(
            token,
            int(assignment["project_id"]),
            int(stage_id),
            max_results=call_limit,
            max_scan=max(call_limit * 10, 500),
        )
        if not leads:
            raise CallQueueError(
                "В выбранной стадии не найдено лидов с телефоном для обзвона."
            )
        batch = await self.store.create_call_batch(
            assignment_id=assignment["id"],
            project_id=int(assignment["project_id"]),
            robot_id=assignment["robot_id"],
            source_stage_id=int(stage_id),
            items=leads,
        )
        await self.store.update_assignment(assignment["id"], {"status": "QUEUE_READY"})
        return batch

    async def start(self, assignment: dict[str, Any], token: str) -> dict[str, Any]:
        if not self.calls_enabled:
            raise CallQueueError("Реальные вызовы выключены настройкой Elvin.")
        if not self.media_ready:
            raise CallQueueError("Новый голосовой медиаконтур ещё не готов.")
        if not await self._gemini_key():
            raise CallQueueError("Gemini API key «Актёр» не настроен.")
        robot = await self.store.get_robot(assignment["robot_id"])
        if robot is None:
            raise CallQueueError("Профиль робота не найден.")
        if any_effect_enabled(robot.get("effects_config")) and not await self._gemini_director_key():
            raise CallQueueError(
                "Gemini API key «Режиссёр» не настроен, но у робота включены эффекты."
            )

        batch = await self.store.get_latest_call_batch(assignment["id"])
        if batch is None or batch["status"] in {"COMPLETED", "FAILED", "STOPPED"}:
            batch = await self.prepare(assignment, token)
        batch_id = batch["id"]

        async with self._lock:
            existing = self._tasks.get(batch_id)
            if existing and not existing.done():
                return await self.store.get_call_batch(batch_id) or batch
            if any(
                not task.done()
                for other_id, task in self._tasks.items()
                if other_id != batch_id
            ):
                raise CallQueueError(
                    "Одновременно может выполняться только одна очередь."
                )
            stop_event = asyncio.Event()
            self._stop_events[batch_id] = stop_event
            self._tasks[batch_id] = asyncio.create_task(
                self._run_batch(batch_id, token, stop_event),
                name=f"elvin-call-batch-{batch_id}",
            )
        logger.warning(
            "Call batch scheduled: batch=%s assignment=%s",
            batch_id,
            assignment["id"],
        )
        return await self.store.get_call_batch(batch_id) or batch

    async def stop(self, assignment_id: str) -> dict[str, Any] | None:
        batch = await self.store.get_latest_call_batch(assignment_id)
        if batch is None:
            return None
        batch_id = batch["id"]
        if event := self._stop_events.get(batch_id):
            event.set()
        await self._terminate_media(batch_id)
        await self.store.update_call_batch(batch_id, status="STOPPING")
        return await self.store.get_call_batch(batch_id)

    async def claim_media_session(self, *, timeout: float = 8.0) -> MediaCallContext:
        async def wait_and_claim() -> MediaCallContext:
            async with self._media_condition:
                await self._media_condition.wait_for(
                    lambda: self._pending_media is not None
                )
                context = self._pending_media
                assert context is not None
                self._pending_media = None
                self._active_media = context
                context.voice_call.timeline.add("ASTERISK_CALL_CLAIMED")
                return context

        try:
            return await asyncio.wait_for(wait_and_claim(), timeout=timeout)
        except TimeoutError as exc:
            raise CallQueueError(
                "Asterisk подключился без ожидающего вызова LPTracker."
            ) from exc

    async def register_media_terminator(
        self, context: MediaCallContext, terminator: MediaTerminator
    ) -> None:
        async with self._media_condition:
            if self._active_media is context:
                self._media_terminators[context.batch_id] = terminator

    async def unregister_media_terminator(self, context: MediaCallContext) -> None:
        self._media_terminators.pop(context.batch_id, None)

    async def media_started(self, batch_id: str, lead_id: int) -> None:
        batch = await self.store.get_call_batch(batch_id)
        if batch is None or int(batch.get("current_lead_id") or 0) != int(lead_id):
            return
        await self.store.update_call_batch(batch_id, status="IN_CALL")
        if event := self._media_started_events.get(batch_id):
            event.set()

    async def media_finished(self, batch_id: str, lead_id: int, result: str) -> None:
        batch = await self.store.get_call_batch(batch_id)
        if batch is None or int(batch.get("current_lead_id") or 0) != int(lead_id):
            return
        self._completion_results[batch_id] = result[:1000]
        if event := self._completion_events.get(batch_id):
            event.set()

    async def media_status(self) -> dict[str, object]:
        async with self._media_condition:
            return {
                "pending": self._context_dict(self._pending_media),
                "active": self._context_dict(self._active_media),
                "running_batches": sum(
                    1 for task in self._tasks.values() if not task.done()
                ),
            }

    async def _run_batch(
        self, batch_id: str, token: str, stop_event: asyncio.Event
    ) -> None:
        try:
            await self.store.update_call_batch(batch_id, status="RUNNING")
            batch = await self.store.get_call_batch(batch_id)
            if batch is None:
                return
            assignment = await self.store.get_assignment(batch["assignment_id"])
            if assignment is None:
                raise CallQueueError("Назначение проекта не найдено.")
            max_call_seconds = max(
                60,
                min(int(assignment.get("max_call_minutes") or 5), 120) * 60,
            )
            call_limit = max(1, min(int(assignment.get("call_limit") or 50), 1000))
            lead_limit = max(0, min(int(assignment.get("lead_limit") or 0), 1000))
            background_path = None
            if assignment.get("background_audio_filename"):
                candidate = (
                    self.settings.data_dir
                    / "background-audio"
                    / f"{assignment['id']}.pcm"
                )
                if candidate.is_file():
                    background_path = candidate
            background_volume = int(assignment.get("background_audio_volume") or 0)
            await self.store.update_assignment(
                batch["assignment_id"], {"status": "RUNNING"}
            )

            while not stop_event.is_set():
                current_batch = await self.store.get_call_batch(batch_id)
                if current_batch is None:
                    break
                stop_reason = self._limit_stop_reason(
                    current_batch,
                    call_limit=call_limit,
                    lead_limit=lead_limit,
                )
                if stop_reason:
                    await self.store.update_call_batch(
                        batch_id, stop_reason=stop_reason
                    )
                    break

                item = await self.store.next_pending_call_item(batch_id)
                if item is None:
                    break
                context: MediaCallContext | None = None
                media_started = asyncio.Event()
                completed = asyncio.Event()
                self._media_started_events[batch_id] = media_started
                self._completion_events[batch_id] = completed
                try:
                    await self.store.update_call_item(item["id"], status="AI_PREPARING")
                    await self.store.update_call_batch(
                        batch_id,
                        status="AI_PREPARING",
                        current_lead_id=int(item["lead_id"]),
                        current_position=int(item["position"]),
                    )
                    robot = await self.store.get_robot(batch["robot_id"])
                    if robot is None:
                        raise CallQueueError("Профиль робота не найден.")
                    actor_api_key = await self._gemini_key()
                    if not actor_api_key:
                        raise CallQueueError("Gemini API key «Актёр» не настроен.")
                    director_api_key = await self._gemini_director_key()
                    if any_effect_enabled(robot.get("effects_config")) and not director_api_key:
                        raise CallQueueError(
                            "Gemini API key «Режиссёр» не настроен, но у робота включены эффекты."
                        )

                    identity = VoiceCallIdentity(
                        batch_id=batch_id,
                        item_id=item["id"],
                        assignment_id=batch["assignment_id"],
                        robot_id=batch["robot_id"],
                        lead_id=int(item["lead_id"]),
                    )
                    voice_call = await self.voice_runtime.prepare_call(
                        identity=identity,
                        robot=robot,
                        actor_api_key=actor_api_key,
                        director_api_key=director_api_key,
                        background_audio_path=background_path,
                        background_audio_volume=background_volume,
                    )
                    context = MediaCallContext(
                        batch_id=batch_id,
                        item_id=item["id"],
                        assignment_id=batch["assignment_id"],
                        robot_id=batch["robot_id"],
                        lead_id=int(item["lead_id"]),
                        voice_call=voice_call,
                    )
                    await self._publish_pending_media(context)
                    await self.store.update_call_item(
                        item["id"], status="CALL_REQUESTING"
                    )
                    await self.store.update_call_batch(
                        batch_id, status="CALL_REQUESTING"
                    )
                    voice_call.timeline.add("LPTRACKER_CALL_REQUEST")
                    await self.lptracker.call_lead(token, context.lead_id)
                    voice_call.timeline.add("LPTRACKER_CALL_ACCEPTED")
                    await self.store.increment_call_batch(batch_id, calls_made=1)
                    await self.store.update_call_item(
                        item["id"], status="WAITING_FOR_MEDIA"
                    )
                    await self.store.update_call_batch(
                        batch_id, status="WAITING_FOR_MEDIA"
                    )

                    wait_result = await self._wait_event_or_stop(
                        media_started,
                        stop_event,
                        self.media_connect_timeout_seconds,
                    )
                    if wait_result == "stopped":
                        await self.store.update_call_item(
                            item["id"],
                            status="STOPPED",
                            result="stopped_by_user",
                        )
                        break
                    if wait_result == "timeout":
                        await self._record_outcome(
                            token=token,
                            assignment=assignment,
                            item=item,
                            outcome=NO_ANSWER_KEY,
                            voice_call=voice_call,
                        )
                        await self.store.update_call_item(
                            item["id"],
                            status="COMPLETED",
                            result="no_answer",
                        )
                        await self.store.increment_call_batch(batch_id, completed=1)
                        continue

                    await self.store.update_call_item(item["id"], status="IN_CALL")
                    call_result = await self._wait_event_or_stop(
                        completed, stop_event, float(max_call_seconds)
                    )
                    if call_result == "event":
                        result = self._completion_results.pop(batch_id, "completed")
                        failed = result.startswith("media_error:")
                        outcome = voice_call.gemini.classified_outcome
                        move_error = ""
                        if outcome:
                            move_error = await self._record_outcome(
                                token=token,
                                assignment=assignment,
                                item=item,
                                outcome=outcome,
                                voice_call=voice_call,
                            )
                        await self.store.update_call_item(
                            item["id"],
                            status="FAILED" if failed else "COMPLETED",
                            result=result,
                            error_message=result if failed else move_error,
                        )
                        await self.store.increment_call_batch(
                            batch_id,
                            **({"failed": 1} if failed else {"completed": 1}),
                        )
                    elif call_result == "stopped":
                        await self._terminate_media(batch_id)
                        await self.store.update_call_item(
                            item["id"],
                            status="STOPPED",
                            result="stopped_by_user",
                        )
                        break
                    else:
                        await self._terminate_media(batch_id)
                        outcome = voice_call.gemini.classified_outcome
                        if outcome:
                            await self._record_outcome(
                                token=token,
                                assignment=assignment,
                                item=item,
                                outcome=outcome,
                                voice_call=voice_call,
                            )
                        await self.store.update_call_item(
                            item["id"],
                            status="CALL_TIMEOUT",
                            error_message=(
                                "Превышена максимальная длительность звонка."
                            ),
                        )
                        await self.store.increment_call_batch(batch_id, failed=1)
                except Exception as exc:
                    logger.exception(
                        "Call item failed: batch=%s lead=%s",
                        batch_id,
                        item.get("lead_id"),
                    )
                    await self.store.update_call_item(
                        item["id"],
                        status="FAILED",
                        error_message=f"{type(exc).__name__}: {exc}"[:1000],
                    )
                    await self.store.increment_call_batch(batch_id, failed=1)
                    if stop_event.is_set():
                        break
                finally:
                    await self._clear_media_for_batch(batch_id)
                    self._clear_item_events(batch_id)
                if not stop_event.is_set():
                    await asyncio.sleep(1.2)

            final = await self.store.get_call_batch(batch_id)
            final_status = "STOPPED" if stop_event.is_set() else "COMPLETED"
            if (
                final
                and not final.get("stop_reason")
                and int(final.get("completed") or 0) == 0
                and int(final.get("failed") or 0) >= int(final.get("total") or 0) > 0
            ):
                final_status = "FAILED"
            await self.store.update_call_batch(
                batch_id, status=final_status, current_lead_id=None
            )
            if final:
                await self.store.update_assignment(
                    final["assignment_id"], {"status": final_status}
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Call batch crashed: batch=%s", batch_id)
            await self.store.update_call_batch(
                batch_id,
                status="FAILED",
                error_message=f"{type(exc).__name__}: {exc}"[:1000],
            )
        finally:
            await self._clear_media_for_batch(batch_id)
            async with self._lock:
                self._tasks.pop(batch_id, None)
                self._stop_events.pop(batch_id, None)
                self._clear_item_events(batch_id)

    @staticmethod
    def _limit_stop_reason(
        batch: dict[str, Any], *, call_limit: int, lead_limit: int
    ) -> str:
        if int(batch.get("calls_made") or 0) >= call_limit:
            return "call_limit"
        if lead_limit > 0 and int(batch.get("leads_count") or 0) >= lead_limit:
            return "lead_limit"
        return ""

    async def _record_outcome(
        self,
        *,
        token: str,
        assignment: dict[str, Any],
        item: dict[str, Any],
        outcome: str,
        voice_call: PreparedVoiceCall,
    ) -> str:
        stage_id, stage_name = destination_for_outcome(assignment, outcome)
        move_error = ""
        if stage_id is not None:
            try:
                await self.lptracker.move_lead_to_stage(
                    token, int(item["lead_id"]), stage_id
                )
                voice_call.timeline.add(
                    "LPTRACKER_OUTCOME_STAGE_UPDATED",
                    outcome=outcome,
                    stage_id=stage_id,
                    stage_name=stage_name,
                )
            except Exception as exc:
                move_error = (
                    f"Не удалось переместить лид на стадию: {type(exc).__name__}: {exc}"
                )[:1000]
                logger.exception(
                    "Outcome stage update failed: lead=%s outcome=%s stage=%s",
                    item.get("lead_id"),
                    outcome,
                    stage_id,
                )
                voice_call.timeline.add(
                    "LPTRACKER_OUTCOME_STAGE_FAILED",
                    outcome=outcome,
                    stage_id=stage_id,
                    error=move_error,
                )
        await self.store.update_call_item(
            item["id"],
            outcome=outcome,
            destination_stage_id=stage_id,
            destination_stage_name=stage_name,
            error_message=move_error,
        )
        if outcome_counts_as_lead(assignment, outcome):
            await self.store.increment_call_batch(item["batch_id"], leads_count=1)
        return move_error

    async def _publish_pending_media(self, context: MediaCallContext) -> None:
        async with self._media_condition:
            if self._pending_media is not None or self._active_media is not None:
                raise CallQueueError("Предыдущая медиасессия ещё не завершена.")
            self._pending_media = context
            context.voice_call.timeline.add("WAITING_FOR_ASTERISK")
            self._media_condition.notify_all()

    async def _clear_media_for_batch(self, batch_id: str) -> None:
        call: PreparedVoiceCall | None = None
        async with self._media_condition:
            if self._pending_media and self._pending_media.batch_id == batch_id:
                call = self._pending_media.voice_call
                self._pending_media = None
            if self._active_media and self._active_media.batch_id == batch_id:
                call = self._active_media.voice_call
                self._active_media = None
            self._media_terminators.pop(batch_id, None)
            self._media_condition.notify_all()
        if call is not None:
            await call.close()

    async def _clear_all_media(self) -> None:
        contexts = []
        async with self._media_condition:
            if self._pending_media:
                contexts.append(self._pending_media)
            if self._active_media:
                contexts.append(self._active_media)
            self._pending_media = None
            self._active_media = None
        for context in contexts:
            await context.voice_call.close()

    async def _terminate_media(self, batch_id: str) -> None:
        terminator = self._media_terminators.get(batch_id)
        if terminator:
            try:
                await terminator()
            except Exception:
                logger.exception("Unable to terminate media: batch=%s", batch_id)

    async def _wait_event_or_stop(
        self,
        event: asyncio.Event,
        stop_event: asyncio.Event,
        timeout: float,
    ) -> str:
        event_task = asyncio.create_task(event.wait())
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {event_task, stop_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done and stop_event.is_set():
                return "stopped"
            if event_task in done and event.is_set():
                return "event"
            return "timeout"
        finally:
            event_task.cancel()
            stop_task.cancel()
            await asyncio.gather(event_task, stop_task, return_exceptions=True)

    async def _gemini_key(self) -> str:
        stored = (await self.store.get_setting("gemini_api_key") or "").strip()
        if stored:
            return stored
        configured = self.settings.gemini_api_key
        return configured.get_secret_value().strip() if configured else ""

    async def _gemini_director_key(self) -> str:
        stored = (
            await self.store.get_setting("gemini_director_api_key") or ""
        ).strip()
        if stored:
            return stored
        configured = self.settings.gemini_director_api_key
        return configured.get_secret_value().strip() if configured else ""

    def _clear_item_events(self, batch_id: str) -> None:
        self._media_started_events.pop(batch_id, None)
        self._completion_events.pop(batch_id, None)
        self._completion_results.pop(batch_id, None)

    @staticmethod
    def _context_dict(context: MediaCallContext | None) -> dict[str, object] | None:
        if context is None:
            return None
        return {
            "batch_id": context.batch_id,
            "lead_id": context.lead_id,
            "assignment_id": context.assignment_id,
            "robot_id": context.robot_id,
            "gemini_ready": context.voice_call.gemini.session is not None,
            "director_ready": (
                context.voice_call.director is None
                or context.voice_call.director.session is not None
            ),
        }
