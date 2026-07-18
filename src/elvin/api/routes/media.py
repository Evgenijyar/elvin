"""Asterisk chan_websocket endpoint and media diagnostics."""

from __future__ import annotations

import importlib.util
import logging

from fastapi import APIRouter, Request, WebSocket
from starlette.websockets import WebSocketState

from elvin.media.asterisk_bridge import AsteriskGeminiBridge
from elvin.services.call_queue import CallQueueError, CallQueueManager, MediaCallContext

logger = logging.getLogger("elvin.media")
router = APIRouter(tags=["media"])


def _voice_runtime_available() -> bool:
    for name in ("pipecat", "pipecat_asterisk", "google.genai"):
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except ModuleNotFoundError:
            return False
    return True


@router.get("/media/status")
async def media_status(request: Request) -> dict[str, object]:
    queue: CallQueueManager = request.app.state.call_queue
    ready = request.app.state.media_ready and _voice_runtime_available()
    return {
        "status": "ready" if ready else "not_ready",
        "calls_enabled": request.app.state.calls_enabled,
        "media_ready": request.app.state.media_ready,
        "voice_runtime_available": _voice_runtime_available(),
        "sample_rate": 16000,
        "codec": "slin16",
        "asterisk_transport": "chan_websocket",
        "turn_detection": "Pipecat Silero VAD + Smart Turn v3",
        "gemini_server_vad": {"enabled": False},
        "gemini_preconnect_before_lptracker": True,
        "media_connect_timeout_seconds": queue.media_connect_timeout_seconds,
        "queue": await queue.media_status(),
    }


@router.websocket("/media/asterisk")
async def asterisk_media(websocket: WebSocket) -> None:
    requested_protocols = {
        item.strip()
        for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    }
    subprotocol = "media" if "media" in requested_protocols else None
    await websocket.accept(subprotocol=subprotocol)

    queue: CallQueueManager = websocket.app.state.call_queue
    context: MediaCallContext | None = None
    result = "completed"
    try:
        if not websocket.app.state.media_ready:
            raise CallQueueError("Медиаконтур Elvin не отмечен готовым.")
        if not _voice_runtime_available():
            raise RuntimeError("Voice runtime отсутствует в Docker-образе.")

        context = await queue.claim_media_session(timeout=8.0)

        async def terminate_socket() -> None:
            if websocket.application_state != WebSocketState.DISCONNECTED:
                await websocket.close(code=1000)

        await queue.register_media_terminator(context, terminate_socket)
        await queue.media_started(context.batch_id, context.lead_id)
        logger.warning(
            "Prepared media attached: batch=%s lead=%s gemini_ready=%s",
            context.batch_id,
            context.lead_id,
            context.voice_call.gemini.session is not None,
        )
        bridge = AsteriskGeminiBridge(websocket, context.voice_call)
        result = await bridge.run()
    except CallQueueError as exc:
        result = f"media_error:{exc}"
        logger.warning("Media correlation rejected: %s", exc)
    except Exception as exc:
        result = f"media_error:{type(exc).__name__}:{exc}"[:1000]
        logger.exception("Asterisk/Gemini media session failed")
    finally:
        if context is not None:
            await queue.unregister_media_terminator(context)
            await queue.media_finished(
                context.batch_id,
                context.lead_id,
                result,
            )
        if websocket.application_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close(code=1000)
            except RuntimeError:
                pass
