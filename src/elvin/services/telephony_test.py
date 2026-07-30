"""Isolated direct-SIP telephony test through Asterisk AMI.

This service deliberately does not use the production call queue, LPTracker's
``/lead/{id}/call`` callback flow, Gemini, or chan_websocket.  It originates one
outbound PJSIP channel and follows its AMI events until Asterisk finishes
playing a prepared test file.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from elvin.config import Settings
from elvin.core.phone import (
    PhoneNumberError,
    mask_phone_number,
    normalize_outbound_phone,
)

logger = logging.getLogger("elvin.telephony_test")

_TERMINAL_STATUSES = {"completed", "ended", "failed", "cancelled"}
_MAX_RETAINED_CALLS = 50


class TelephonyTestError(RuntimeError):
    """The isolated telephony test could not be started."""


def normalize_phone_number(value: str) -> str:
    """Return a dial-safe international number containing digits only."""
    try:
        return normalize_outbound_phone(value)
    except PhoneNumberError as exc:
        raise TelephonyTestError(str(exc)) from exc


@dataclass
class TelephonyTestCall:
    test_id: str
    phone: str
    original_filename: str
    audio_path: Path
    duration_seconds: float
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: str = "queued"
    error: str = ""
    playback_status: str = ""
    timeline: list[dict[str, Any]] = field(default_factory=list)
    started_monotonic: float = field(default_factory=monotonic, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def add_event(self, event: str, **details: Any) -> None:
        elapsed_ms = round((monotonic() - self.started_monotonic) * 1000, 3)
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "elapsed_ms": elapsed_ms,
            "event": event,
            "details": details,
        }
        self.timeline.append(entry)
        logger.warning(
            "Telephony test timeline: test=%s phone=%s t=%.3fms event=%s details=%s",
            self.test_id,
            mask_phone_number(self.phone),
            elapsed_ms,
            event,
            details,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "phone": mask_phone_number(self.phone),
            "audio_filename": self.original_filename,
            "duration_seconds": round(self.duration_seconds, 3),
            "created_at": self.created_at,
            "status": self.status,
            "terminal": self.status in _TERMINAL_STATUSES,
            "error": self.error,
            "playback_status": self.playback_status,
            "timeline": list(self.timeline),
        }


class TelephonyTestService:
    """Own one isolated outbound playback test at a time."""

    def __init__(self, settings: Settings) -> None:
        self.host = settings.asterisk_ami_host
        self.port = settings.asterisk_ami_port
        self.username = settings.asterisk_ami_username or ""
        password = settings.asterisk_ami_password
        self.password = password.get_secret_value() if password is not None else ""
        self.audio_dir = settings.data_dir / "telephony-test-audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self._calls: OrderedDict[str, TelephonyTestCall] = OrderedDict()
        self._lock = asyncio.Lock()
        self._closed = False
        self._remove_stale_audio()

    @property
    def configured(self) -> bool:
        return bool(self.host and self.port and self.username and self.password)

    async def start_call(
        self,
        *,
        test_id: str,
        phone: str,
        original_filename: str,
        audio_path: Path,
        duration_seconds: float,
    ) -> dict[str, Any]:
        if self._closed:
            raise TelephonyTestError("Сервис тестовой телефонии остановлен.")
        if not self.configured:
            raise TelephonyTestError("Asterisk AMI не настроен.")
        normalized_phone = normalize_phone_number(phone)
        if not re.fullmatch(r"[0-9a-f]{32}", test_id):
            raise TelephonyTestError("Некорректный идентификатор теста.")
        if audio_path.parent.resolve() != self.audio_dir.resolve():
            raise TelephonyTestError("Аудиофайл находится вне тестового каталога.")
        if not audio_path.is_file():
            raise TelephonyTestError("Подготовленный аудиофайл не найден.")

        async with self._lock:
            active = next(
                (
                    call
                    for call in self._calls.values()
                    if call.task is not None
                    and not call.task.done()
                ),
                None,
            )
            if active is not None:
                raise TelephonyTestError(
                    f"Уже выполняется тест {active.test_id[:8]}. Дождитесь его завершения."
                )
            call = TelephonyTestCall(
                test_id=test_id,
                phone=normalized_phone,
                original_filename=original_filename,
                audio_path=audio_path,
                duration_seconds=duration_seconds,
            )
            call.add_event(
                "TEST_CREATED",
                audio_filename=original_filename,
                duration_seconds=round(duration_seconds, 3),
            )
            self._calls[test_id] = call
            self._trim_history()
            call.task = asyncio.create_task(
                self._run_call(call),
                name=f"telephony-test-{test_id}",
            )
            return call.snapshot()

    def get_call(self, test_id: str) -> dict[str, Any] | None:
        call = self._calls.get(test_id)
        return call.snapshot() if call is not None else None

    async def close(self) -> None:
        self._closed = True
        tasks = [
            call.task
            for call in self._calls.values()
            if call.task is not None and not call.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_call(self, call: TelephonyTestCall) -> None:
        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        try:
            call.status = "connecting"
            call.add_event("AMI_CONNECT_START", host=self.host, port=self.port)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=3.0,
            )
            greeting = await asyncio.wait_for(reader.readline(), timeout=3.0)
            if not greeting.startswith(b"Asterisk Call Manager/"):
                raise TelephonyTestError("Asterisk вернул некорректное AMI-приветствие.")
            call.add_event(
                "AMI_CONNECTED",
                protocol=greeting.decode("utf-8", "replace").strip(),
            )

            login_action_id = f"telephony-test-login-{call.test_id}"
            await _send_ami_message(
                writer,
                [
                    ("Action", "Login"),
                    ("ActionID", login_action_id),
                    ("Username", self.username),
                    ("Secret", self.password),
                    ("Events", "on"),
                ],
            )
            login_response = await _wait_for_action_response(
                reader,
                login_action_id,
                timeout=3.0,
            )
            if login_response.get("Response", "").lower() != "success":
                raise TelephonyTestError(
                    login_response.get("Message") or "AMI-аутентификация отклонена."
                )
            call.add_event("AMI_AUTHENTICATED")

            action_id = f"telephony-test-originate-{call.test_id}"
            channel_id = f"elvin-test-{call.test_id}"
            call.status = "dialing"
            call.add_event(
                "ORIGINATE_SENT",
                channel=f"PJSIP/{mask_phone_number(call.phone)}@lptracker-endpoint",
                context="elvin-telephony-test",
                timeout_ms=60_000,
            )
            await _send_ami_message(
                writer,
                [
                    ("Action", "Originate"),
                    ("ActionID", action_id),
                    ("Channel", f"PJSIP/{call.phone}@lptracker-endpoint"),
                    ("Context", "elvin-telephony-test"),
                    ("Exten", "s"),
                    ("Priority", "1"),
                    ("Timeout", "60000"),
                    ("Variable", f"ELVIN_TEST_ID={call.test_id}"),
                    ("Async", "true"),
                    ("EarlyMedia", "false"),
                    ("Codecs", "alaw,ulaw"),
                    ("ChannelId", channel_id),
                ],
            )

            timeout_seconds = max(120.0, 90.0 + call.duration_seconds)
            async with asyncio.timeout(timeout_seconds):
                while True:
                    message = await _read_ami_message(reader)
                    if not _message_belongs_to_call(
                        message,
                        action_id=action_id,
                        channel_id=channel_id,
                        test_id=call.test_id,
                    ):
                        continue
                    if await self._handle_ami_message(
                        call,
                        message,
                        action_id=action_id,
                    ):
                        break
        except asyncio.CancelledError:
            call.status = "cancelled"
            call.error = "Тест остановлен при завершении приложения."
            call.add_event("TEST_CANCELLED")
            raise
        except TimeoutError:
            call.status = "failed"
            call.error = "Истекло время ожидания завершения тестового звонка."
            call.add_event("TEST_TIMEOUT", error=call.error)
        except Exception as exc:
            call.status = "failed"
            call.error = str(exc)[:500]
            call.add_event(
                "TEST_ERROR",
                error=f"{type(exc).__name__}: {call.error}",
            )
            logger.exception("Isolated telephony test failed: test=%s", call.test_id)
        finally:
            if writer is not None:
                try:
                    await _send_ami_message(writer, [("Action", "Logoff")])
                except (OSError, ConnectionError):
                    pass
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            call.audio_path.unlink(missing_ok=True)
            call.add_event(
                "TEST_RESOURCE_CLEANUP",
                audio_deleted=not call.audio_path.exists(),
                final_status=call.status,
            )

    async def _handle_ami_message(
        self,
        call: TelephonyTestCall,
        message: dict[str, str],
        *,
        action_id: str,
    ) -> bool:
        event = message.get("Event", "")
        if not event and message.get("ActionID") == action_id:
            response = message.get("Response", "")
            detail = message.get("Message", "")
            call.add_event(
                "ORIGINATE_ACTION_RESPONSE",
                response=response,
                message=detail,
            )
            if response.lower() != "success":
                call.status = "failed"
                call.error = detail or "AMI отклонил Originate."
                return True
            return False

        selected = _selected_event_details(message)
        if event == "OriginateResponse":
            response = message.get("Response", "")
            call.add_event("ORIGINATE_RESPONSE", **selected)
            if response.lower() != "success":
                call.status = "failed"
                call.error = (
                    f"Исходящий вызов не состоялся: "
                    f"{message.get('Reason', 'unknown')}."
                )
                return True
            if call.status in {"dialing", "ringing"}:
                call.status = "answered"
            return False

        if event == "Newstate":
            state = message.get("ChannelStateDesc", "")
            call.add_event("SIP_CHANNEL_STATE", **selected)
            if state.lower() == "ringing":
                call.status = "ringing"
            elif state.lower() == "up" and call.status in {"dialing", "ringing"}:
                call.status = "answered"
            return False

        if event in {"DialBegin", "DialEnd", "Newchannel", "HangupRequest"}:
            call.add_event(f"AMI_{event.upper()}", **selected)
            return False

        if event == "UserEvent":
            stage = message.get("Stage", "").upper()
            call.add_event(f"DIALPLAN_{stage or 'EVENT'}", **selected)
            if stage == "ANSWERED":
                call.status = "answered"
            elif stage == "PLAYBACK_STARTED":
                call.status = "playing"
            elif stage == "PLAYBACK_FINISHED":
                call.playback_status = message.get("PlaybackStatus", "")
                if call.playback_status.upper() == "SUCCESS":
                    call.status = "completed"
                else:
                    call.status = "failed"
                    call.error = (
                        "Asterisk не смог полностью проиграть тестовый аудиофайл: "
                        f"{call.playback_status or 'unknown'}."
                    )
                # Dialplan immediately executes Hangup(). Keep listening for
                # that final AMI event so the console timeline proves the
                # complete SIP lifecycle and file cleanup cannot race playback.
                return False
            return False

        if event == "Hangup":
            call.add_event("AMI_HANGUP", **selected)
            if call.status not in _TERMINAL_STATUSES:
                if call.status in {"answered", "playing"}:
                    call.status = "ended"
                    call.error = "Звонок завершился до окончания аудиофайла."
                else:
                    call.status = "failed"
                    call.error = (
                        f"Звонок завершён до ответа: "
                        f"{message.get('Cause-txt') or message.get('Cause') or 'unknown'}."
                    )
            return True
        return False

    def _trim_history(self) -> None:
        while len(self._calls) > _MAX_RETAINED_CALLS:
            test_id, call = next(iter(self._calls.items()))
            if call.status not in _TERMINAL_STATUSES:
                break
            self._calls.pop(test_id, None)

    def _remove_stale_audio(self) -> None:
        cutoff = datetime.now(UTC).timestamp() - 24 * 60 * 60
        for path in self.audio_dir.glob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Unable to remove stale telephony test audio: %s", path)


async def _send_ami_message(
    writer: asyncio.StreamWriter,
    fields: list[tuple[str, str]],
) -> None:
    lines: list[str] = []
    for key, value in fields:
        text = str(value)
        if "\r" in text or "\n" in text:
            raise TelephonyTestError(f"Некорректное AMI-поле {key}.")
        lines.append(f"{key}: {text}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
    await asyncio.wait_for(writer.drain(), timeout=3.0)


async def _read_ami_message(reader: asyncio.StreamReader) -> dict[str, str]:
    message: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            raise ConnectionError("Asterisk закрыл AMI-соединение.")
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if not text:
            if message:
                return message
            continue
        key, separator, value = text.partition(":")
        if separator:
            message[key.strip()] = value.strip()


async def _wait_for_action_response(
    reader: asyncio.StreamReader,
    action_id: str,
    *,
    timeout: float,
) -> dict[str, str]:
    async with asyncio.timeout(timeout):
        while True:
            message = await _read_ami_message(reader)
            if message.get("ActionID") == action_id and "Response" in message:
                return message


def _message_belongs_to_call(
    message: dict[str, str],
    *,
    action_id: str,
    channel_id: str,
    test_id: str,
) -> bool:
    if message.get("ActionID") == action_id:
        return True
    if message.get("UserEvent") == "ElvinTelephonyTest":
        return message.get("TestId") == test_id
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
        "PlaybackStatus",
        "Cause",
        "Cause-txt",
        "Uniqueid",
        "Linkedid",
    )
    return {key: message[key] for key in keys if message.get(key)}
