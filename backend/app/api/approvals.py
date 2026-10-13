"""Human approval of purchase orders the policy would not let the agent send on its own.

A PO held by a paused agent run is resumed in the background, so the agent sees the decision
(and the supplier's answer) and finishes its work. Any other pending PO is decided directly.
There is no login in this demo: anyone who can reach the API can approve (see README).
"""

import threading
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_app_settings, get_session
from app.background import run_in_background
from app.config import Settings
from app.models import AgentRun, Product, PurchaseOrder
from app.schemas import PurchaseOrderOut
from app.services.purchase_orders import (
    HardConstraintError,
    PurchaseOrderError,
    PurchaseOrderService,
    SupplierUnavailableError,
)

router = APIRouter(tags=["approvals"])

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]

# Runs a resume was requested for and has not finished: stops a double click resuming twice.
_resuming: set[str] = set()
_resuming_lock = threading.Lock()


class ApprovalItem(BaseModel):
    purchase_order: PurchaseOrderOut
    product_name: str
    reasons: list[dict]
    run_id: str | None
    run_status: str | None


class ApproveRequest(BaseModel):
    approver: str = Field(min_length=2, max_length=64, examples=["buyer:ana"])
    comment: str | None = Field(None, max_length=500)


class DeclineRequest(BaseModel):
    approver: str = Field(min_length=2, max_length=64, examples=["buyer:ana"])
    comment: str = Field(min_length=3, max_length=500, description="Why, for the agent and log")


class ApprovalOutcome(BaseModel):
    po_id: str
    handled_by: Literal["agent_run", "direct"]
    run_id: str | None
    po_status: str


@router.get("/api/approvals", response_model=list[ApprovalItem])
def pending_approvals(session: SessionDep):
    pos = session.scalars(
        select(PurchaseOrder)
        .where(PurchaseOrder.status == "pending_approval")
        .order_by(PurchaseOrder.id)
    ).all()
    items = []
    for po in pos:
        run = session.get(AgentRun, po.run_id) if po.run_id else None
        items.append(
            ApprovalItem(
                purchase_order=PurchaseOrderOut.model_validate(po),
                product_name=session.get(Product, po.sku).name,
                reasons=po.approval_reasons or [],
                run_id=run.id if run else None,
                run_status=run.status if run else None,
            )
        )
    return items


@router.post("/api/purchase-orders/{po_id}/approve", response_model=ApprovalOutcome)
def approve(
    po_id: str, body: ApproveRequest, request: Request, session: SessionDep, settings: SettingsDep
):
    return _decide(po_id, request, session, settings, True, body.approver, body.comment)


@router.post("/api/purchase-orders/{po_id}/decline", response_model=ApprovalOutcome)
def decline(
    po_id: str, body: DeclineRequest, request: Request, session: SessionDep, settings: SettingsDep
):
    return _decide(po_id, request, session, settings, False, body.approver, body.comment)


def _decide(
    po_id: str,
    request: Request,
    session: Session,
    settings: Settings,
    approve: bool,
    approver: str,
    comment: str | None,
) -> ApprovalOutcome:
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(404, f"Purchase order {po_id} not found")
    if po.status != "pending_approval":
        raise HTTPException(409, f"{po_id} is {po.status}, not waiting for approval")

    run = session.get(AgentRun, po.run_id) if po.run_id else None
    if (
        run is not None
        and run.status == "awaiting_approval"
        and (run.paused_state or {}).get("po_id") == po.id
    ):
        return _resume_run(request, run.id, po, approve, approver, comment)

    service = PurchaseOrderService(session, today=settings.sim_today)
    try:
        if approve:
            result = service.approve(po.id, approver=approver, comment=comment)
        else:
            result = service.decline(po.id, approver=approver, comment=comment or "")
    except HardConstraintError as exc:
        raise HTTPException(409, exc.message) from exc
    except SupplierUnavailableError as exc:
        raise HTTPException(502, exc.message) from exc
    except PurchaseOrderError as exc:
        raise HTTPException(400, exc.message) from exc
    return ApprovalOutcome(
        po_id=po.id, handled_by="direct", run_id=po.run_id, po_status=result.po.status
    )


def _resume_run(
    request: Request,
    run_id: str,
    po: PurchaseOrder,
    approve: bool,
    approver: str,
    comment: str | None,
) -> ApprovalOutcome:
    runner = request.app.state.runner
    if runner is None:
        raise HTTPException(
            503,
            f"Cannot resume the agent run: agent runs are disabled ({request.app.state.llm_error})",
        )
    with _resuming_lock:
        if run_id in _resuming:
            raise HTTPException(409, f"{run_id} is already being resumed")
        _resuming.add(run_id)

    def resume() -> None:
        try:
            runner.resume(run_id, approve=approve, approver=approver, comment=comment)
        finally:
            with _resuming_lock:
                _resuming.discard(run_id)

    run_in_background(request.app.state.executor, resume)
    return ApprovalOutcome(
        po_id=po.id, handled_by="agent_run", run_id=run_id, po_status="pending_approval"
    )
