"""Provider-neutral interface between the agent loop and an LLM.

The loop only sees `ToolCall`, `ToolResult` and `LLMResponse`. Each session keeps its
conversation history in the provider's native format, so provider details (e.g. Gemini
thought signatures) survive, and exports it as JSON for persistence and pause/resume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the arguments


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    payload: dict[str, Any]


@dataclass
class Usage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None


class LLMError(RuntimeError):
    """The model could not produce a response (after any retries)."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class LLMSession(Protocol):
    def send_message(self, text: str) -> LLMResponse: ...

    def send_tool_results(self, results: list[ToolResult]) -> LLMResponse: ...

    def export_history(self) -> list[dict[str, Any]]: ...


class LLMClient(Protocol):
    provider: str
    model: str

    def new_session(
        self,
        system_prompt: str,
        tools: list[ToolSpec],
        history: list[dict[str, Any]] | None = None,
    ) -> LLMSession:
        """Start a conversation, or continue one from exported `history`."""
        ...
