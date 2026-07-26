"""Configuration loaded from environment variables / .env file."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PI Web API
    pi_base_url: str = "http://localhost:8080/piwebapi"
    pi_auth_mode: str = "none"  # none | basic
    pi_username: str = ""
    pi_password: str = ""
    pi_verify_ssl: bool = True
    pi_points: str = ""
    poll_interval_seconds: float = 5.0

    # Backfill / recovery: read recorded values since the last checkpoint.
    # On first run (no checkpoint) seed this many minutes of history.
    backfill_start_minutes: int = 10
    recorded_max_count: int = 1000  # max samples per tag per request

    # Health / metrics HTTP server.
    health_port: int = 9100

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = "pi"
    redis_ts_retention_ms: int = 604_800_000

    @property
    def points(self) -> list[str]:
        return [p.strip() for p in self.pi_points.split(",") if p.strip()]


settings = Settings()
