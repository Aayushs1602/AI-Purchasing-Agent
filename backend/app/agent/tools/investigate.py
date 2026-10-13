"""Investigation tools (read the data) and analysis tools (ask the engine).

None of them take a SKU, store or supplier: they always answer for the recommendation under
review, so the agent cannot drift onto other products (scope lock by construction).
"""

from __future__ import annotations

from statistics import fmean
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from app.agent.tools.base import NoArgs, RunContext, Tool, ToolArgs, ToolError, untrusted
from app.engine import inventory_math as im
from app.engine.models import Assumptions
from app.engine.policy import evaluate_quantity, recommendation_findings
from app.models import (
    LIVE_PO_STATUSES,
    Budget,
    Node,
    Product,
    PurchaseOrder,
    Supplier,
    SupplierTerm,
)
from app.services.context import open_purchase_orders, recommendation_inputs


def _avg(values: list[float]) -> float | None:
    return round(fmean(values), 2) if values else None


# --- Read tools --------------------------------------------------------------------------


def get_recommendation(ctx: RunContext, _: NoArgs) -> dict:
    rec = ctx.recommendation
    product = ctx.session.get(Product, rec.sku)
    node = ctx.session.get(Node, rec.node_id)
    supplier = ctx.session.get(Supplier, rec.supplier_id)
    return {
        "recommendation_id": rec.id,
        "recommended_quantity": rec.quantity,
        "product": {"sku": product.sku, "name": product.name, "category": product.category},
        "node": {"id": node.id, "name": node.name, "city": node.city},
        "supplier": {"id": supplier.id, "name": supplier.name},
        "created_on": rec.created_on.isoformat(),
        "age_days": (ctx.today - rec.created_on).days,
        "source": rec.source,
        "method": rec.method,
        "inputs_snapshot": rec.inputs,
        "today": ctx.today.isoformat(),
    }


def get_inventory(ctx: RunContext, _: NoArgs) -> dict:
    pc = ctx.purchasing_context()
    counted = pc.inventory_counted_at
    return {
        "on_hand": pc.on_hand,
        "reserved": pc.reserved,
        "available": im.available_on_hand(pc),
        "safety_stock": pc.safety_stock,
        "review_period_days": pc.review_period_days,
        "last_counted_on": counted.isoformat() if counted else None,
        "days_since_count": (ctx.today - counted).days if counted else None,
    }


def get_storage_capacity(ctx: RunContext, _: NoArgs) -> dict:
    pc = ctx.purchasing_context()
    return {
        "storage_capacity_units": pc.storage_capacity,
        "on_hand": pc.on_hand,
        "free_space_today": pc.storage_capacity - pc.on_hand,
        "note": (
            "Free space when a new order lands depends on sales and other deliveries before "
            "then. evaluate_order_quantity projects it."
        ),
    }


def get_demand_forecast(ctx: RunContext, _: NoArgs) -> dict:
    pc = ctx.purchasing_context()
    window = im.coverage_window_days(pc)
    forecast = pc.daily_forecast
    return {
        "unit": "units per day",
        "days_available": len(forecast),
        "coverage_window_days": window,
        "coverage_window_meaning": "supplier lead time + review period until the next order",
        "avg_per_day_coverage_window": _avg(forecast[:window]),
        "total_coverage_window": round(sum(forecast[:window]), 2),
        "avg_per_day_next_7": _avg(forecast[:7]),
        "next_14_days": forecast[:14],
    }


class SalesHistoryArgs(ToolArgs):
    days: int = Field(28, ge=7, le=28, description="How many recent days to return")


def get_sales_history(ctx: RunContext, args: SalesHistoryArgs) -> dict:
    pc = ctx.purchasing_context()
    sales = pc.recent_daily_sales[-args.days :]
    window = im.coverage_window_days(pc)
    forecast_avg = _avg(pc.daily_forecast[:window])
    last_14 = _avg(sales[-14:])
    return {
        "days": len(sales),
        "daily_units_oldest_first": sales,
        "avg_last_7": _avg(sales[-7:]),
        "avg_last_14": last_14,
        "avg_all_days": _avg(sales),
        "forecast_avg_per_day": forecast_avg,
        "last_14_vs_forecast_pct": (
            round((last_14 - forecast_avg) / forecast_avg * 100, 1)
            if last_14 is not None and forecast_avg
            else None
        ),
    }


