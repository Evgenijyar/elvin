import asyncio
from types import SimpleNamespace

from pydantic import SecretStr

from elvin.core.phone import first_phone_from_details
from elvin.services.call_queue import CallQueueManager, MediaCallContext
from elvin.services.call_transport import DIRECT_SIP, LPTRACKER_API
from elvin.services.direct_call import AsteriskDirectCallService


async def _read_ami_message(reader: asyncio.StreamReader) -> dict[str, str]:
    message: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            return message
        text = line.decode().rstrip("\r\n")
        if not text:
            return message
        key, _, value = text.partition(":")
        message[key.strip()] = value.strip()


async def _send_ami_message(
    writer: asyncio.StreamWriter,
    fields: list[tuple[str, str]],
) -> None:
    payload = "\r\n".join(f"{key}: {value}" for key, value in fields)
    writer.write(f"{payload}\r\n\r\n".encode())
    await writer.drain()


def test_first_documented_phone_is_normalized() -> None:
    details = [
        {"type": "email", "data": "person@example.com"},
        {"type": "phone", "data": "8 (999) 123-45-67"},
        {"type": "phone", "data": "+7 999 000-00-00"},
    ]
    assert first_phone_from_details(details) == "79991234567"
    assert (
        first_phone_from_details(
            [{"type": "email/phone", "data": "person@example.com/+7 999 123-45-67"}]
        )
        == "79991234567"
    )


def test_direct_call_originates_into_existing_media_runtime() -> None:
    received: list[dict[str, str]] = []

    async def exercise() -> tuple[object, list[dict[str, str]]]:
        finished = asyncio.Event()

        async def handle_ami(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            writer.write(b"Asterisk Call Manager/11.0.0\r\n")
            await writer.drain()
            try:
                login = await _read_ami_message(reader)
                received.append(login)
                await _send_ami_message(
                    writer,
                    [
                        ("Response", "Success"),
                        ("ActionID", login["ActionID"]),
                        ("Message", "Authentication accepted"),
                    ],
                )
                originate = await _read_ami_message(reader)
                received.append(originate)
                action_id = originate["ActionID"]
                channel_id = originate["ChannelId"]
                call_id = originate["Variable"].split("=", 1)[1]
                await _send_ami_message(
                    writer,
                    [
                        ("Response", "Success"),
                        ("ActionID", action_id),
                        ("Message", "Originate successfully queued"),
                    ],
                )
                await _send_ami_message(
                    writer,
                    [
                        ("Event", "Newchannel"),
                        ("Channel", "PJSIP/lptracker-endpoint-00000001"),
                        ("Uniqueid", channel_id),
                        ("Linkedid", channel_id),
                    ],
                )
                await _send_ami_message(
                    writer,
                    [
                        ("Event", "UserEvent"),
                        ("UserEvent", "ElvinDirectCall"),
                        ("DirectCallId", call_id),
                        ("Stage", "MEDIA_BRIDGE_STARTED"),
                        ("Channel", "PJSIP/lptracker-endpoint-00000001"),
                        ("Uniqueid", channel_id),
                    ],
                )
                await _send_ami_message(
                    writer,
                    [
                        ("Event", "Hangup"),
                        ("Channel", "PJSIP/lptracker-endpoint-00000001"),
                        ("Uniqueid", channel_id),
                        ("Cause", "16"),
                        ("Cause-txt", "Normal Clearing"),
                    ],
                )
                await _read_ami_message(reader)  # Logoff
            finally:
                finished.set()
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle_ami, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        settings = SimpleNamespace(
            asterisk_ami_host="127.0.0.1",
            asterisk_ami_port=port,
            asterisk_ami_username="elvin",
            asterisk_ami_password=SecretStr("secret"),
        )
        service = AsteriskDirectCallService(settings)
        try:
            call = await service.start_call(
                phone="+7 999 123-45-67",
                batch_id="batch-1",
                lead_id=123,
                max_call_seconds=60,
            )
            await asyncio.wait_for(finished.wait(), timeout=2.0)
            assert call.task is not None
            await asyncio.wait_for(call.task, timeout=2.0)
            return call, received
        finally:
            await service.close()
            server.close()
            await server.wait_closed()

    call, messages = asyncio.run(exercise())

    assert messages[0]["Action"] == "Login"
    assert messages[0]["Events"] == "on"
    assert messages[1]["Action"] == "Originate"
    assert messages[1]["Channel"] == "PJSIP/79991234567@lptracker-endpoint"
    assert messages[1]["Context"] == "elvin-direct-outbound"
    assert messages[1]["EarlyMedia"] == "false"
    assert messages[1]["Async"] == "true"
    assert messages[1]["Variable"].startswith("ELVIN_DIRECT_CALL_ID=")
    assert call.media_bridge_started is True
    assert call.status == "ended"
    assert call.cause == "Normal Clearing"


def test_queue_selects_exactly_one_outbound_transport() -> None:
    class Timeline:
        def __init__(self) -> None:
            self.events: list[str] = []

        def add(self, event: str, **_details: object) -> None:
            self.events.append(event)

    class DirectCalls:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def start_call(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return SimpleNamespace(call_id="direct-1")

    class LPTracker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def call_lead(self, token: str, lead_id: int) -> None:
            self.calls.append((token, lead_id))

    manager = object.__new__(CallQueueManager)
    manager.direct_calls = DirectCalls()
    manager.lptracker = LPTracker()
    timeline = Timeline()
    context = MediaCallContext(
        batch_id="batch-1",
        item_id="item-1",
        assignment_id="assignment-1",
        robot_id="robot-1",
        lead_id=123,
        voice_call=SimpleNamespace(timeline=timeline),
    )
    item = {
        "phone_number": "79991234567",
        "phone_masked": "+***4567",
    }

    async def exercise() -> None:
        direct = await manager._request_outbound_call(
            transport=DIRECT_SIP,
            token="token",
            context=context,
            item=item,
            max_call_seconds=300,
        )
        assert direct is not None
        assert manager.direct_calls.calls[0]["phone"] == "79991234567"
        assert manager.lptracker.calls == []

        legacy = await manager._request_outbound_call(
            transport=LPTRACKER_API,
            token="token",
            context=context,
            item=item,
            max_call_seconds=300,
        )
        assert legacy is None
        assert manager.lptracker.calls == [("token", 123)]

    asyncio.run(exercise())
    assert timeline.events == [
        "DIRECT_SIP_CALL_REQUEST",
        "DIRECT_SIP_CALL_ACCEPTED",
        "LPTRACKER_CALL_REQUEST",
        "LPTRACKER_CALL_ACCEPTED",
    ]
