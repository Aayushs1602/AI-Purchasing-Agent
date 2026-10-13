"""Runs the agent: the tool-use loop around the LLM, with limits, pauses and a full trace.

    kickoff ─► model turn ─► execute tool calls ─► results back to the model ─► ...
                  │                │                    └─ until finalize_decision succeeds
                  │                └─ submit needs approval: pause; resume when a human decides
                  └─ no tool call: nudge once, then stop the run as failed

Guardrails owned by the loop (not the model): a budget of model calls per run, one nudge
when the model stops calling tools, LLM failures ending the run cleanly, and every turn,
tool call and intervention written to `agent_steps` as it happens.

Approval pause: when a submit needs a human, its result is held back from the model. The run
stores the model's turn, the other results of that turn and its loop state, and waits in
`awaiting_approval`. `resume()` applies the human's decision, sends it to the model as the
result of that same tool call, and the loop continues.

Degraded mode: if the LLM fails, the run does not just die. Code computes the rules-only
reference decision and hands it to a human as a proposal (escalation, status needs_review).
It never acts on that proposal itself.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.llm.base import LLMClient, LLMError, LLMResponse, LLMSession, ToolCall, ToolResult
from app.agent.prompts import NUDGE, SYSTEM_PROMPT, kickoff_message
from app.agent.tools import RunContext, execute_tool, tool_specs
from app.agent.tools.actions import approval_result, open_escalation
from app.agent.tools.base import error
from app.engine.policy import DEFAULT_POLICY, PolicyConfig
from app.engine.reference_policy import decide
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


class RunNotPausedError(RuntimeError):
    """resume() was called on a run that is not waiting for approval."""


@dataclass(frozen=True)
class AgentConfig:
    max_llm_calls: int = 16
    max_nudges: int = 1
    # Failed attempts to fix a supplier deviation before code escalates (circuit breaker).
    max_repair_attempts: int = 2
    # When the LLM fails, hand a rules-only proposal to a human instead of just failing.
    fallback_to_rules: bool = True


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Trace:
    """Writes the run's steps to the database as they happen, so progress can be watched."""

    def __init__(self, session: Session, run: AgentRun):
        self._session = session
        self._run = run
        # A resumed run continues numbering where it paused.
        self._seq = session.scalar(
            select(func.coalesce(func.max(AgentStep.seq), 0)).where(AgentStep.run_id == run.id)
        )

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


