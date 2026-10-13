"""Google Gemini client using `generate_content` with manual function calling.

- Automatic function calling is off: the orchestrator runs every tool itself.
- The model's turn is kept as returned, so Gemini's thought signatures are sent back intact.
- History is exported with `model_dump_json` and restored with `model_validate_json`, which
  keeps the signatures' bytes (base64) exact across pause/resume.
- Calls are spaced by a rate limiter (the free tier allows few requests per minute) and
  retried with backoff on 429/5xx, except when the daily quota is exhausted.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

from app.agent.llm.base import LLMError, LLMResponse, ToolCall, ToolResult, ToolSpec, Usage

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*:\s*['\"](\d+(?:\.\d+)?)s")


class RateLimiter:
    """Keeps at least `min_interval` seconds between calls, across threads."""

    def __init__(self, min_interval: float, clock=time.monotonic, sleep=time.sleep):
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next_allowed:
                self._sleep(self._next_allowed - now)
                now = self._next_allowed
            self._next_allowed = now + self._min_interval


class GeminiClient:
    provider = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        thinking_level: str | None = "low",
        min_interval_seconds: float = 4.0,
        max_retries: int = 4,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.model = model
        self._client = client or genai.Client(api_key=api_key)
        self._thinking_level = thinking_level
        self._max_retries = max_retries
        self._sleep = sleep
        self._limiter = RateLimiter(min_interval_seconds, sleep=sleep)

    def new_session(
        self,
        system_prompt: str,
        tools: list[ToolSpec],
        history: list[dict[str, Any]] | None = None,
        tags: dict[str, str] | None = None,
    ) -> GeminiSession:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=tool.name,
                            description=tool.description,
                            parameters_json_schema=tool.parameters,
                        )
                        for tool in tools
                    ]
                )
            ],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=(
                types.ThinkingConfig(thinking_level=self._thinking_level)
                if self._thinking_level
                else None
            ),
        )
        restored = [types.Content.model_validate_json(json.dumps(item)) for item in history or []]
        return GeminiSession(self, config, restored)

    def generate(
        self, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        for attempt in range(self._max_retries + 1):
            self._limiter.wait()
            try:
                return self._client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except errors.APIError as exc:
                details = json.dumps(exc.details, default=str)
                if "PerDay" in details:
                    raise LLMError(
                        f"Gemini daily quota for {self.model} is exhausted; it resets at "
                        "midnight Pacific time. Try another model via GEMINI_MODEL.",
                    ) from exc
                retryable = exc.code in RETRYABLE_STATUS
                if not retryable or attempt == self._max_retries:
                    raise LLMError(
                        f"Gemini API error {exc.code}: {exc.message}", retryable=retryable
                    ) from exc
                delay = self._retry_delay(details, attempt)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise LLMError(f"Could not reach Gemini: {exc}", retryable=True) from exc
                delay = self._retry_delay("", attempt)
            log.warning("Gemini call failed (attempt %d); retrying in %.1fs", attempt + 1, delay)
            self._sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_delay(details: str, attempt: int) -> float:
        hinted = _RETRY_DELAY.search(details)
        if hinted:
            return min(float(hinted.group(1)) + 1.0, 90.0)
        return min(2.0 * 2**attempt, 60.0)


class GeminiSession:
    def __init__(
        self,
        client: GeminiClient,
        config: types.GenerateContentConfig,
        history: list[types.Content],
    ):
        self._client = client
        self._config = config
        self._history = history
        # Ids we invented for calls Gemini returned without one (older models).
        self._synthetic_ids: set[str] = set()

    def send_message(self, text: str) -> LLMResponse:
        self._history.append(types.Content(role="user", parts=[types.Part(text=text)]))
        return self._generate()

    def send_tool_results(self, results: list[ToolResult]) -> LLMResponse:
        # Parallel calls are answered together, in one user turn, matched by call id.
        parts = [
            types.Part(
                function_response=types.FunctionResponse(
                    id=None if r.call_id in self._synthetic_ids else r.call_id,
                    name=r.name,
                    response=r.payload,
                )
            )
            for r in results
        ]
        self._history.append(types.Content(role="user", parts=parts))
        return self._generate()

    def export_history(self) -> list[dict[str, Any]]:
        return [json.loads(content.model_dump_json(exclude_none=True)) for content in self._history]

    def _generate(self) -> LLMResponse:
        response = self._client.generate(self._history, self._config)
        candidate = response.candidates[0] if response.candidates else None
        if candidate is None or candidate.content is None or not candidate.content.parts:
            reason = candidate.finish_reason if candidate else "no candidates"
            raise LLMError(f"Gemini returned no content (finish reason: {reason})")

        # Keep the model's turn exactly as returned: it carries the thought signature.
        self._history.append(candidate.content)

        calls = []
        for index, part in enumerate(candidate.content.parts):
            call = part.function_call
            if call is None:
                continue
            call_id = call.id
            if not call_id:
                call_id = f"call-{len(self._history)}-{index}"
                self._synthetic_ids.add(call_id)
            calls.append(ToolCall(id=call_id, name=call.name, args=dict(call.args or {})))

        text = "".join(p.text for p in candidate.content.parts if p.text and not p.thought)
        usage = response.usage_metadata
        return LLMResponse(
            text=text or None,
            tool_calls=calls,
            usage=Usage(
                prompt_tokens=(usage.prompt_token_count or 0) if usage else 0,
                output_tokens=(usage.candidates_token_count or 0) if usage else 0,
                thought_tokens=(usage.thoughts_token_count or 0) if usage else 0,
            ),
            finish_reason=str(candidate.finish_reason) if candidate.finish_reason else None,
        )
