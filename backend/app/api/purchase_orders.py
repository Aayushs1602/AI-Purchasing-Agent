"""Read-only purchase order views, including each PO's event log."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.models import PurchaseOrder, PurchaseOrderEvent
from app.schemas import PurchaseOrderDetail, PurchaseOrderEventOut, PurchaseOrderOut

router = APIRouter(prefix="/api/purchase-orders", tags=["purchase orders"])

SessionDep = Annotated[Session, Depends(get_session)]


@router.get("", response_model=list[PurchaseOrderOut])
def list_purchase_orders(
    session: SessionDep,
    sku: str | None = None,
    node_id: str | None = None,
    status: str | None = None,
    recommendation_id: str | None = None,
):
    query = select(PurchaseOrder).order_by(PurchaseOrder.id)
    if sku:
        query = query.where(PurchaseOrder.sku == sku)
    if node_id:
        query = query.where(PurchaseOrder.node_id == node_id)
    if status:
        query = query.where(PurchaseOrder.status == status)
    if recommendation_id:
        query = query.where(PurchaseOrder.recommendation_id == recommendation_id)
    return session.scalars(query).all()


@router.get("/{po_id}", response_model=PurchaseOrderDetail)
def get_purchase_order(po_id: str, session: SessionDep):
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(404, f"Purchase order {po_id} not found")
    events = session.scalars(
        select(PurchaseOrderEvent)
        .where(PurchaseOrderEvent.po_id == po_id)
        .order_by(PurchaseOrderEvent.id)
    ).all()
    return PurchaseOrderDetail(
        **PurchaseOrderOut.model_validate(po).model_dump(),
        events=[PurchaseOrderEventOut.model_validate(event) for event in events],
    )
