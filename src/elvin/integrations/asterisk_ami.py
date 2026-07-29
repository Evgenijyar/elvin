"""Minimal asynchronous Asterisk Manager Interface client.

Only the authenticated actions needed for real-time channel gain and an
explicit robot-requested channel hangup are implemented. AMI events are
disabled during login so command responses remain strictly serialized and no
background reader is required.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping


class AsteriskAmiError(RuntimeError):
    """AMI connection, authentication or action failure."""


class AsteriskAmiClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout_seconds: float = 1.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._action_sequence = 0

    async def hangup_channel(self, channel: str, cause: int = 16) -> None:
        """Hang up the exact active Asterisk channel through AMI."""
        if not channel:
            raise AsteriskAmiError("Asterisk channel is not available")
        bounded_cause = max(1, min(int(cause), 127))
        await self.action(
            "Hangup",
            {
                "Channel": channel,
                "Cause": str(bounded_cause),
            },
        )

    async def set_channel_rx_gain(self, channel: str, gain: float) -> None:
        """Apply linear gain to media read from the WebSocket channel.

        ``VOLUME(RX)`` uses a negative divisor for attenuation. A value of zero
        disables adjustment, while ``-2`` is one half of the original
        amplitude.
        """
        if not channel:
            raise AsteriskAmiError("Asterisk channel is not available")
        bounded_gain = max(0.001, min(float(gain), 1.0))
        value = "0" if bounded_gain >= 0.999 else f"{-1.0 / bounded_gain:.6f}"
        await self.action(
            "Setvar",
            {
                "Channel": channel,
                "Variable": "VOLUME(RX)",
                "Value": value,
            },
        )

    async def action(
        self,
        name: str,
        fields: Mapping[str, str],
    ) -> dict[str, str]:
        async with self._lock:
            try:
                await self._ensure_connected()
                return await self._action_unlocked(name, fields)
            except BaseException:
                await self._close_unlocked()
                raise

    async def close(self) -> None:
        async with self._lock:
            await self._close_unlocked()

    async def _ensure_connected(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout_seconds,
            )
            greeting = await asyncio.wait_for(
                reader.readline(),
                timeout=self.timeout_seconds,
            )
        except (OSError, TimeoutError) as exc:
            raise AsteriskAmiError(
                f"Unable to connect to Asterisk AMI at {self.host}:{self.port}"
            ) from exc
        if not greeting.startswith(b"Asterisk Call Manager/"):
            writer.close()
            await writer.wait_closed()
            raise AsteriskAmiError("Unexpected Asterisk AMI greeting")
        self._reader = reader
        self._writer = writer
        response = await self._action_unlocked(
            "Login",
            {
                "Username": self.username,
                "Secret": self.password,
                "Events": "off",
            },
        )
        if response.get("Response", "").lower() != "success":
            raise AsteriskAmiError("Asterisk AMI authentication failed")

    async def _action_unlocked(
        self,
        name: str,
        fields: Mapping[str, str],
    ) -> dict[str, str]:
        reader = self._reader
        writer = self._writer
        if reader is None or writer is None:
            raise AsteriskAmiError("Asterisk AMI is not connected")
        self._action_sequence += 1
        action_id = f"elvin-{self._action_sequence}"
        lines = [
            f"Action: {name}",
            f"ActionID: {action_id}",
            *(f"{key}: {value}" for key, value in fields.items()),
            "",
            "",
        ]
        writer.write("\r\n".join(lines).encode("utf-8"))
        try:
            await asyncio.wait_for(
                writer.drain(),
                timeout=self.timeout_seconds,
            )
            while True:
                message = await asyncio.wait_for(
                    self._read_message(reader),
                    timeout=self.timeout_seconds,
                )
                if message.get("ActionID") != action_id:
                    continue
                if message.get("Response", "").lower() != "success":
                    detail = message.get("Message") or "AMI action failed"
                    raise AsteriskAmiError(detail)
                return message
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise AsteriskAmiError(f"Asterisk AMI action {name} failed") from exc

    @staticmethod
    async def _read_message(
        reader: asyncio.StreamReader,
    ) -> dict[str, str]:
        message: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if not line:
                raise asyncio.IncompleteReadError(line, None)
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if not text:
                if message:
                    return message
                continue
            key, separator, value = text.partition(":")
            if separator:
                message[key.strip()] = value.strip()

    async def _close_unlocked(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
