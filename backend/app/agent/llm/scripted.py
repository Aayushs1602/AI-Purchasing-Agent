"""A deterministic stand-in for the LLM, for tests and offline evals.

It plays back a script of turns. Each turn is one of:
- a list of tool calls, e.g. [{"name": "get_budget"}, {"name": "get_inventory", "args": {}}]
- a string: a final text reply with no tool calls
- a callable receiving the previous tool results and returning either of the above

The script position lives on the client, so a resumed session continues where it paused.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from app.agent.llm.base import LLMError, LLMResponse, ToolCall, ToolResult, ToolSpec

ScriptedCall = dict[str, Any]
ScriptedTurn = list[ScriptedCall] | str | Callable[[list[ToolResult]], list[ScriptedCall] | str]


class ScriptedClient:
    provider = "scripted"
    model = "scripted"

    def __init__(self, script: Iterable[ScriptedTurn]):
        self._turns = iter(script)
        self._turn_count = 0
        self.sessions: list[ScriptedSession] = []

    def new_session(
        self,
        system_prompt: str,
        tools: list[ToolSpec],
        history: list[dict[str, Any]] | None = None,
    ) -> ScriptedSession:
        session = ScriptedSession(self, system_prompt, tools, list(history or []))
        self.sessions.append(session)
        return session

    def next_turn(self, results: list[ToolResult]) -> LLMResponse:
        try:
            turn = next(self._turns)
        except StopIteration:
            raise LLMError("Scripted LLM ran out of turns") from None
        self._turn_count += 1
        if callable(turn):
            turn = turn(results)
        if isinstance(turn, str):
            return LLMResponse(text=turn, tool_calls=[])
        calls = [
            ToolCall(id=f"call-{self._turn_count}-{i}", name=c["name"], args=c.get("args", {}))
            for i, c in enumerate(turn)
        ]
        return LLMResponse(text=None, tool_calls=calls)


class ScriptedSession:
    def __init__(
        self,
        client: ScriptedClient,
        system_prompt: str,
        tools: list[ToolSpec],
        history: list[dict[str, Any]],
    ):
        self._client = client
        self.system_prompt = system_prompt
        self.tools = tools
        self._history = history
        self.received_results: list[list[ToolResult]] = []

    def send_message(self, text: str) -> LLMResponse:
        self._history.append({"role": "user", "text": text})
        return self._reply([])

    def send_tool_results(self, results: list[ToolResult]) -> LLMResponse:
        self.received_results.append(results)
        self._history.append(
            {"role": "user", "results": [{"name": r.name, "payload": r.payload} for r in results]}
        )
        return self._reply(results)

    def export_history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def _reply(self, results: list[ToolResult]) -> LLMResponse:
        response = self._client.next_turn(results)
        self._history.append(
            {
                "role": "model",
                "text": response.text,
                "calls": [{"name": c.name, "args": c.args} for c in response.tool_calls],
            }
        )
        return response
