"""Read-only purchase order listing."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.models import PurchaseOrder
from app.schemas import PurchaseOrderOut

router = APIRouter(prefix="/api/purchase-orders", tags=["purchase orders"])


@router.get("", response_model=list[PurchaseOrderOut])
def list_purchase_orders(
    session: Annotated[Session, Depends(get_session)],
    sku: str | None = None,
    node_id: str | None = None,
    status: str | None = None,
):
    query = select(PurchaseOrder).order_by(PurchaseOrder.id)
    if sku:
        query = query.where(PurchaseOrder.sku == sku)
    if node_id:
        query = query.where(PurchaseOrder.node_id == node_id)
    if status:
        query = query.where(PurchaseOrder.status == status)
    return session.scalars(query).all()
