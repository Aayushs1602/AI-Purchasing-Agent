"""Build the configured LLM client."""

from __future__ import annotations

from app.agent.llm.base import LLMClient
from app.agent.llm.gemini import GeminiClient
from app.config import Settings


class LLMConfigError(RuntimeError):
    pass


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "gemini":
        key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else ""
        if not key:
            raise LLMConfigError(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and add a key from "
                "https://aistudio.google.com/apikey"
            )
        return GeminiClient(
            api_key=key,
            model=settings.gemini_model,
            thinking_level=settings.gemini_thinking_level or None,
            min_interval_seconds=settings.llm_min_interval_seconds,
            max_retries=settings.llm_max_retries,
        )
    raise LLMConfigError(f"Unknown LLM provider '{settings.llm_provider}'")
