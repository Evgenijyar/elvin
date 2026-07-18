"""Gemini Live constants and connection validation.

The validation path deliberately mirrors the working Java implementation:
direct WebSocket connection, v1beta BidiGenerateContent endpoint, model
resource name prefixed with ``models/``, and generation settings nested
under ``generationConfig``.
"""

import asyncio
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidProxy,
    InvalidStatus,
)

logger = logging.getLogger("elvin.gemini")

GEMINI_LIVE_MODEL_ID = "gemini-3.1-flash-live-preview"
GEMINI_LIVE_WEBSOCKET_ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService."
    "BidiGenerateContent"
)
GEMINI_MODEL_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_LIVE_MODEL_ID}"
)


class GeminiConnectionError(RuntimeError):
    """Raised when a Gemini Live session cannot be established."""


def _extract_google_error(payload: str) -> str:
    """Extract a readable Google API error without exposing the key."""
    text = payload.strip()
    if not text:
        return ""

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text[:1000]

    error = parsed.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        status = error.get("status")
        code = error.get("code")
        pieces = [
            str(piece)
            for piece in (code, status, message)
            if piece not in (None, "")
        ]
        if pieces:
            return " | ".join(pieces)

    return text[:1000]


def _validate_key_and_model_sync(
    api_key: str,
    timeout_seconds: float,
) -> dict[str, str]:
    """Validate the API key and model through the official Models API."""
    request = Request(
        GEMINI_MODEL_ENDPOINT,
        method="GET",
        headers={
            "x-goog-api-key": api_key,
            "Accept": "application/json",
            "User-Agent": "elvin-backend/0.4",
        },
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = _extract_google_error(body)
        raise GeminiConnectionError(
            "Gemini Models API отклонил ключ или модель: "
            f"HTTP {exc.code}"
            + (f" | {detail}" if detail else "")
        ) from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise GeminiConnectionError(
            "Не удалось обратиться к Gemini Models API: "
            f"{type(reason).__name__}: {reason}"
        ) from exc
    except TimeoutError as exc:
        raise GeminiConnectionError(
            "Gemini Models API не ответил за отведённое время."
        ) from exc

    try:
        model = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GeminiConnectionError(
            "Gemini Models API вернул некорректный JSON."
        ) from exc

    returned_name = str(model.get("name") or "")
    expected_name = f"models/{GEMINI_LIVE_MODEL_ID}"
    if returned_name and returned_name != expected_name:
        raise GeminiConnectionError(
            "Gemini Models API вернул неожиданную модель: "
            f"{returned_name}"
        )

    return {
        "model_name": returned_name or expected_name,
        "display_name": str(model.get("displayName") or ""),
    }


def _setup_message(model_id: str) -> dict[str, object]:
    """Build the same proven setup structure as the Java application."""
    normalized_model = (
        model_id if model_id.startswith("models/") else f"models/{model_id}"
    )

    return {
        "setup": {
            "model": normalized_model,
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 4096,
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": "Kore",
                        }
                    }
                },
            },
            "systemInstruction": {
                "parts": [
                    {
                        "text": (
                            "Connection validation only. "
                            "Do not start a conversation."
                        )
                    }
                ]
            },
        }
    }


