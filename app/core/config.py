"""Application settings — loaded from environment variables with `.env` as fallback.

Every knob the deployment may need lives here; nothing else in the code base
reads `os.environ` directly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    # ---- runtime -----------------------------------------------------------
    app_name: str = "TibDaftari API"
    app_env: Literal["development", "test", "production"] = "development"
    port: int = 8000
    log_level: str = "INFO"
    log_json: bool = False
    # Uvicorn worker count. Keep 1 when WORKERS_ENABLED / TELEGRAM_ENABLED (single
    # dispatcher); scale horizontally with more containers instead.
    web_concurrency: int = 1

    # ---- data stores -------------------------------------------------------
    database_url: str
    redis_url: str
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    # ---- security ----------------------------------------------------------
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    staff_token_ttl_hours: int = 12
    patient_token_ttl_hours: int = 24 * 30
    # Fernet key (urlsafe base64, 32 bytes). Empty → derived from JWT_SECRET (dev only).
    encryption_key: str = ""
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5180"]
    public_api_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:5180"
    # Brute-force protection
    login_max_attempts: int = 10
    login_lock_minutes: int = 15
    rate_limit_per_minute: int = 600  # per IP, all endpoints
    auth_rate_limit_per_minute: int = 20  # per IP, auth endpoints
    max_request_body_bytes: int = 15 * 1024 * 1024  # assets / imports (base64 images)
    trust_proxy_headers: bool = True

    # ---- integrations ------------------------------------------------------
    xabarchi_base_url: str = "https://manager-xabarchi-backend-bula2s-f6aaa1-13-140-185-49.sslip.io"
    xabarchi_timeout_seconds: float = 15
    # Returns the OTP in the API response / logs. Never honoured in production.
    otp_dev_mode: bool = False
    otp_ttl_seconds: int = 300
    otp_length: int = 4
    otp_max_attempts: int = 3
    otp_resend_cooldown_seconds: int = 60

    # ---- background workers ------------------------------------------------
    workers_enabled: bool = True
    telegram_enabled: bool = True
    outbox_poll_seconds: float = 2.0
    outbox_batch_size: int = 50
    outbox_max_attempts: int = 5

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [o.strip() for o in value.split(",") if o.strip()]
        return value

    @model_validator(mode="after")
    def _no_otp_dev_mode_in_production(self) -> Settings:
        if self.app_env == "production" and self.otp_dev_mode:
            self.otp_dev_mode = False
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def sqlalchemy_url(self) -> str:
        """DATABASE_URL rewritten for the asyncpg driver."""
        if self.database_url.startswith("postgresql+asyncpg://"):
            return self.database_url
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
