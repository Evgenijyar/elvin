import asyncio

import httpx

from elvin.integrations.lptracker import LPTrackerClient


def test_move_lead_uses_official_funnel_endpoint_and_payload() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["token"] = request.headers.get("token")
        captured["body"] = request.content
        return httpx.Response(200, json={"status": "success", "result": True})

    async def exercise() -> None:
        client = LPTrackerClient("https://direct.lptracker.test")
        await client.client.aclose()
        client.client = httpx.AsyncClient(
            base_url="https://direct.lptracker.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await client.move_lead_to_stage("secret", 123, 456)
            assert result is True
        finally:
            await client.close()

    asyncio.run(exercise())
    assert captured["method"] == "PUT"
    assert captured["path"] == "/lead/123/funnel"
    assert captured["token"] == "secret"
    assert captured["body"] == b'{"funnel":456}'
