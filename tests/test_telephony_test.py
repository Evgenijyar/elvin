import asyncio
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from elvin.services.telephony_test import (
    TelephonyTestError,
    TelephonyTestService,
    normalize_phone_number,
)


def test_normalize_phone_number_accepts_safe_russian_forms() -> None:
    assert normalize_phone_number("+7 (999) 123-45-67") == "79991234567"
    assert normalize_phone_number("8 999 123 45 67") == "79991234567"
    assert normalize_phone_number("9991234567") == "79991234567"


def test_normalize_phone_number_rejects_ami_injection() -> None:
    try:
        normalize_phone_number("79991234567\r\nAction: Command")
    except TelephonyTestError as exc:
        assert "только цифры" in str(exc)
    else:
        raise AssertionError("AMI header injection must be rejected")


def test_direct_sip_test_tracks_answer_playback_and_hangup(tmp_path: Path) -> None:
    received: list[dict[str, str]] = []

    async def exercise() -> dict[str, object]:
        finished = asyncio.Event()

        async def read_message(reader: asyncio.StreamReader) -> dict[str, str]:
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

        async def send_message(
            writer: asyncio.StreamWriter,
            fields: list[tuple[str, str]],
        ) -> None:
            payload = "\r\n".join(f"{key}: {value}" for key, value in fields)
            writer.write(f"{payload}\r\n\r\n".encode())
            await writer.drain()

        async def handle(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            writer.write(b"Asterisk Call Manager/10.0.0\r\n")
            await writer.drain()
            try:
                login = await read_message(reader)
                received.append(login)
                await send_message(
                    writer,
                    [
                        ("Response", "Success"),
                        ("ActionID", login["ActionID"]),
                        ("Message", "Authentication accepted"),
                    ],
                )
                originate = await read_message(reader)
                received.append(originate)
                action_id = originate["ActionID"]
                channel_id = originate["ChannelId"]
                test_id = originate["Variable"].split("=", 1)[1]
                await send_message(
                    writer,
                    [
                        ("Response", "Success"),
                        ("ActionID", action_id),
                        ("Message", "Originate successfully queued"),
                    ],
                )
                await send_message(
                    writer,
                    [
                        ("Event", "Newchannel"),
                        ("Channel", "PJSIP/lptracker-endpoint-00000001"),
                        ("Uniqueid", channel_id),
                        ("Linkedid", channel_id),
                    ],
                )
                await send_message(
                    writer,
                    [
                        ("Event", "Newstate"),
                        ("Channel", "PJSIP/lptracker-endpoint-00000001"),
                        ("ChannelStateDesc", "Ringing"),
                        ("Uniqueid", channel_id),
                    ],
                )
                await send_message(
                    writer,
                    [
                        ("Event", "OriginateResponse"),
                        ("ActionID", action_id),
                        ("Response", "Success"),
                        ("Reason", "4"),
                        ("Uniqueid", channel_id),
                    ],
                )
                for stage, extra in (
                    ("ANSWERED", []),
                    ("PLAYBACK_STARTED", []),
                    ("PLAYBACK_FINISHED", [("PlaybackStatus", "SUCCESS")]),
                ):
                    await send_message(
                        writer,
                        [
                            ("Event", "UserEvent"),
                            ("UserEvent", "ElvinTelephonyTest"),
                            ("TestId", test_id),
                            ("Stage", stage),
                            ("Uniqueid", channel_id),
                            *extra,
                        ],
                    )
                await send_message(
                    writer,
                    [
                        ("Event", "Hangup"),
                        ("Channel", "PJSIP/lptracker-endpoint-00000001"),
                        ("Uniqueid", channel_id),
                        ("Cause", "16"),
                        ("Cause-txt", "Normal Clearing"),
                    ],
                )
                await read_message(reader)  # Logoff
            finally:
                finished.set()
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        settings = SimpleNamespace(
            asterisk_ami_host="127.0.0.1",
            asterisk_ami_port=port,
            asterisk_ami_username="elvin",
            asterisk_ami_password=SecretStr("secret"),
            data_dir=tmp_path,
        )
        service = TelephonyTestService(settings)
        audio_path = service.audio_dir / f"{'a' * 32}.wav"
        audio_path.write_bytes(b"RIFF-test-audio")
        try:
            result = await service.start_call(
                test_id="a" * 32,
                phone="+7 999 123-45-67",
                original_filename="greeting.mp3",
                audio_path=audio_path,
                duration_seconds=1.25,
            )
            assert result["status"] == "queued"
            await asyncio.wait_for(finished.wait(), timeout=2.0)
            call = service._calls["a" * 32]
            assert call.task is not None
            await asyncio.wait_for(call.task, timeout=2.0)
            return call.snapshot()
        finally:
            await service.close()
            server.close()
            await server.wait_closed()

    result = asyncio.run(exercise())

    assert received[0]["Action"] == "Login"
    assert received[0]["Events"] == "on"
    assert received[1]["Action"] == "Originate"
    assert received[1]["Channel"] == "PJSIP/79991234567@lptracker-endpoint"
    assert received[1]["Context"] == "elvin-telephony-test"
    assert received[1]["EarlyMedia"] == "false"
    assert received[1]["Async"] == "true"
    assert received[1]["Variable"] == f"ELVIN_TEST_ID={'a' * 32}"
    assert result["status"] == "completed"
    assert result["terminal"] is True
    assert result["playback_status"] == "SUCCESS"
    events = [item["event"] for item in result["timeline"]]
    assert "DIALPLAN_ANSWERED" in events
    assert "DIALPLAN_PLAYBACK_STARTED" in events
    assert "DIALPLAN_PLAYBACK_FINISHED" in events
    assert "AMI_HANGUP" in events
    assert "TEST_RESOURCE_CLEANUP" in events
    assert not (tmp_path / "telephony-test-audio" / f"{'a' * 32}.wav").exists()
