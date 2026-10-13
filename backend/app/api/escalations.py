"""Questions the agent (or its circuit breaker / degraded mode) handed to a human."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_app_settings, get_session
from app.config import Settings
from app.models import Escalation, Recommendation
from app.schemas import EscalationOut

router = APIRouter(prefix="/api/escalations", tags=["escalations"])

SessionDep = Annotated[Session, Depends(get_session)]


class ResolveRequest(BaseModel):
    resolved_by: str = Field(min_length=2, max_length=64, examples=["buyer:ana"])
    resolution: str = Field(min_length=3, max_length=1000, description="What was decided")


@router.get("", response_model=list[EscalationOut])
def list_escalations(
    session: SessionDep,
    status: Literal["open", "resolved"] | None = None,
    recommendation_id: str | None = None,
):
    query = select(Escalation).order_by(Escalation.id)
    if status:
        query = query.where(Escalation.status == status)
    if recommendation_id:
        query = query.where(Escalation.recommendation_id == recommendation_id)
    return session.scalars(query).all()


@router.post("/{escalation_id}/resolve", response_model=EscalationOut)
def resolve_escalation(
    escalation_id: str,
    body: ResolveRequest,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_app_settings)],
):
    escalation = session.get(Escalation, escalation_id)
    if escalation is None:
        raise HTTPException(404, f"Escalation {escalation_id} not found")
    if escalation.status != "open":
        raise HTTPException(409, f"{escalation_id} is already {escalation.status}")

    escalation.status = "resolved"
    escalation.resolved_by = body.resolved_by
    escalation.resolution = body.resolution
    escalation.resolved_on = settings.sim_today
    session.flush()

    # Once nothing is open for the recommendation, it no longer waits on a human.
    still_open = session.scalar(
        select(Escalation).where(
            Escalation.recommendation_id == escalation.recommendation_id,
            Escalation.status == "open",
        )
    )
    rec = session.get(Recommendation, escalation.recommendation_id)
    if still_open is None and rec.status in ("escalated", "needs_review"):
        rec.status = "resolved"
    session.commit()
    return escalation