# A function that sends the first message of a drive (kickoff or resumed results).
Starter = Callable[[LLMSession, RunContext, Trace], LLMResponse]


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
        """Create a run and execute it until it ends or pauses. Returns the run id."""
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
        def start(llm_session: LLMSession, ctx: RunContext, trace: Trace) -> LLMResponse:
            kickoff = self._kickoff(ctx.session, ctx.recommendation)
            trace.note("system", "kickoff", {"message": kickoff})
            return trace.llm_turn(lambda: llm_session.send_message(kickoff), sent=kickoff)

        with self._session_factory() as session:
            self._drive(session, session.get(AgentRun, run_id), start, history=None)

    def resume(
        self, run_id: str, *, approve: bool, approver: str, comment: str | None = None
    ) -> None:
        """Apply a human's approval decision to a paused run and let the agent continue."""
        with self._session_factory() as session:
            run = session.get(AgentRun, run_id)
            if run is None or run.status != "awaiting_approval" or not run.paused_state:
                raise RunNotPausedError(f"Run {run_id} is not waiting for approval")
            paused = dict(run.paused_state)

            def start(llm_session: LLMSession, ctx: RunContext, trace: Trace) -> LLMResponse:
                held = ToolCall(paused["call_id"], "submit_purchase_order", {"resumed": True})
                payload = trace.tool(
                    held,
                    lambda: approval_result(
                        ctx,
                        po_id=paused["po_id"],
                        expectation=paused["expectation"],
                        approve=approve,
                        approver=approver,
                        comment=comment,
                    ),
                )
                trace.note(
                    "system",
                    "resumed",
                    {"approved": approve, "approver": approver, "comment": comment},
                )
                results = [
                    ToolResult(
                        r["call_id"],
                        r["name"],
                        payload if r["call_id"] == paused["call_id"] else r["payload"],
                    )
                    for r in paused["results"]
                ]
                ctx.run.paused_state = None
                return trace.llm_turn(lambda: llm_session.send_tool_results(results))

            self._drive(session, run, start, history=run.history)

    # --- Driving the loop ----------------------------------------------------------------

    def _drive(
        self,
        session: Session,
        run: AgentRun,
        start: Starter,
        *,
        history: list[dict[str, Any]] | None,
    ) -> None:
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
            max_repairs=self._config.max_repair_attempts,
        )
        ctx.restore_state(run.agent_state, run.evidence)
        trace = Trace(session, run)
        run.status = "running"
        rec.status = "in_review"
        session.commit()

        llm_session: LLMSession | None = None
        try:
            llm_session = self._llm.new_session(SYSTEM_PROMPT, tool_specs(), history=history)
            self._loop(ctx, trace, llm_session, start(llm_session, ctx, trace))
        except LLMError as exc:
            log.warning("Run %s: LLM failed: %s", run.id, exc)
            session.rollback()
            trace.note("system", "llm_error", {"error": str(exc)})
            self._fail(run, f"LLM unavailable: {exc}")
            if self._config.fallback_to_rules:
                self._fall_back_to_rules(ctx, trace)
        except Exception as exc:
            log.exception("Run %s crashed", run.id)
            session.rollback()
            self._fail(run, f"Internal error: {exc}")
        finally:
            run.evidence = sorted(ctx.evidence)
            run.agent_state = ctx.to_state()
            if llm_session is not None:
                run.history = llm_session.export_history()
            if run.status != "awaiting_approval":
                run.finished_at = _now()
            rec.status = RECOMMENDATION_STATUS.get(run.status, rec.status)
            session.commit()

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

            results, held = self._execute_turn(ctx, trace, response.tool_calls)
            ctx.session.commit()

            if held is not None:
                self._pause(ctx, trace, held, results)
                return
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

    def _execute_turn(
        self, ctx: RunContext, trace: Trace, tool_calls: list[ToolCall]
    ) -> tuple[list[ToolResult], tuple[ToolCall, dict] | None]:
        """Run one turn's calls. Returns the results and the submit held for approval, if any."""
        # finalize_decision runs last, so it sees the results of calls made alongside it.
        calls = sorted(tool_calls, key=lambda c: c.name == "finalize_decision")
        results: list[ToolResult] = []
        held: tuple[ToolCall, dict] | None = None
        for call in calls:
            if call.name == "finalize_decision" and held is not None:
                deferred = error(
                    "FINALIZE_DEFERRED",
                    "A PO from this turn is waiting for human approval. You will receive the "
                    "approval result first; finalize after that.",
                )
                payload = trace.tool(call, lambda d=deferred: d)
            else:
                payload = trace.tool(call, lambda c=call: execute_tool(ctx, c))
            if (
                held is None
                and call.name == "submit_purchase_order"
                and payload.get("ok")
                and payload["data"].get("outcome") == "PENDING_APPROVAL"
            ):
                held = (call, payload)
            results.append(ToolResult(call.id, call.name, payload))
        return results, held

    def _pause(
        self,
        ctx: RunContext,
        trace: Trace,
        held: tuple[ToolCall, dict],
        results: list[ToolResult],
    ) -> None:
        call, payload = held
        po = payload["data"]["purchase_order"]
        ctx.run.status = "awaiting_approval"
        ctx.run.paused_state = {
            "call_id": call.id,
            "po_id": po["po_id"],
            "expectation": call.args.get("expectation"),
            "results": [
                {"call_id": r.call_id, "name": r.name, "payload": r.payload} for r in results
            ],
        }
        trace.note(
            "system",
            "paused_for_approval",
            {"po_id": po["po_id"], "reasons": [r["code"] for r in po["approval_reasons"]]},
        )

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
        if ctx.review_issues:
            run.status = "needs_review"
            run.validation_issues = ctx.review_issues
        elif pending is not None:
            run.status = "awaiting_approval"
        elif escalated is not None or run.decision == "investigate":
            run.status = "escalated"
        else:
            run.status = "completed"
        trace.note("system", "settled", {"status": run.status, "decision": run.decision})

    def _fall_back_to_rules(self, ctx: RunContext, trace: Trace) -> None:
        """Degraded mode: propose the rules-only decision to a human; take no action."""
        try:
            ref = decide(ctx.purchasing_context(), ctx.recommendation.quantity, self._policy)
            placed = ctx.session.scalar(
                select(PurchaseOrder).where(PurchaseOrder.run_id == ctx.run.id)
            )
            proposal = {
                "source": "rules-only reference policy",
                "decision": ref.decision,
                "quantity": ref.quantity,
                "requires_approval": ref.requires_approval,
                "reasons": ref.reasons,
            }
            where = f" {placed.id} from this run is {placed.status}." if placed else ""
            escalation = open_escalation(
                ctx,
                severity="high",
                reason=(
                    f"[degraded mode] The agent could not finish: {ctx.run.error}. Rules-only "
                    f"proposal: {ref.decision} {ref.quantity} units. {' '.join(ref.reasons)}{where}"
                ),
                questions=["Confirm, change or reject the rules-only proposal."],
                options=[
                    f"Apply the proposal ({ref.decision} {ref.quantity} units)",
                    "Re-run the agent when the LLM is available",
                    "Decide manually",
                ],
            )
        except Exception:
            log.exception("Run %s: rules fallback failed", ctx.run.id)
            return
        ctx.run.status = "needs_review"
        ctx.run.fallback_proposal = {**proposal, "escalation_id": escalation.id}
        trace.note("system", "degraded_mode", ctx.run.fallback_proposal)

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
