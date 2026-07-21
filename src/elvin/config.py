"""Validated application settings."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Elvin settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = Field(
        default="development",
        validation_alias="ELVIN_ENV",
    )
    domain: str = Field(default="localhost", validation_alias="ELVIN_DOMAIN")
    bind_host: str = Field(
        default="127.0.0.1",
        validation_alias="ELVIN_BIND_HOST",
    )
    bind_port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        validation_alias="ELVIN_BIND_PORT",
    )

    app_secret_key: SecretStr | None = Field(
        default=None,
        validation_alias="APP_SECRET_KEY",
    )
    credentials_encryption_key: SecretStr | None = Field(
        default=None,
        validation_alias="CREDENTIALS_ENCRYPTION_KEY",
    )
    gemini_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="GEMINI_API_KEY",
    )
    gemini_director_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="GEMINI_DIRECTOR_API_KEY",
    )

    data_dir: Path = Field(
        default=Path("./data"),
        validation_alias="ELVIN_DATA_DIR",
    )
    log_dir: Path = Field(
        default=Path("./logs"),
        validation_alias="ELVIN_LOG_DIR",
    )
    recordings_dir: Path = Field(
        default=Path("./recordings"),
        validation_alias="ELVIN_RECORDINGS_DIR",
    )

    db_host: str | None = Field(default=None, validation_alias="ELVIN_DB_HOST")
    db_port: int = Field(
        default=5432,
        ge=1,
        le=65535,
        validation_alias="ELVIN_DB_PORT",
    )
    db_name: str | None = Field(default=None, validation_alias="ELVIN_DB_NAME")
    db_user: str | None = Field(default=None, validation_alias="ELVIN_DB_USER")
    db_password: SecretStr | None = Field(
        default=None,
        validation_alias="ELVIN_DB_PASSWORD",
    )
    db_sslmode: Literal[
        "disable",
        "allow",
        "prefer",
        "require",
        "verify-ca",
        "verify-full",
    ] = Field(default="require", validation_alias="ELVIN_DB_SSLMODE")
    db_pool_min_size: int = Field(
        default=1,
        ge=1,
        validation_alias="ELVIN_DB_POOL_MIN_SIZE",
    )
    db_pool_max_size: int = Field(
        default=4,
        ge=1,
        validation_alias="ELVIN_DB_POOL_MAX_SIZE",
    )
    db_connect_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        validation_alias="ELVIN_DB_CONNECT_TIMEOUT_SECONDS",
    )
    db_command_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        validation_alias="ELVIN_DB_COMMAND_TIMEOUT_SECONDS",
    )

    lptracker_base_url: str = Field(
        default="https://direct.lptracker.ru",
        validation_alias="ELVIN_LPTRACKER_BASE_URL",
    )
    session_cookie_name: str = Field(
        default="elvin_session",
        validation_alias="ELVIN_SESSION_COOKIE_NAME",
    )
    session_ttl_hours: int = Field(
        default=168,
        ge=1,
        validation_alias="ELVIN_SESSION_TTL_HOURS",
    )
    calls_enabled: bool = Field(
        default=False,
        validation_alias="ELVIN_CALLS_ENABLED",
    )
    media_ready: bool = Field(
        default=False,
        validation_alias="ELVIN_MEDIA_READY",
    )
    media_connect_timeout_seconds: float = Field(
        default=900.0,
        ge=60.0,
        le=3600.0,
        validation_alias="ELVIN_MEDIA_CONNECT_TIMEOUT_SECONDS",
    )
    frame_trace_enabled: bool = Field(default=True, validation_alias="ELVIN_FRAME_TRACE_ENABLED")
    vad_confidence: float = Field(default=0.45, ge=0.05, le=0.99, validation_alias="ELVIN_VAD_CONFIDENCE")
    vad_start_seconds: float = Field(default=0.08, ge=0.02, le=1.0, validation_alias="ELVIN_VAD_START_SECONDS")
    vad_stop_seconds: float = Field(default=0.20, ge=0.04, le=2.0, validation_alias="ELVIN_VAD_STOP_SECONDS")
    vad_min_volume: float = Field(default=0.03, ge=0.0, le=1.0, validation_alias="ELVIN_VAD_MIN_VOLUME")
    pre_roll_ms: int = Field(default=240, ge=80, le=1000, validation_alias="ELVIN_PRE_ROLL_MS")
    smart_turn_retry_ms: int = Field(default=200, ge=100, le=2000, validation_alias="ELVIN_SMART_TURN_RETRY_MS")
    turn_merge_grace_ms: int = Field(default=300, ge=100, le=1200, validation_alias="ELVIN_TURN_MERGE_GRACE_MS")
    force_end_silence_ms: int = Field(default=900, ge=500, le=5000, validation_alias="ELVIN_FORCE_END_SILENCE_MS")
    pcm_level_log_interval_seconds: float = Field(default=1.0, ge=0.2, le=10.0, validation_alias="ELVIN_PCM_LEVEL_LOG_INTERVAL_SECONDS")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def database_configured(self) -> bool:
        password = (
            self.db_password.get_secret_value()
            if self.db_password is not None
            else ""
        )
        return bool(self.db_host and self.db_name and self.db_user and password)

    @property
    def public_base_url(self) -> str | None:
        domain = self.domain.strip().strip("/")
        if not domain or domain in {"localhost", "127.0.0.1"}:
            return None
        if domain.startswith("http://") or domain.startswith("https://"):
            return domain
        return f"https://{domain}"

    @property
    def gemini_key_configured(self) -> bool:
        return bool(
            self.gemini_api_key
            and self.gemini_api_key.get_secret_value().strip()
        )

    @property
    def gemini_director_key_configured(self) -> bool:
        return bool(
            self.gemini_director_api_key
            and self.gemini_director_api_key.get_secret_value().strip()
        )


@lru_cache
def get_settings() -> Settings:
    """Load and cache application settings."""
    return Settings()
