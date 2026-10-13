"""Record live model turns, and replay them later without an API key.

A recording holds what the model said (text and tool calls) plus a small signature of how the
tools answered each turn (tool name, ok or error code) - never the results themselves. On
replay the tools, the PO service and every validator run for real against fresh data, and the
signatures are compared: if the code or data has drifted since recording, replay fails loudly
instead of quietly telling a different story.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.agent.llm.base import (
    LLMClient,
    LLMError,
    LLMResponse,
    LLMSession,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)

RECORDING_FORMAT = 1


def _signature(results: Iterable[ToolResult]) -> list[dict[str, Any]]:
    """How the tools answered: name and success or error code, not the data."""
    return [
        {
            "name": r.name,
            "ok": bool(r.payload.get("ok")),
            "error": None if r.payload.get("ok") else r.payload.get("error", {}).get("code"),
        }
        for r in results
    ]


def _counted(signature: list[dict[str, Any]]) -> Counter:
    return Counter((s["name"], s["ok"], s["error"]) for s in signature)


def _turn(response: LLMResponse) -> dict[str, Any]:
    return {
        "text": response.text,
        "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in response.tool_calls],
        "usage": vars(response.usage),
        "finish_reason": response.finish_reason,
    }


class RecordingClient:
    """Wraps a real client and keeps every model turn, so the run can be replayed."""

    def __init__(self, inner: LLMClient):
        self.inner = inner
        self.provider = inner.provider
        self.model = inner.model
        self.turns: list[dict[str, Any]] = []

    def new_session(
        self,
        system_prompt: str,
        tools: list[ToolSpec],
        history: list[dict[str, Any]] | None = None,
        tags: dict[str, str] | None = None,
    ) -> LLMSession:
        inner = self.inner.new_session(system_prompt, tools, history=history, tags=tags)
        return RecordingSession(inner, self.turns)

    def save(self, path: Path, **metadata: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "format": RECORDING_FORMAT,
            "provider": self.provider,
            "model": self.model,
            **metadata,
            "turns": self.turns,
        }
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


class RecordingSession:
    def __init__(self, inner: LLMSession, turns: list[dict[str, Any]]):
        self._inner = inner
        self._turns = turns

    def send_message(self, text: str) -> LLMResponse:
        return self._keep(self._inner.send_message(text))

    def send_tool_results(self, results: list[ToolResult]) -> LLMResponse:
        if self._turns:
            # These results answer the previous turn's calls.
            self._turns[-1]["results"] = _signature(results)
        return self._keep(self._inner.send_tool_results(results))

    def export_history(self) -> list[dict[str, Any]]:
        return self._inner.export_history()

    def _keep(self, response: LLMResponse) -> LLMResponse:
        self._turns.append(_turn(response))
        return response


class ReplayClient:
    """Plays back the recording for the run's scenario (picked from the run's tags)."""

    provider = "replay"
    model = "replay"

    def __init__(self, directory: Path):
        self.directory = directory

    def new_session(
        self,
        system_prompt: str,
        tools: list[ToolSpec],
        history: list[dict[str, Any]] | None = None,
        tags: dict[str, str] | None = None,
    ) -> ReplaySession:
        scenario = (tags or {}).get("scenario")
        path = self.directory / f"{scenario}.json"
        if not path.exists():
            raise LLMError(
                f"No recording for scenario '{scenario}' in {self.directory}. Record one with: "
                f"python -m app.cli run {scenario} --reset --record"
            )
        recording = json.loads(path.read_text(encoding="utf-8"))
        return ReplaySession(recording["turns"], list(history or []))


class ReplaySession:
    def __init__(self, turns: list[dict[str, Any]], history: list[dict[str, Any]]):
        self._turns = turns
        self._history = history  # [{"role": "user" | "model", ...}]; resumable

    def send_message(self, text: str) -> LLMResponse:
        self._history.append({"role": "user", "text": text})
        return self._next()

    def send_tool_results(self, results: list[ToolResult]) -> LLMResponse:
        played = self._played()
        recorded = self._turns[played - 1].get("results") if played else None
        if recorded is not None and _counted(_signature(results)) != _counted(recorded):
            now = _counted(_signature(results))
            changed = sorted(
                f"{n}: {'ok' if ok else err}" for n, ok, err in now - _counted(recorded)
            )
            raise LLMError(
                "Replay diverged from the recording: the tools answered differently "
                f"({', '.join(changed)}). The code or data has changed since it was recorded; "
                "re-record it."
            )
        self._history.append({"role": "user", "results": [r.name for r in results]})
        return self._next()

    def export_history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def _played(self) -> int:
        return sum(1 for entry in self._history if entry["role"] == "model")

    def _next(self) -> LLMResponse:
        index = self._played()
        if index >= len(self._turns):
            raise LLMError(
                "The recording ended before the run did; the code or data has changed since it "
                "was recorded. Re-record it."
            )
        turn = self._turns[index]
        self._history.append({"role": "model", "turn": index})
        return LLMResponse(
            text=turn["text"],
            tool_calls=[ToolCall(c["id"], c["name"], c["args"]) for c in turn["tool_calls"]],
            usage=Usage(**turn.get("usage", {})),
            finish_reason=turn.get("finish_reason"),
        )
