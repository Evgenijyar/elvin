"""Authenticated UI API for the isolated direct-SIP playback test."""

from __future__ import annotations

import asyncio
import logging
import shutil
import wave
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from elvin.api.dependencies import require_session
from elvin.services.telephony_test import (
    TelephonyTestError,
    TelephonyTestService,
    mask_phone_number,
    normalize_phone_number,
)

logger = logging.getLogger("elvin.telephony_test")
router = APIRouter(prefix="/telephony-test", tags=["telephony-test"])

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_MAX_AUDIO_SECONDS = 10 * 60
_ALLOWED_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}


def _service(request: Request) -> TelephonyTestService:
    return request.app.state.telephony_test


@router.get("/status")
async def telephony_test_status(
    request: Request,
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    service = _service(request)
    return {
        "configured": service.configured,
        "transport": "Asterisk AMI Originate → PJSIP/lptracker-endpoint",
        "production_call_flow_untouched": True,
    }


@router.post("/calls", status_code=status.HTTP_202_ACCEPTED)
async def start_telephony_test(
    request: Request,
    phone: Annotated[str, Form(...)],
    file: Annotated[UploadFile, File(...)],
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    service = _service(request)
    if not service.configured:
        raise HTTPException(status_code=503, detail="Asterisk AMI не настроен.")
    try:
        normalized_phone = normalize_phone_number(phone)
    except TelephonyTestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    original_name = Path(file.filename or "test-audio").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Поддерживаются WAV, MP3, M4A, AAC, FLAC, OGG, OPUS и WEBM.",
        )
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(
            status_code=503,
            detail="На сервере не найден ffmpeg для подготовки тестового аудио.",
        )

    test_id = uuid4().hex
    service.audio_dir.mkdir(parents=True, exist_ok=True)
    source_path = service.audio_dir / f".{test_id}{suffix}"
    output_tmp = service.audio_dir / f".{test_id}.tmp.wav"
    target_path = service.audio_dir / f"{test_id}.wav"
    total_bytes = 0
    logger.warning(
        "Telephony test upload started: test=%s phone=%s filename=%s",
        test_id,
        mask_phone_number(normalized_phone),
        original_name,
    )
    try:
        with source_path.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > _MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Аудиофайл не должен превышать 50 МБ.",
                    )
                destination.write(chunk)
        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="Загруженный аудиофайл пуст.")

        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-t",
            str(_MAX_AUDIO_SECONDS),
            "-ac",
            "1",
            "-ar",
            "8000",
            "-acodec",
            "pcm_s16le",
            str(output_tmp),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise HTTPException(
                status_code=408,
                detail="Истекло время подготовки аудиофайла.",
            ) from exc
        if (
            process.returncode != 0
            or not output_tmp.exists()
            or output_tmp.stat().st_size < 46
        ):
            message = stderr.decode("utf-8", errors="replace").strip()
            raise HTTPException(
                status_code=400,
                detail=(
                    "Не удалось декодировать аудиофайл."
                    + (f" {message[:300]}" if message else "")
                ),
            )
        try:
            with wave.open(str(output_tmp), "rb") as audio:
                if (
                    audio.getnchannels() != 1
                    or audio.getsampwidth() != 2
                    or audio.getframerate() != 8000
                ):
                    raise HTTPException(
                        status_code=500,
                        detail="ffmpeg подготовил аудио в неожиданном формате.",
                    )
                duration_seconds = audio.getnframes() / audio.getframerate()
        except (wave.Error, EOFError) as exc:
            raise HTTPException(
                status_code=400,
                detail="Подготовленный WAV-файл повреждён.",
            ) from exc
        if duration_seconds <= 0:
            raise HTTPException(status_code=400, detail="В аудиофайле нет звука.")

        output_tmp.replace(target_path)
        target_path.chmod(0o644)
        logger.warning(
            "Telephony test audio prepared: test=%s bytes=%s duration=%.3fs "
            "format=wav/pcm_s16le/8000/mono",
            test_id,
            target_path.stat().st_size,
            duration_seconds,
        )
        try:
            call = await service.start_call(
                test_id=test_id,
                phone=normalized_phone,
                original_filename=original_name,
                audio_path=target_path,
                duration_seconds=duration_seconds,
            )
        except TelephonyTestError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"success": True, "call": call}
    finally:
        await file.close()
        source_path.unlink(missing_ok=True)
        output_tmp.unlink(missing_ok=True)
        if target_path.exists() and service.get_call(test_id) is None:
            target_path.unlink(missing_ok=True)


@router.get("/calls/{test_id}")
async def get_telephony_test(
    test_id: str,
    request: Request,
    _session: Annotated[str, Depends(require_session)],
) -> dict[str, object]:
    if not test_id or any(character not in "0123456789abcdef" for character in test_id):
        raise HTTPException(status_code=404, detail="Тестовый звонок не найден.")
    call = _service(request).get_call(test_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Тестовый звонок не найден.")
    return {"call": call}
