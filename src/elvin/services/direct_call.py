"""Production direct-SIP origination through Asterisk AMI."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from uuid import uuid4

from elvin.config import Settings
from elvin.core.phone import (
    PhoneNumberError,
    mask_phone_number,
    normalize_outbound_phone,
)
from elvin.integrations.asterisk_ami import (
    AsteriskAmiError,
    AsteriskAmiEventConnection,
)

logger = logging.getLogger("elvin.direct_call")


class DirectCallError(RuntimeError):
    """A direct production call could not be originated."""


@dataclass(slots=True)
class DirectCallHandle:
    call_id: str
    batch_id: str
    lead_id: int
    phone: str
    max_call_seconds: float
    status: str = "queued"
    error: str = ""
    channel: str = ""
    cause: str = ""
    media_bridge_started: bool = False
    accepted: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    finished: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    channel_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    started_monotonic: float = field(default_factory=monotonic, repr=False)
    timeline: list[dict[str, Any]] = field(default_factory=list, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    connection: AsteriskAmiEventConnection | None = field(default=None, repr=False)
    hangup_requested: bool = field(default=False, repr=False)

    def add_event(self, event: str, **details: Any) -> None:
        elapsed_ms = round((monotonic() - self.started_monotonic) * 1000, 3)
        self.timeline.append(
            {
                "elapsed_ms": elapsed_ms,
                "event": event,
                "details": details,
            }
        )
        logger.warning(
            "Direct call timeline: call=%s batch=%s lead=%s phone=%s "
            "t=%.3fms event=%s details=%s",
            self.call_id,
            self.batch_id,
            self.lead_id,
            mask_phone_number(self.phone),
            elapsed_ms,
            event,
            details,
        )

    async def wait_finished(self, timeout: float | None = None) -> bool:
        try:
            if timeout is None:
                await self.finished.wait()
            else:
                await asyncio.wait_for(self.finished.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def hangup(self) -> None:
        if self.finished.is_set() or self.hangup_requested:
            return
        if not self.channel:
            try:
                await asyncio.wait_for(self.channel_ready.wait(), timeout=1.0)
            except TimeoutError:
                self.add_event("DIRECT_HANGUP_SKIPPED", reason="channel_not_available")
                return
        connection = self.connection
        if connection is None:
            return
        self.hangup_requested = True
        action_id = f"direct-hangup-{self.call_id}"
        try:
            await connection.send(
                {
                    "Action": "Hangup",
                    "ActionID": action_id,
                    "Channel": self.channel,
                    "Cause": "16",
                }
            )
            self.add_event("DIRECT_HANGUP_SENT", channel=self.channel)
        except AsteriskAmiError as exc:
            self.hangup_requested = False
            self.add_event(
                "DIRECT_HANGUP_FAILED",
                error=f"{type(exc).__name__}: {exc}",
            )


class AsteriskDirectCallService:
    """Own at most one production direct-SIP call at a time."""

    def __init__(self, settings: Settings) -> None:
        self.host = settings.asterisk_ami_host
        self.port = settings.asterisk_ami_port
        self.username = settings.asterisk_ami_username or ""
        configured_password = settings.asterisk_ami_password
        self.password = (
            configured_password.get_secret_value()
            if configured_password is not None
            else ""
        )
        self._active: DirectCallHandle | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def configured(self) -> bool:
        return bool(self.host and self.port and self.username and self.password)

    async def start_call(
        self,
        *,
        phone: str,
        batch_id: str,
        lead_id: int,
        max_call_seconds: float,
    ) -> DirectCallHandle:
        if self._closed:
            raise DirectCallError("Сервис прямых звонков остановлен.")
        if not self.configured:
            raise DirectCallError("Asterisk AMI не настроен для прямых звонков.")
        try:
            normalized_phone = normalize_outbound_phone(phone)
        except PhoneNumberError as exc:
            raise DirectCallError(str(exc)) from exc

        async with self._lock:
            if self._active is not None and not self._active.finished.is_set():
                raise DirectCallError(
                    f"Прямой звонок {self._active.call_id[:8]} ещё не завершён."
                )
            handle = DirectCallHandle(
                call_id=uuid4().hex,
                batch_id=batch_id,
                lead_id=int(lead_id),
                phone=normalized_phone,
                max_call_seconds=max(60.0, float(max_call_seconds)),
            )
            handle.add_event("DIRECT_CALL_CREATED")
            self._active = handle
            handle.task = asyncio.create_task(
                self._run_call(handle),
                name=f"elvin-direct-call-{handle.call_id}",
            )

        accepted_waiter = asyncio.create_task(handle.accepted.wait())
        finished_waiter = asyncio.create_task(handle.finished.wait())
        try:
            done, _pending = await asyncio.wait(
                {accepted_waiter, finished_waiter},
                timeout=8.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if accepted_waiter in done and handle.accepted.is_set():
                return handle
            if finished_waiter in done and handle.finished.is_set():
                raise DirectCallError(
                    handle.error or "Asterisk завершил прямой вызов до его запуска."
                )
            await handle.hangup()
            if handle.task is not None and not handle.task.done():
                handle.task.cancel()
                await asyncio.gather(handle.task, return_exceptions=True)
            raise DirectCallError(
                "Asterisk не подтвердил постановку прямого вызова в очередь."
            )
        finally:
            accepted_waiter.cancel()
            finished_waiter.cancel()
            await asyncio.gather(
                accepted_waiter,
                finished_waiter,
                return_exceptions=True,
            )

    async def close(self) -> None:
        self._closed = True
        handle = self._active
        if handle is None or handle.finished.is_set():
            return
        await handle.hangup()
        if not await handle.wait_finished(timeout=3.0) and handle.task is not None:
            handle.task.cancel()
            await asyncio.gather(handle.task, return_exceptions=True)

    async def _run_call(self, handle: DirectCallHandle) -> None:
        connection = AsteriskAmiEventConnection(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout_seconds=3.0,
        )
        handle.connection = connection
        action_id = f"direct-originate-{handle.call_id}"
        channel_id = f"elvin-direct-{handle.call_id}"
        try:
            handle.status = "connecting"
            handle.add_event("DIRECT_AMI_CONNECT_START", host=self.host, port=self.port)
            protocol = await connection.connect()
            handle.add_event("DIRECT_AMI_AUTHENTICATED", protocol=protocol)

            handle.status = "dialing"
            handle.add_event(
                "DIRECT_ORIGINATE_SENT",
                channel=(
                    f"PJSIP/{mask_phone_number(handle.phone)}@lptracker-endpoint"
                ),
                context="elvin-direct-outbound",
                timeout_ms=60_000,
            )
            await connection.send(
                {
                    "Action": "Originate",
                    "ActionID": action_id,
                    "Channel": f"PJSIP/{handle.phone}@lptracker-endpoint",
                    "Context": "elvin-direct-outbound",
                    "Exten": "s",
                    "Priority": "1",
                    "Timeout": "60000",
                    "Variable": f"ELVIN_DIRECT_CALL_ID={handle.call_id}",
                    "Async": "true",
                    "EarlyMedia": "false",
                    "Codecs": "alaw,ulaw",
                    "ChannelId": channel_id,
                }
            )

            async with asyncio.timeout(handle.max_call_seconds + 120.0):
                while True:
                    message = await connection.read()
                    if not _message_belongs_to_call(
                        message,
                        action_id=action_id,
                        channel_id=channel_id,
                        call_id=handle.call_id,
                    ):
                        continue
                    if self._handle_message(
                        handle,
                        message,
                        action_id=action_id,
                    ):
                        break
        except asyncio.CancelledError:
            handle.status = "cancelled"
            handle.error = "Прямой звонок остановлен при завершении приложения."
            handle.add_event("DIRECT_CALL_CANCELLED")
            raise
        except TimeoutError:
            handle.status = "failed"
            handle.error = "Истекло максимальное время прямого звонка."
            handle.add_event("DIRECT_CALL_TIMEOUT")
            await handle.hangup()
        except Exception as exc:
            handle.status = "failed"
            handle.error = f"{type(exc).__name__}: {exc}"[:1000]
            handle.add_event("DIRECT_CALL_ERROR", error=handle.error)
            logger.exception(
                "Production direct call failed: call=%s batch=%s lead=%s",
                handle.call_id,
                handle.batch_id,
                handle.lead_id,
            )
        finally:
            if not handle.accepted.is_set() and handle.status == "dialing":
                handle.status = "failed"
                handle.error = handle.error or "AMI Originate не был подтверждён."
            await connection.close()
            handle.connection = None
            handle.finished.set()
            handle.add_event(
                "DIRECT_CALL_FINISHED",
                status=handle.status,
                cause=handle.cause,
                media_bridge_started=handle.media_bridge_started,
            )
            async with self._lock:
                if self._active is handle:
                    self._active = None

    @staticmethod
    def _handle_message(
        handle: DirectCallHandle,
        message: dict[str, str],
        *,
        action_id: str,
    ) -> bool:
        event = message.get("Event", "")
        if not event and message.get("ActionID") == action_id:
            response = message.get("Response", "")
            detail = message.get("Message", "")
            handle.add_event(
                "DIRECT_ORIGINATE_ACTION_RESPONSE",
                response=response,
                message=detail,
            )
            if response.lower() != "success":
                handle.status = "failed"
                handle.error = detail or "AMI отклонил прямой Originate."
                return True
            handle.accepted.set()
            return False

        details = _selected_event_details(message)
        if event == "Newchannel":
            handle.channel = message.get("Channel", "")
            if handle.channel:
                handle.channel_ready.set()
            handle.add_event("DIRECT_AMI_NEWCHANNEL", **details)
            return False

        if event == "Newstate":
            handle.add_event("DIRECT_SIP_CHANNEL_STATE", **details)
            if message.get("ChannelStateDesc", "").lower() == "up":
                handle.status = "answered"
            return False

        if event in {"DialBegin", "DialEnd", "HangupRequest"}:
            handle.add_event(f"DIRECT_AMI_{event.upper()}", **details)
            return False

        if event == "OriginateResponse":
            handle.add_event("DIRECT_ORIGINATE_RESPONSE", **details)
            if message.get("Response", "").lower() != "success":
                handle.status = "no_answer"
                handle.cause = message.get("Reason", "unknown")
                handle.error = f"Прямой вызов не состоялся: reason={handle.cause}."
                return True
            if handle.status == "dialing":
                handle.status = "answered"
            return False

        if event == "UserEvent":
            stage = message.get("Stage", "").upper()
            handle.add_event(f"DIRECT_DIALPLAN_{stage or 'EVENT'}", **details)
            if stage == "MEDIA_BRIDGE_STARTED":
                handle.media_bridge_started = True
                handle.status = "media"
            elif stage == "MEDIA_BRIDGE_FINISHED":
                handle.cause = message.get("MediaDialStatus", "")
            return False

        if event == "Hangup":
            handle.add_event("DIRECT_AMI_HANGUP", **details)
            handle.cause = (
                message.get("Cause-txt")
                or message.get("Cause")
                or handle.cause
            )
            if handle.media_bridge_started:
                handle.status = "ended"
            elif handle.hangup_requested:
                handle.status = "cancelled"
            elif handle.status not in {"failed", "no_answer"}:
                handle.status = "no_answer"
                handle.error = (
                    f"Прямой вызов завершён до медиасессии: {handle.cause or 'unknown'}."
                )
            return True
        return False


def _message_belongs_to_call(
    message: dict[str, str],
    *,
    action_id: str,
    channel_id: str,
    call_id: str,
) -> bool:
    if message.get("ActionID") == action_id:
        return True
    if message.get("UserEvent") == "ElvinDirectCall":
        return message.get("DirectCallId") == call_id
    unique_ids = {
        message.get("Uniqueid", ""),
        message.get("DestUniqueid", ""),
        message.get("Linkedid", ""),
    }
    return channel_id in unique_ids


def _selected_event_details(message: dict[str, str]) -> dict[str, str]:
    keys = (
        "Event",
        "Response",
        "Reason",
        "Channel",
        "ChannelStateDesc",
        "DestChannel",
        "DialStatus",
        "Stage",
        "MediaDialStatus",
        "Cause",
        "Cause-txt",
        "Uniqueid",
        "Linkedid",
    )
    return {key: message[key] for key in keys if message.get(key)}
