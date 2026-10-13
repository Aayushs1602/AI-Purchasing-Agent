"""Application settings, read from environment variables and the repo-root `.env`."""

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    # Comma-separated so it can be set from a single env var.
    cors_origins: str = "http://localhost:5173"

    database_url: str = f"sqlite:///{(BACKEND_DIR / 'purchasing.db').as_posix()}"
    scenarios_dir: Path = BACKEND_DIR / "scenarios"
    # Simulated "today". Fixed so forecasts, ETAs and sales windows never drift between runs.
    sim_today: date = date(2026, 10, 1)
    # Lets POST /api/admin/reset wipe and reseed the database. Turn off outside demos.
    allow_demo_reset: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
