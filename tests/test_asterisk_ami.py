import asyncio

from elvin.integrations.asterisk_ami import AsteriskAmiClient


def test_ami_sets_websocket_rx_gain_and_resets_it() -> None:
    received: list[dict[str, str]] = []

    async def exercise() -> None:
        completed = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            writer.write(b"Asterisk Call Manager/10.0.0\r\n")
            await writer.drain()
            try:
                while len(received) < 4:
                    message: dict[str, str] = {}
                    while True:
                        line = await reader.readline()
                        if not line:
                            return
                        text = line.decode().rstrip("\r\n")
                        if not text:
                            break
                        key, _, value = text.partition(":")
                        message[key.strip()] = value.strip()
                    received.append(message)
                    response = (
                        "Response: Success\r\n"
                        f"ActionID: {message['ActionID']}\r\n"
                        "Message: Authentication accepted\r\n\r\n"
                    )
                    writer.write(response.encode())
                    await writer.drain()
            finally:
                completed.set()
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = AsteriskAmiClient(
            host="127.0.0.1",
            port=port,
            username="elvin",
            password="secret",
        )
        try:
            await client.set_channel_rx_gain("WebSocket/elvin/1", 0.5)
            await client.set_channel_rx_gain("WebSocket/elvin/1", 1.0)
            await client.hangup_channel("WebSocket/elvin/1")
            await asyncio.wait_for(completed.wait(), timeout=1.0)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(exercise())

    assert received[0]["Action"] == "Login"
    assert received[0]["Events"] == "off"
    assert received[1] == {
        "Action": "Setvar",
        "ActionID": "elvin-2",
        "Channel": "WebSocket/elvin/1",
        "Variable": "VOLUME(RX)",
        "Value": "-2.000000",
    }
    assert received[2]["Value"] == "0"
    assert received[3] == {
        "Action": "Hangup",
        "ActionID": "elvin-4",
        "Channel": "WebSocket/elvin/1",
        "Cause": "16",
    }