def _websocket_status_detail(exc: BaseException) -> str:
    """Return useful handshake diagnostics across websockets versions."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    body = getattr(response, "body", None)

    pieces: list[str] = []
    if status_code is not None:
        pieces.append(f"HTTP {status_code}")

    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str) and body.strip():
        detail = _extract_google_error(body)
        if detail:
            pieces.append(detail)

    text = str(exc).strip()
    if text and text not in pieces:
        pieces.append(text)

    return " | ".join(pieces) or type(exc).__name__


def _websocket_close_detail(exc: ConnectionClosed) -> str:
    """Return the close code and reason supplied by Gemini."""
    received = getattr(exc, "rcvd", None)
    code = getattr(received, "code", None)
    reason = getattr(received, "reason", None)

    if code is None:
        code = getattr(exc, "code", None)
    if not reason:
        reason = getattr(exc, "reason", None)

    pieces = []
    if code is not None:
        pieces.append(f"code={code}")
    if reason:
        pieces.append(f"reason={reason}")

    text = str(exc).strip()
    if text and not pieces:
        pieces.append(text)

    return ", ".join(pieces) or "соединение закрыто без причины"


async def test_gemini_live_connection(
    api_key: str,
    *,
    model_id: str = GEMINI_LIVE_MODEL_ID,
    timeout_seconds: float = 15.0,
) -> dict[str, str]:
    """Validate the key/model and wait for Gemini Live setupComplete."""
    clean_key = api_key.strip()
    if not clean_key:
        raise GeminiConnectionError("Gemini API key не указан.")

    model_info = await asyncio.to_thread(
        _validate_key_and_model_sync,
        clean_key,
        min(timeout_seconds, 10.0),
    )

    url = (
        f"{GEMINI_LIVE_WEBSOCKET_ENDPOINT}"
        f"?key={quote(clean_key, safe='')}"
    )
    setup_message = _setup_message(model_id)

    logger.info(
        "Gemini Live validation started: endpoint=%s model=%s "
        "proxy=disabled compression=disabled",
        GEMINI_LIVE_WEBSOCKET_ENDPOINT,
        model_id,
    )

    try:
        async with asyncio.timeout(timeout_seconds):
            # websockets 15+ automatically uses a Windows/system proxy.
            # The working Java client connects directly, therefore the
            # validation client explicitly disables proxy discovery.
            async with websockets.connect(
                url,
                proxy=None,
                compression=None,
                open_timeout=10,
                close_timeout=2,
                ping_interval=None,
                max_size=4 * 1024 * 1024,
            ) as websocket:
                await websocket.send(
                    json.dumps(
                        setup_message,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )

                while True:
                    raw_message = await websocket.recv(decode=False)
                    if isinstance(raw_message, bytes):
                        raw_message = raw_message.decode(
                            "utf-8",
                            errors="replace",
                        )

                    try:
                        data = json.loads(raw_message)
                    except json.JSONDecodeError as exc:
                        raise GeminiConnectionError(
                            "Gemini Live вернул сообщение, "
                            "которое не удалось разобрать как JSON."
                        ) from exc

                    if "setupComplete" in data:
                        logger.info(
                            "Gemini Live setupComplete received: model=%s",
                            model_id,
                        )
                        return {
                            "status": "ok",
                            "model_id": model_id,
                            "model_name": model_info["model_name"],
                            "message": (
                                "Gemini API key, модель и Live WebSocket "
                                "проверены: setupComplete получен."
                            ),
                        }

                    error = data.get("error")
                    if isinstance(error, dict):
                        detail = _extract_google_error(
                            json.dumps(error, ensure_ascii=False)
                        )
                        raise GeminiConnectionError(
                            "Gemini Live вернул ошибку"
                            + (f": {detail}" if detail else ".")
                        )

                    if "goAway" in data:
                        raise GeminiConnectionError(
                            "Gemini Live прислал goAway до setupComplete: "
                            f"{json.dumps(data['goAway'], ensure_ascii=False)}"
                        )

    except GeminiConnectionError:
        raise
    except TimeoutError as exc:
        raise GeminiConnectionError(
            "Gemini Live не вернул setupComplete "
            f"за {timeout_seconds:.0f} секунд."
        ) from exc
    except ConnectionClosed as exc:
        raise GeminiConnectionError(
            "Gemini Live закрыл WebSocket до setupComplete: "
            f"{_websocket_close_detail(exc)}"
        ) from exc
    except (InvalidStatus, InvalidHandshake, InvalidProxy) as exc:
        raise GeminiConnectionError(
            "Не удалось выполнить WebSocket handshake с Gemini Live: "
            f"{_websocket_status_detail(exc)}"
        ) from exc
    except OSError as exc:
        raise GeminiConnectionError(
            "Сетевая ошибка при подключении к Gemini Live: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected Gemini Live validation failure.")
        raise GeminiConnectionError(
            "Неожиданная ошибка Gemini Live: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    raise GeminiConnectionError(
        "Gemini Live завершил проверку без setupComplete."
    )
