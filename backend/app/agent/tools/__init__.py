"""Tool registry and executor: the only way the agent touches the world."""

from __future__ import annotations

import logging

from pydantic import ValidationError

from app.agent.llm.base import ToolCall, ToolSpec
from app.agent.tools.actions import ACTION_TOOLS
from app.agent.tools.base import (
    REQUIRED_EVIDENCE,
    RunContext,
    Tool,
    ToolError,
    collect_numbers,
    error,
    ok,
)
from app.agent.tools.investigate import INVESTIGATION_TOOLS
from app.services.context import ContextNotFoundError
from app.services.purchase_orders import PurchaseOrderError

log = logging.getLogger(__name__)

TOOLS: dict[str, Tool] = {tool.name: tool for tool in [*INVESTIGATION_TOOLS, *ACTION_TOOLS]}

__all__ = ["REQUIRED_EVIDENCE", "TOOLS", "RunContext", "execute_tool", "tool_specs"]


def tool_specs() -> list[ToolSpec]:
    return [tool.spec() for tool in TOOLS.values()]


def execute_tool(ctx: RunContext, call: ToolCall) -> dict:
    """Run one tool call and return its result envelope. Never raises."""
    tool = TOOLS.get(call.name)
    if tool is None:
        return error("UNKNOWN_TOOL", f"No tool named '{call.name}'", {"tools": sorted(TOOLS)})

    try:
        args = tool.args_model.model_validate(call.args or {})
    except ValidationError as exc:
        problems = [
            {"field": ".".join(str(p) for p in e["loc"]) or "(arguments)", "problem": e["msg"]}
            for e in exc.errors()
        ]
        return error(
            "INVALID_ARGUMENTS", f"Invalid arguments for {call.name}", {"errors": problems}
        )

    # Count evidence as gathered even if the read fails: the agent looked, the data was missing.
    if tool.evidence:
        ctx.evidence.add(tool.evidence)
    ctx.call_id = call.id
    result = _run_handler(ctx, tool, args)
    # Numbers the model has seen (results and error details) are what it may cite.
    collect_numbers(result, ctx.observed_numbers)
    return result


def _run_handler(ctx: RunContext, tool: Tool, args) -> dict:
    try:
        return ok(tool.handler(ctx, args))
    except ToolError as exc:
        return error(exc.code, exc.message, exc.details)
    except PurchaseOrderError as exc:
        details = None
        if exc.evaluation is not None:
            details = {
                "hard_violations": [f.model_dump() for f in exc.evaluation.hard_violations],
                "limits": exc.evaluation.limits.model_dump(),
            }
        return error(exc.code, exc.message, details)
    except ContextNotFoundError as exc:
        return error("DATA_NOT_FOUND", str(exc))
    except Exception:
        # Keep the run alive: log the bug, reset the DB session, tell the model it failed.
        log.exception("Tool %s crashed", tool.name)
        ctx.session.rollback()
        return error("INTERNAL_ERROR", f"{tool.name} failed unexpectedly; try another approach.")
