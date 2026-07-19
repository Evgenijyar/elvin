"""FastAPI application entry point for the new Elvin voice platform."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from elvin import __version__
from elvin.api.routes import auth, dashboard, media, projects, robots, settings, system, webhooks
from elvin.config import get_settings
from elvin.infrastructure.state_store import StateStore
from elvin.integrations.lptracker import LPTrackerClient, LPTrackerError
from elvin.media.runtime import VoiceRuntime, preload_voice_runtime
from elvin.media.turn_detector import TurnDetectorConfig
from elvin.services.call_queue import CallQueueManager

logger = logging.getLogger("elvin")
PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
STATIC_DIR = WEB_DIR / "static"


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    # File logging is intentionally handled by Docker/systemd. The hot audio
    # path never writes a line per 20ms frame; frame traces use an async writer.


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    app_settings = get_settings()
    for directory in (app_settings.data_dir, app_settings.log_dir, app_settings.recordings_dir):
        directory.mkdir(parents=True, exist_ok=True)
    configure_logging(app_settings.log_dir)

    store = StateStore(app_settings)
    await store.initialize()
    lptracker = LPTrackerClient(app_settings.lptracker_base_url)

    media_marker = app_settings.data_dir / "media-ready"
    effective_media_ready = app_settings.media_ready or media_marker.is_file()
    effective_calls_enabled = app_settings.calls_enabled or effective_media_ready

    voice_runtime = VoiceRuntime(
        recordings_dir=app_settings.recordings_dir,
        trace_enabled=app_settings.frame_trace_enabled,
        turn_config=TurnDetectorConfig(
            vad_confidence=app_settings.vad_confidence,
            vad_start_secs=app_settings.vad_start_seconds,
            vad_stop_secs=app_settings.vad_stop_seconds,
            vad_min_volume=app_settings.vad_min_volume,
            pre_roll_ms=app_settings.pre_roll_ms,
            smart_turn_retry_ms=app_settings.smart_turn_retry_ms,
            turn_merge_grace_ms=app_settings.turn_merge_grace_ms,
            force_end_silence_ms=app_settings.force_end_silence_ms,
            level_log_interval_seconds=app_settings.pcm_level_log_interval_seconds,
        ),
    )

    if effective_media_ready:
        logger.warning("Preloading Pipecat Silero VAD and Smart Turn before accepting calls...")
        await asyncio.to_thread(preload_voice_runtime)

    call_queue = CallQueueManager(
        store,
        lptracker,
        voice_runtime,
        app_settings,
        calls_enabled=effective_calls_enabled,
        media_ready=effective_media_ready,
        media_connect_timeout_seconds=app_settings.media_connect_timeout_seconds,
    )

    application.state.settings = app_settings
    application.state.store = store
    application.state.lptracker = lptracker
    application.state.voice_runtime = voice_runtime
    application.state.call_queue = call_queue
    application.state.calls_enabled = effective_calls_enabled
    application.state.media_ready = effective_media_ready

    logger.warning(
        "Elvin started: env=%s storage=%s calls_enabled=%s media_ready=%s "
        "Gemini_preconnect=true server_vad=false",
        app_settings.environment,
        store.mode,
        effective_calls_enabled,
        effective_media_ready,
    )
    try:
        yield
    finally:
        await call_queue.close()
        await lptracker.close()
        await store.close()


def create_app() -> FastAPI:
    application = FastAPI(
        title="Elvin Voice Backend",
        description="LPTracker + Asterisk chan_websocket + local VAD + Gemini Live.",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )

    for route in (system, auth, projects, robots, settings, dashboard, webhooks, media):
        application.include_router(route.router, prefix="/api")

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.middleware("http")
    async def disable_frontend_cache(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @application.exception_handler(LPTrackerError)
    async def handle_lptracker_error(_request: Request, exc: LPTrackerError) -> JSONResponse:
        code = 401 if exc.http_status == 401 or exc.api_code == 401 else 502
        return JSONResponse(status_code=code, content={"detail": str(exc)})

    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    return application


app = create_app()