def get_open_purchase_orders(ctx: RunContext, _: NoArgs) -> dict:
    rec = ctx.recommendation
    pos = open_purchase_orders(ctx.session, sku=rec.sku, node_id=rec.node_id)
    rows = []
    for po in pos:
        days_until = (po.eta - ctx.today).days if po.eta else None
        rows.append(
            {
                "po_id": po.id,
                "status": po.status,
                "supplier_id": po.supplier_id,
                "ordered_quantity": po.quantity,
                "outstanding_quantity": po.outstanding_quantity,
                "eta": po.eta.isoformat() if po.eta else None,
                "days_until_eta": days_until,
                "overdue": days_until is not None and days_until < 0,
                "note": untrusted(po.note, "supplier / PO system"),
            }
        )
    return {
        "open_purchase_orders": rows,
        "total_outstanding": sum(row["outstanding_quantity"] for row in rows),
    }


def get_supplier_terms(ctx: RunContext, _: NoArgs) -> dict:
    rec = ctx.recommendation
    supplier = ctx.session.get(Supplier, rec.supplier_id)
    terms = ctx.session.scalar(
        select(SupplierTerm).where(
            SupplierTerm.supplier_id == rec.supplier_id, SupplierTerm.sku == rec.sku
        )
    )
    if terms is None:
        raise ToolError("NO_SUPPLIER_TERMS", f"{supplier.name} has no terms for {rec.sku}")
    return {
        "supplier_id": supplier.id,
        "supplier_name": supplier.name,
        "lead_time_days": terms.lead_time_days,
        "moq": terms.moq,
        "case_pack": terms.case_pack,
        "smallest_valid_order": im.min_valid_quantity(terms.moq, terms.case_pack),
        "unit_cost": terms.unit_cost,
        "currency": "USD",
        "on_time_rate": supplier.on_time_rate,
        "fill_rate": supplier.fill_rate,
        "supplier_note": untrusted(terms.note, "supplier portal"),
    }


def get_budget(ctx: RunContext, _: NoArgs) -> dict:
    rec = ctx.recommendation
    category = ctx.session.get(Product, rec.sku).category
    period = ctx.today.strftime("%Y-%m")
    budget = ctx.session.scalar(
        select(Budget).where(
            Budget.node_id == rec.node_id, Budget.category == category, Budget.period == period
        )
    )
    if budget is None:
        raise ToolError("NO_BUDGET", f"No {period} budget for {category} at {rec.node_id}")
    return {
        "category": category,
        "period": period,
        "total": budget.total,
        "committed": budget.committed,
        "available": budget.available,
        "currency": "USD",
    }


# --- Analysis tools ----------------------------------------------------------------------


def check_recommendation_inputs(ctx: RunContext, _: NoArgs) -> dict:
    pc = ctx.purchasing_context()
    window = im.coverage_window_days(pc)
    findings = recommendation_findings(pc, recommendation_inputs(ctx.recommendation), ctx.policy)
    return {
        "inputs_snapshot": ctx.recommendation.inputs,
        "current": {
            "on_hand": pc.on_hand,
            "open_po_units": sum(po.quantity for po in pc.open_purchase_orders),
            "forecast_avg_per_day": _avg(pc.daily_forecast[:window]),
        },
        "differences": [f.model_dump() for f in findings],
    }


class EvaluateArgs(ToolArgs):
    quantity: int = Field(ge=0, le=100_000, description="Candidate order quantity in units")
    exclude_po_ids: list[str] = Field(
        default_factory=list,
        description="Open POs to treat as not arriving (e.g. on hold). Routes to human approval.",
    )
    demand_basis: Literal["forecast", "recent_actuals"] = Field(
        "forecast",
        description="Use 'recent_actuals' only if sales clearly contradict the forecast. "
        "Routes to human approval.",
    )


