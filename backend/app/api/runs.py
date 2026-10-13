"""Start agent runs and watch them progress.

Runs execute in a background worker; the UI polls GET /api/runs/{id}, passing `steps_after`
to fetch only the steps it has not seen yet.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.orchestrator import AgentRunner, RunConflictError
from app.api.deps import get_session
from app.background import run_in_background
from app.models import AgentRun, AgentStep, Escalation, PurchaseOrder
from app.schemas import (
    EscalationOut,
    PausedFor,
    PurchaseOrderOut,
    RunDetail,
    RunStarted,
    RunSummary,
    StepOut,
)

router = APIRouter(tags=["agent runs"])

SessionDep = Annotated[Session, Depends(get_session)]


def get_runner(request: Request) -> AgentRunner:
    runner = request.app.state.runner
    if runner is None:
        raise HTTPException(503, f"Agent runs are disabled: {request.app.state.llm_error}")
    return runner


RunnerDep = Annotated[AgentRunner, Depends(get_runner)]


@router.post(
    "/api/recommendations/{recommendation_id}/runs", status_code=202, response_model=RunStarted
)
def start_run(recommendation_id: str, request: Request, runner: RunnerDep):
    try:
        run_id = runner.create_run(recommendation_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    run_in_background(request.app.state.executor, runner.execute, run_id)
    return RunStarted(run_id=run_id, status="queued")


@router.get("/api/runs", response_model=list[RunSummary])
def list_runs(
    session: SessionDep,
    recommendation_id: str | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    query = select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)
    if recommendation_id:
        query = query.where(AgentRun.recommendation_id == recommendation_id)
    if status:
        query = query.where(AgentRun.status == status)
    return session.scalars(query).all()


@router.get("/api/runs/{run_id}", response_model=RunDetail)
def get_run(
    run_id: str,
    session: SessionDep,
    steps_after: Annotated[int, Query(ge=0, description="Only steps with a higher seq")] = 0,
):
    run = session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(404, f"Run {run_id} not found")

    pos = session.scalars(
        select(PurchaseOrder).where(PurchaseOrder.run_id == run_id).order_by(PurchaseOrder.id)
    ).all()
    escalations = session.scalars(
        select(Escalation).where(Escalation.run_id == run_id).order_by(Escalation.id)
    ).all()
    steps = session.scalars(
        select(AgentStep)
        .where(AgentStep.run_id == run_id, AgentStep.seq > steps_after)
        .order_by(AgentStep.seq)
    ).all()

    paused_for = None
    if run.paused_state:
        po = session.get(PurchaseOrder, run.paused_state["po_id"])
        paused_for = PausedFor(po_id=po.id, reasons=po.approval_reasons or [])

    return RunDetail(
        **RunSummary.model_validate(run).model_dump(),
        rationale=run.rationale,
        key_factors=run.key_factors,
        risks=run.risks,
        evidence=run.evidence or [],
        error=run.error,
        validation_issues=run.validation_issues,
        fallback_proposal=run.fallback_proposal,
        paused_for=paused_for,
        purchase_orders=[PurchaseOrderOut.model_validate(po) for po in pos],
        escalations=[EscalationOut.model_validate(e) for e in escalations],
        steps=[StepOut.model_validate(step) for step in steps],
    )
