"""Application settings, read from environment variables and the repo-root `.env`."""

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
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
    # Read-only database browser at /admin. It has no login, so keep it off outside local dev.
    admin_ui_enabled: bool = True

    # --- LLM ---
    # "replay" plays back recorded runs from recordings_dir: no API key needed.
    llm_provider: Literal["gemini", "replay"] = "gemini"
    recordings_dir: Path = BACKEND_DIR / "recordings"
    gemini_api_key: SecretStr | None = None  # SecretStr keeps the key out of logs and reprs
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_thinking_level: str = "low"
    # The free tier allows only a few requests per minute; space calls out.
    llm_min_interval_seconds: float = 4.0
    llm_max_retries: int = 4
    # Hard cap on model calls per agent run (a normal run needs 5-8).
    agent_max_llm_calls: int = 16

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