def assumptions_for(ctx: RunContext, exclude_po_ids: list[str], demand_basis: str) -> Assumptions:
    known = {po.po_id for po in ctx.purchasing_context().open_purchase_orders}
    unknown = sorted(set(exclude_po_ids) - known)
    if unknown:
        raise ToolError(
            "UNKNOWN_PO",
            f"Not open POs for this product: {', '.join(unknown)}",
            {"open_po_ids": sorted(known)},
        )
    return Assumptions(exclude_po_ids=exclude_po_ids, demand_basis=demand_basis)


def evaluate_order_quantity(ctx: RunContext, args: EvaluateArgs) -> dict:
    assumptions = assumptions_for(ctx, args.exclude_po_ids, args.demand_basis)
    # Once the run has its own PO, a candidate quantity means "change that PO to this",
    # not "order this on top of it": evaluate it exactly as an amend would be validated.
    own_po = ctx.session.scalar(
        select(PurchaseOrder).where(
            PurchaseOrder.run_id == ctx.run.id, PurchaseOrder.status.in_(LIVE_PO_STATUSES)
        )
    )
    if own_po is not None:
        evaluation = ctx.po_service.evaluate_replacement(
            own_po, args.quantity, assumptions=assumptions
        )
        evaluated_as = f"replacement for {own_po.id} (this run's {own_po.status} PO)"
    else:
        evaluation = evaluate_quantity(
            ctx.purchasing_context(),
            args.quantity,
            assumptions=assumptions,
            recommended_qty=ctx.recommendation.quantity,
            config=ctx.policy,
        )
        evaluated_as = "new order"
    return {"evaluated_as": evaluated_as, **evaluation.model_dump(mode="json")}


INVESTIGATION_TOOLS = [
    Tool(
        "get_recommendation",
        "The recommendation under review: product, store, supplier, quantity, how it was "
        "computed and the inputs it was based on.",
        NoArgs,
        get_recommendation,
        "read",
    ),
    Tool(
        "get_inventory",
        "Current stock: on hand, reserved, safety stock, review period, last physical count.",
        NoArgs,
        get_inventory,
        "read",
        evidence="inventory",
    ),
    Tool(
        "get_storage_capacity",
        "Storage capacity for this product at the store and today's free space.",
        NoArgs,
        get_storage_capacity,
        "read",
        evidence="storage",
    ),
    Tool(
        "get_demand_forecast",
        "Daily demand forecast, including the average over the coverage window "
        "(lead time + review period).",
        NoArgs,
        get_demand_forecast,
        "read",
        evidence="demand_forecast",
    ),
    Tool(
        "get_sales_history",
        "Actual daily sales for recent days, with averages compared to the forecast.",
        SalesHistoryArgs,
        get_sales_history,
        "read",
        evidence="sales_history",
    ),
    Tool(
        "get_open_purchase_orders",
        "Purchase orders already placed and not yet received: quantity, ETA, status, notes.",
        NoArgs,
        get_open_purchase_orders,
        "read",
        evidence="open_purchase_orders",
    ),
    Tool(
        "get_supplier_terms",
        "Supplier terms for this product: lead time, MOQ, case pack, unit cost, reliability, "
        "and any message from the supplier portal.",
        NoArgs,
        get_supplier_terms,
        "read",
        evidence="supplier_terms",
    ),
    Tool(
        "get_budget",
        "This month's purchasing budget for the product's category: total, committed, available.",
        NoArgs,
        get_budget,
        "read",
        evidence="budget",
    ),
    Tool(
        "check_recommendation_inputs",
        "Compare the data the recommendation was computed from with current data.",
        NoArgs,
        check_recommendation_inputs,
        "analysis",
    ),
    Tool(
        "evaluate_order_quantity",
        "Project stock for a candidate quantity and check it against every constraint: "
        "need, cover days, budget, storage, MOQ, case pack, approval rules. Use it for every "
        "number you rely on; never compute these yourself. Once this run has its own PO, the "
        "candidate is evaluated as a replacement for that PO (exactly what an amend would be).",
        EvaluateArgs,
        evaluate_order_quantity,
        "analysis",
    ),
]
