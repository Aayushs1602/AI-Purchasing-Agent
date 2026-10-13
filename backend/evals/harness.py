"""Run one scenario end to end on a fresh database and collect what the graders need.

The harness plays the human: every PO that reaches the approval gate is approved
("rubber-stamp approver"). So "approval" grading checks that the agent routed the PO to a
human when it should; whether a human should then have said yes is judged by the decision
and quantity labels.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select

from app.agent.llm.base import LLMClient
from app.agent.orchestrator import AgentConfig, AgentRunner
from app.db import create_schema, make_engine, make_session_factory
from app.engine.reference_policy import ReferenceDecision, decide
from app.models import AgentRun, AgentStep, Escalation, PurchaseOrder, Recommendation
from app.scenarios import Scenario
from app.seed import seed_scenarios
from app.services.context import context_for_recommendation
from app.services.purchase_orders import PurchaseOrderService
from evals.graders import Grade, POState, RunRecord, grade_baseline, grade_run

AUTO_APPROVER = "eval:auto-approver"


@dataclass
class CaseResult:
    scenario_id: str
    repeat: int
    status: str
    decision: str | None
    po_quantity: int | None
    po_status: str | None
    approvals_given: int
    llm_calls: int
    prompt_tokens: int
    output_tokens: int
    seconds: float
    error: str | None
    grade: Grade
    baseline: ReferenceDecision
    baseline_ok: bool


def _fresh_database(workdir: Path, name: str, scenarios: list[Scenario], today: date):
    engine = make_engine(f"sqlite:///{(workdir / f'{name}.db').as_posix()}")
    create_schema(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        seed_scenarios(session, scenarios, today)
    return engine, factory


def run_case(
    scenario: Scenario,
    all_scenarios: list[Scenario],
    llm: LLMClient,
    *,
    repeat: int,
    workdir: Path,
    today: date,
    max_llm_calls: int = 16,
    on_finish: Callable[[str], None] | None = None,
) -> CaseResult:
    engine, factory = _fresh_database(workdir, f"{scenario.id}-{repeat}", all_scenarios, today)
    try:
        rec_id = scenario.world.recommendation.id
        with factory() as session:
            rec = session.get(Recommendation, rec_id)
            baseline = decide(context_for_recommendation(session, rec, today), rec.quantity)

        runner = AgentRunner(
            factory, llm, today=today, config=AgentConfig(max_llm_calls=max_llm_calls)
        )
        started = time.perf_counter()
        run_id = runner.run(rec_id)
        approvals = 0
        while _status(factory, run_id) == "awaiting_approval" and approvals < 3:
            runner.resume(
                run_id,
                approve=True,
                approver=AUTO_APPROVER,
                comment="Approved by the evaluation harness",
            )
            approvals += 1
        seconds = time.perf_counter() - started
        if on_finish:
            on_finish(run_id)

        with factory() as session:
            record, run = _record(session, scenario, run_id, today)
            return CaseResult(
                scenario_id=scenario.id,
                repeat=repeat,
                status=run.status,
                decision=run.decision,
                po_quantity=record.po.outstanding_quantity if record.po else None,
                po_status=record.po.status if record.po else None,
                approvals_given=approvals,
                llm_calls=run.llm_calls,
                prompt_tokens=run.prompt_tokens,
                output_tokens=run.output_tokens,
                seconds=round(seconds, 1),
                error=run.error,
                grade=grade_run(record),
                baseline=baseline,
                baseline_ok=grade_baseline(scenario, baseline),
            )
    finally:
        engine.dispose()


def _status(factory, run_id: str) -> str:
    with factory() as session:
        return session.get(AgentRun, run_id).status


def _record(session, scenario: Scenario, run_id: str, today: date) -> tuple[RunRecord, AgentRun]:
    run = session.get(AgentRun, run_id)
    pos = session.scalars(
        select(PurchaseOrder).where(PurchaseOrder.run_id == run_id).order_by(PurchaseOrder.id)
    ).all()
    # The run's PO that matters is the one it left live; otherwise its latest one.
    po = next(
        (p for p in pos if p.status not in ("cancelled", "declined")), pos[-1] if pos else None
    )
    revalidation = None
    if po is not None:
        revalidation = PurchaseOrderService(session, today=today).revalidate(po)
    steps = session.scalars(
        select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.seq)
    ).all()
    escalations = session.scalars(select(Escalation).where(Escalation.run_id == run_id)).all()
    record = RunRecord(
        scenario=scenario,
        recommended_qty=scenario.world.recommendation.quantity,
        status=run.status,
        decision=run.decision,
        final_quantity=run.final_quantity,
        key_factors=run.key_factors or [],
        evidence=run.evidence or [],
        po=(
            POState(
                id=po.id,
                status=po.status,
                quantity=po.quantity,
                outstanding_quantity=po.outstanding_quantity,
                approval_status=po.approval_status,
            )
            if po
            else None
        ),
        revalidation=revalidation,
        escalations=len(escalations),
        steps=[{"kind": s.kind, "name": s.name, "payload": s.payload} for s in steps],
    )
    return record, run
