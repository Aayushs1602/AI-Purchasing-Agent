"""Build the engine's `PurchasingContext` snapshot from the database."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.models import OpenPurchaseOrder, PurchasingContext, SupplierTerms
from app.engine.policy import RecommendationInputs
from app.models import (
    OPEN_PO_STATUSES,
    Budget,
    DemandForecast,
    InventoryRecord,
    Product,
    PurchaseOrder,
    Recommendation,
    SalesRecord,
    Supplier,
    SupplierTerm,
)


class ContextNotFoundError(LookupError):
    """A record the engine needs is missing (e.g. no inventory row or supplier terms)."""


def build_context(
    session: Session, *, sku: str, node_id: str, supplier_id: str, today: date
) -> PurchasingContext:
    product = session.get(Product, sku)
    inventory = session.get(InventoryRecord, (sku, node_id))
    supplier = session.get(Supplier, supplier_id)
    terms = session.scalar(
        select(SupplierTerm).where(SupplierTerm.supplier_id == supplier_id, SupplierTerm.sku == sku)
    )
    missing = [
        name
        for name, row in (
            ("product", product),
            ("inventory", inventory),
            ("supplier", supplier),
            ("supplier terms", terms),
        )
        if row is None
    ]
    if missing:
        raise ContextNotFoundError(f"No {', '.join(missing)} for {sku} at {node_id}")

    budget = session.scalar(
        select(Budget).where(
            Budget.node_id == node_id,
            Budget.category == product.category,
            Budget.period == today.strftime("%Y-%m"),
        )
    )
    forecast = session.scalars(
        select(DemandForecast.units)
        .where(DemandForecast.sku == sku, DemandForecast.node_id == node_id)
        .where(DemandForecast.day >= today)
        .order_by(DemandForecast.day)
    ).all()
    sales = session.scalars(
        select(SalesRecord.units)
        .where(SalesRecord.sku == sku, SalesRecord.node_id == node_id)
        .where(SalesRecord.day < today)
        .order_by(SalesRecord.day)
    ).all()

    return PurchasingContext(
        sku=sku,
        node_id=node_id,
        today=today,
        on_hand=inventory.on_hand,
        reserved=inventory.reserved,
        safety_stock=inventory.safety_stock,
        inventory_counted_at=inventory.counted_on,
        review_period_days=inventory.review_period_days,
        daily_forecast=list(forecast),
        recent_daily_sales=[float(units) for units in sales],
        open_purchase_orders=[
            OpenPurchaseOrder(
                po_id=po.id,
                supplier_id=po.supplier_id,
                quantity=po.quantity,
                eta=po.eta,
                status=po.status,
                note=po.note,
            )
            for po in open_purchase_orders(session, sku=sku, node_id=node_id)
        ],
        supplier=SupplierTerms(
            supplier_id=supplier.id,
            supplier_name=supplier.name,
            lead_time_days=terms.lead_time_days,
            moq=terms.moq,
            case_pack=terms.case_pack,
            unit_cost=terms.unit_cost,
        ),
        budget_available=budget.available if budget else 0.0,
        storage_capacity=inventory.storage_capacity,
    )


def open_purchase_orders(session: Session, *, sku: str, node_id: str) -> list[PurchaseOrder]:
    return list(
        session.scalars(
            select(PurchaseOrder)
            .where(PurchaseOrder.sku == sku, PurchaseOrder.node_id == node_id)
            .where(PurchaseOrder.status.in_(OPEN_PO_STATUSES))
            .order_by(PurchaseOrder.eta)
        )
    )


def context_for_recommendation(
    session: Session, recommendation: Recommendation, today: date
) -> PurchasingContext:
    return build_context(
        session,
        sku=recommendation.sku,
        node_id=recommendation.node_id,
        supplier_id=recommendation.supplier_id,
        today=today,
    )


def recommendation_inputs(recommendation: Recommendation) -> RecommendationInputs:
    inputs = recommendation.inputs or {}
    return RecommendationInputs(
        quantity=recommendation.quantity,
        on_hand=inputs.get("on_hand"),
        open_po_units=inputs.get("open_po_units"),
        daily_demand_rate=inputs.get("daily_demand_rate"),
    )
