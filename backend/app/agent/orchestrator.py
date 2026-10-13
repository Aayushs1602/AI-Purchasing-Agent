"""Runs the agent: the tool-use loop around the LLM, with limits and a full trace.

    kickoff ─► model turn ─► execute tool calls ─► results back to the model ─► ...
                  │                                     └─ until finalize_decision succeeds
                  └─ no tool call: nudge once, then stop the run as failed

Guardrails owned by the loop (not the model): a budget of model calls per run, one nudge
when the model stops calling tools, LLM failures ending the run cleanly, and every turn,
tool call and intervention written to `agent_steps` as it happens.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.llm.base import LLMClient, LLMError, LLMResponse, LLMSession, ToolCall, ToolResult
from app.agent.prompts import NUDGE, SYSTEM_PROMPT, kickoff_message
from app.agent.tools import RunContext, execute_tool, tool_specs
from app.engine.policy import DEFAULT_POLICY, PolicyConfig
from app.models import (
    AgentRun,
    AgentStep,
    Escalation,
    Node,
    Product,
    PurchaseOrder,
    Recommendation,
    Supplier,
)
from app.services.purchase_orders import PurchaseOrderService
from app.services.supplier_gateway import SupplierGateway

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running", "awaiting_approval")
# What the recommendation shows once a run ends in each status.
RECOMMENDATION_STATUS = {
    "completed": "decided",
    "escalated": "escalated",
    "awaiting_approval": "awaiting_approval",
    "failed": "open",
    "needs_review": "needs_review",
}


class RunConflictError(RuntimeError):
    """The recommendation already has a run in progress."""


@dataclass(frozen=True)
class AgentConfig:
    max_llm_calls: int = 16
    max_nudges: int = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Trace:
    """Writes the run's steps to the database as they happen, so progress can be watched."""

    def __init__(self, session: Session, run: AgentRun):
        self._session = session
        self._run = run
        self._seq = 0

    def llm_turn(self, send: Callable[[], LLMResponse], *, sent: str | None = None) -> LLMResponse:
        started = time.perf_counter()
        response = send()
        run = self._run
        run.llm_calls += 1
        run.prompt_tokens += response.usage.prompt_tokens
        run.output_tokens += response.usage.output_tokens
        self._add(
            "llm",
            None,
            {
                "sent": sent,
                "text": response.text,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "args": c.args} for c in response.tool_calls
                ],
                "usage": vars(response.usage),
                "finish_reason": response.finish_reason,
            },
            started,
        )
        self._session.commit()
        return response

    def tool(self, call: ToolCall, execute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        started = time.perf_counter()
        payload = execute()
        self._add(
            "tool", call.name, {"call_id": call.id, "args": call.args, "result": payload}, started
        )
        return payload

    def note(self, kind: str, name: str, payload: dict[str, Any]) -> None:
        self._add(kind, name, payload, None)

    def _add(self, kind: str, name: str | None, payload: dict, started: float | None) -> None:
        self._seq += 1
        self._session.add(
            AgentStep(
                run_id=self._run.id,
                seq=self._seq,
                kind=kind,
                name=name,
                payload=payload,
                duration_ms=round((time.perf_counter() - started) * 1000) if started else None,
                created_at=_now(),
            )
        )


class AgentRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        llm: LLMClient,
        *,
        today: date,
        policy: PolicyConfig = DEFAULT_POLICY,
        config: AgentConfig | None = None,
        gateway_factory: Callable[[Session], SupplierGateway] | None = None,
    ):
        self._session_factory = session_factory
        self._llm = llm
        self._today = today
        self._policy = policy
        self._config = config or AgentConfig()
        self._gateway_factory = gateway_factory

    def run(self, recommendation_id: str) -> str:
        """Create a run and execute it to the end. Returns the run id."""
        run_id = self.create_run(recommendation_id)
        self.execute(run_id)
        return run_id

    def create_run(self, recommendation_id: str) -> str:
        with self._session_factory() as session:
            rec = session.get(Recommendation, recommendation_id)
            if rec is None:
                raise LookupError(f"Recommendation {recommendation_id} not found")
            active = session.scalar(
                select(AgentRun).where(
                    AgentRun.recommendation_id == rec.id, AgentRun.status.in_(ACTIVE_STATUSES)
                )
            )
            if active is not None:
                raise RunConflictError(f"{active.id} is already {active.status} for {rec.id}")
            run = AgentRun(
                id=f"RUN-{uuid.uuid4().hex[:8].upper()}",
                recommendation_id=rec.id,
                status="queued",
                provider=self._llm.provider,
                model=self._llm.model,
                evidence=[],
                started_at=_now(),
            )
            session.add(run)
            session.commit()
            return run.id

    def execute(self, run_id: str) -> None:
        with self._session_factory() as session:
            run = session.get(AgentRun, run_id)
            rec = session.get(Recommendation, run.recommendation_id)
            gateway = self._gateway_factory(session) if self._gateway_factory else None
            ctx = RunContext(
                session=session,
                run=run,
                recommendation=rec,
                today=self._today,
                policy=self._policy,
                po_service=PurchaseOrderService(
                    session, today=self._today, gateway=gateway, policy=self._policy
                ),
            )
            trace = Trace(session, run)
            run.status = "running"
            rec.status = "in_review"
            session.commit()

            llm_session: LLMSession | None = None
            try:
                llm_session = self._llm.new_session(SYSTEM_PROMPT, tool_specs())
                kickoff = self._kickoff(session, rec)
                trace.note("system", "kickoff", {"message": kickoff})
                response = trace.llm_turn(lambda: llm_session.send_message(kickoff), sent=kickoff)
                self._loop(ctx, trace, llm_session, response)
            except LLMError as exc:
                log.warning("Run %s: LLM failed: %s", run_id, exc)
                session.rollback()
                trace.note("system", "llm_error", {"error": str(exc)})
                self._fail(run, f"LLM unavailable: {exc}")
            except Exception as exc:
                log.exception("Run %s crashed", run_id)
                session.rollback()
                self._fail(run, f"Internal error: {exc}")
            finally:
                run.evidence = sorted(ctx.evidence)
                if llm_session is not None:
                    run.history = llm_session.export_history()
                if run.status != "awaiting_approval":
                    run.finished_at = _now()
                rec.status = RECOMMENDATION_STATUS.get(run.status, rec.status)
                session.commit()

    # --- Loop ----------------------------------------------------------------------------

    def _loop(
        self, ctx: RunContext, trace: Trace, llm_session: LLMSession, response: LLMResponse
    ) -> None:
        nudges = 0
        while True:
            if not response.tool_calls:
                if nudges >= self._config.max_nudges:
                    trace.note("guardrail", "stopped_without_decision", {"text": response.text})
                    self._fail(ctx.run, "The agent stopped without calling finalize_decision.")
                    return
                nudges += 1
                trace.note("guardrail", "nudge", {"text": response.text, "nudge": NUDGE})
                response = trace.llm_turn(lambda: llm_session.send_message(NUDGE), sent=NUDGE)
                continue

            # finalize_decision runs last, so it sees the results of calls made alongside it.
            calls = sorted(response.tool_calls, key=lambda c: c.name == "finalize_decision")
            results = [
                ToolResult(
                    call.id, call.name, trace.tool(call, lambda c=call: execute_tool(ctx, c))
                )
                for call in calls
            ]
            ctx.session.commit()

            if ctx.finalized:
                self._settle(ctx, trace)
                return
            if ctx.run.llm_calls >= self._config.max_llm_calls:
                trace.note("guardrail", "llm_call_budget", {"limit": self._config.max_llm_calls})
                self._fail(
                    ctx.run,
                    f"Stopped after {self._config.max_llm_calls} model calls without a decision.",
                )
                return
            response = trace.llm_turn(lambda r=results: llm_session.send_tool_results(r))

    def _settle(self, ctx: RunContext, trace: Trace) -> None:
        """Set the run's status from what it left behind."""
        session, run = ctx.session, ctx.run
        pending = session.scalar(
            select(PurchaseOrder).where(
                PurchaseOrder.run_id == run.id, PurchaseOrder.status == "pending_approval"
            )
        )
        escalated = session.scalar(
            select(Escalation).where(Escalation.run_id == run.id, Escalation.status == "open")
        )
        if pending is not None:
            run.status = "awaiting_approval"
        elif escalated is not None or run.decision == "investigate":
            run.status = "escalated"
        else:
            run.status = "completed"
        trace.note("system", "settled", {"status": run.status, "decision": run.decision})

    @staticmethod
    def _fail(run: AgentRun, reason: str) -> None:
        run.status = "failed"
        run.error = reason

    def _kickoff(self, session: Session, rec: Recommendation) -> str:
        product = session.get(Product, rec.sku)
        node = session.get(Node, rec.node_id)
        supplier = session.get(Supplier, rec.supplier_id)
        return kickoff_message(
            recommendation_id=rec.id,
            quantity=rec.quantity,
            product=product.name,
            sku=rec.sku,
            node=node.name,
            node_id=rec.node_id,
            supplier=supplier.name,
            supplier_id=rec.supplier_id,
            today=self._today,
        )
