"""Constraint validation and approval policy for a candidate order.

Every finding has a severity:
- hard:          the order cannot be placed (MOQ, case pack, budget, storage); tools refuse it.
- soft:          allowed but risky (overstock, under-coverage, far above the recommendation,
                 overridden assumptions). Placing it needs human approval.
- data_quality:  the inputs look unreliable (forecast drift, overdue PO, stale stock count).
                 Any order placed on top of them needs human approval.
- info:          context worth explaining, no approval needed.

Thresholds live in `PolicyConfig`, not in the agent's prompt, so the LLM cannot talk its way
past them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, computed_field

from app.engine import inventory_math as im
from app.engine.models import Assumptions, Projection, PurchasingContext

Severity = Literal["hard", "soft", "data_quality", "info"]


class PolicyConfig(BaseModel):
    max_cover_days: int = 25
    auto_approve_max_value: float = 3000.0
    max_increase_over_recommendation: float = Field(0.20, description="Fraction above rec qty")
    max_shortfall_vs_need: float = Field(0.20, description="Fraction below computed net need")
    forecast_drift_threshold: float = Field(0.30, description="|actual - forecast| / forecast")
    stale_count_days: int = 14
    stale_recommendation_threshold: float = 0.20
    min_confidence_for_auto_submit: float = 0.70


DEFAULT_POLICY = PolicyConfig()


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str
    observed: float | None = None
    limit: float | None = None


class RecommendationInputs(BaseModel):
    """What the replenishment system saw when it produced its recommendation."""

    quantity: int
    on_hand: int | None = None
    open_po_units: int | None = None
    daily_demand_rate: float | None = None


class OrderLimits(BaseModel):
    min_valid_quantity: int = Field(description="Smallest order meeting MOQ and case pack")
    order_for_need: int = Field(
        description="Smallest valid order that covers the net need; 0 if nothing is needed"
    )
    max_affordable_quantity: int = Field(description="Largest case multiple within budget")
    max_storable_quantity: int = Field(description="Largest case multiple within storage")
    max_feasible_quantity: int = Field(description="Largest valid order within both; 0 if none")


class QuantityEvaluation(BaseModel):
    quantity: int
    assumptions: Assumptions
    projection: Projection
    limits: OrderLimits
    hard_violations: list[Finding]
    soft_warnings: list[Finding]
    data_quality_flags: list[Finding]
    info: list[Finding]
    approval_reasons: list[Finding]

    @computed_field
    @property
    def feasible(self) -> bool:
        return not self.hard_violations

    @computed_field
    @property
    def requires_approval(self) -> bool:
        return self.quantity > 0 and bool(self.approval_reasons)


# --- Checks that depend only on the data ------------------------------------------------


def data_quality_flags(
    ctx: PurchasingContext, config: PolicyConfig = DEFAULT_POLICY
) -> list[Finding]:
    flags: list[Finding] = []

    actual = im.recent_sales_rate(ctx)
    if actual is None:
        flags.append(
            Finding(
                code="NO_SALES_HISTORY",
                severity="data_quality",
                message="No recent sales to check the forecast against.",
            )
        )
    else:
        window = im.coverage_window_days(ctx)
        forecast = sum(im.demand_series(ctx, Assumptions(), window)) / window
        if forecast > 0:
            drift = (actual - forecast) / forecast
            if abs(drift) > config.forecast_drift_threshold:
                flags.append(
                    Finding(
                        code="FORECAST_DRIFT",
                        severity="data_quality",
                        message=(
                            f"Last-14-day sales average {actual:.1f}/day vs forecast "
                            f"{forecast:.1f}/day ({drift:+.0%})."
                        ),
                        observed=round(actual, 2),
                        limit=round(forecast, 2),
                    )
                )

    for po in ctx.open_purchase_orders:
        days_late = (ctx.today - po.eta).days
        if days_late > 0 and po.quantity > 0:
            flags.append(
                Finding(
                    code="OVERDUE_OPEN_PO",
                    severity="data_quality",
                    message=(
                        f"{po.po_id} ({po.quantity} units, status '{po.status}') was due "
                        f"{days_late} day(s) ago and has not been received."
                    ),
                    observed=days_late,
                    limit=0,
                )
            )

    if ctx.inventory_counted_at is None:
        flags.append(
            Finding(
                code="STALE_INVENTORY_COUNT",
                severity="data_quality",
                message="On-hand stock has never been physically counted.",
            )
        )
    else:
        age = (ctx.today - ctx.inventory_counted_at).days
        if age > config.stale_count_days:
            flags.append(
                Finding(
                    code="STALE_INVENTORY_COUNT",
                    severity="data_quality",
                    message=f"On-hand stock was last counted {age} days ago.",
                    observed=age,
                    limit=config.stale_count_days,
                )
            )
    return flags


def recommendation_findings(
    ctx: PurchasingContext,
    recommendation: RecommendationInputs,
    config: PolicyConfig = DEFAULT_POLICY,
) -> list[Finding]:
    """Where the inputs behind the recommendation differ from current data."""
    findings: list[Finding] = []
    threshold = config.stale_recommendation_threshold

    if recommendation.on_hand is not None:
        base = max(recommendation.on_hand, 1)
        if abs(ctx.on_hand - recommendation.on_hand) / base > threshold:
            findings.append(
                Finding(
                    code="REC_ON_HAND_CHANGED",
                    severity="info",
                    message=(
                        f"Recommendation assumed {recommendation.on_hand} on hand; "
                        f"current on hand is {ctx.on_hand}."
                    ),
                    observed=ctx.on_hand,
                    limit=recommendation.on_hand,
                )
            )

    current_on_order = sum(po.quantity for po in ctx.open_purchase_orders)
    if (
        recommendation.open_po_units is not None
        and current_on_order != recommendation.open_po_units
    ):
        findings.append(
            Finding(
                code="REC_OPEN_POS_CHANGED",
                severity="info",
                message=(
                    f"Recommendation assumed {recommendation.open_po_units} units on order; "
                    f"open POs now total {current_on_order}."
                ),
                observed=current_on_order,
                limit=recommendation.open_po_units,
            )
        )

    if recommendation.daily_demand_rate:
        current = sum(im.demand_series(ctx, Assumptions(), im.coverage_window_days(ctx)))
        current_rate = current / im.coverage_window_days(ctx)
        change = (
            current_rate - recommendation.daily_demand_rate
        ) / recommendation.daily_demand_rate
        if abs(change) > threshold:
            findings.append(
                Finding(
                    code="REC_DEMAND_CHANGED",
                    severity="info",
                    message=(
                        f"Recommendation assumed {recommendation.daily_demand_rate:.1f}/day; "
                        f"current forecast is {current_rate:.1f}/day."
                    ),
                    observed=round(current_rate, 2),
                    limit=recommendation.daily_demand_rate,
                )
            )
    return findings


def order_limits(ctx: PurchasingContext, assumptions: Assumptions | None = None) -> OrderLimits:
    assumptions = assumptions or Assumptions()
    affordable = im.max_affordable_quantity(ctx)
    storable = im.max_storable_quantity(ctx, assumptions)
    supplier = ctx.supplier
    return OrderLimits(
        min_valid_quantity=im.min_valid_quantity(supplier.moq, supplier.case_pack),
        order_for_need=im.round_up_to_valid(
            im.net_need(ctx, assumptions), supplier.moq, supplier.case_pack
        ),
        max_affordable_quantity=affordable,
        max_storable_quantity=storable,
        max_feasible_quantity=im.round_down_to_valid(
            min(affordable, storable), supplier.moq, supplier.case_pack
        ),
    )


# --- Checks for a specific quantity ------------------------------------------------------


def _hard_violations(
    ctx: PurchasingContext, quantity: int, projection: Projection
) -> list[Finding]:
    supplier = ctx.supplier
    if quantity <= 0:
        return [
            Finding(
                code="QTY_NOT_POSITIVE",
                severity="hard",
                message="A purchase order needs a positive quantity.",
                observed=quantity,
                limit=1,
            )
        ]

    violations: list[Finding] = []
    if quantity < supplier.moq:
        violations.append(
            Finding(
                code="BELOW_MOQ",
                severity="hard",
                message=f"{quantity} units is below the supplier MOQ of {supplier.moq}.",
                observed=quantity,
                limit=supplier.moq,
            )
        )
    if quantity % supplier.case_pack:
        violations.append(
            Finding(
                code="NOT_CASE_MULTIPLE",
                severity="hard",
                message=f"{quantity} units is not a multiple of case pack {supplier.case_pack}.",
                observed=quantity,
                limit=supplier.case_pack,
            )
        )
    if projection.order_value > ctx.budget_available + 1e-6:
        violations.append(
            Finding(
                code="OVER_BUDGET",
                severity="hard",
                message=(
                    f"Order value ${projection.order_value:,.2f} exceeds available budget "
                    f"${ctx.budget_available:,.2f}."
                ),
                observed=projection.order_value,
                limit=ctx.budget_available,
            )
        )
    if projection.peak_on_hand_after_arrival > ctx.storage_capacity:
        violations.append(
            Finding(
                code="OVER_STORAGE_CAPACITY",
                severity="hard",
                message=(
                    f"Projected stock peaks at {projection.peak_on_hand_after_arrival} units, "
                    f"above storage capacity of {ctx.storage_capacity}."
                ),
                observed=projection.peak_on_hand_after_arrival,
                limit=ctx.storage_capacity,
            )
        )
    return violations


def _soft_warnings(
    ctx: PurchasingContext,
    quantity: int,
    projection: Projection,
    assumptions: Assumptions,
    recommended_qty: int | None,
    config: PolicyConfig,
) -> list[Finding]:
    warnings: list[Finding] = []
    need = projection.net_need

    if quantity > 0 and projection.cover_days_after_receipt > config.max_cover_days:
        warnings.append(
            Finding(
                code="OVERSTOCK",
                severity="soft",
                message=(
                    f"Stock after receipt covers {projection.cover_days_after_receipt} days, "
                    f"above the {config.max_cover_days}-day maximum."
                ),
                observed=projection.cover_days_after_receipt,
                limit=config.max_cover_days,
            )
        )
    if quantity > 0 and need == 0:
        warnings.append(
            Finding(
                code="NO_NET_NEED",
                severity="soft",
                message="Inventory position already meets the order-up-to level.",
                observed=projection.inventory_position,
                limit=projection.order_up_to_level,
            )
        )
    min_acceptable = need * (1 - config.max_shortfall_vs_need)
    if need > 0 and quantity < min_acceptable:
        warnings.append(
            Finding(
                code="UNDER_COVERAGE",
                severity="soft",
                message=(
                    f"{quantity} units is more than {config.max_shortfall_vs_need:.0%} below "
                    f"the computed need of {need}."
                ),
                observed=quantity,
                limit=round(min_acceptable, 2),
            )
        )
    if recommended_qty:
        max_without_approval = recommended_qty * (1 + config.max_increase_over_recommendation)
        if quantity > max_without_approval:
            warnings.append(
                Finding(
                    code="ABOVE_RECOMMENDATION",
                    severity="soft",
                    message=(
                        f"{quantity} units is more than "
                        f"{config.max_increase_over_recommendation:.0%} above the recommended "
                        f"{recommended_qty}."
                    ),
                    observed=quantity,
                    limit=round(max_without_approval, 2),
                )
            )
    if not assumptions.is_default():
        parts = []
        if assumptions.exclude_po_ids:
            parts.append(f"excludes open PO(s) {', '.join(assumptions.exclude_po_ids)}")
        if assumptions.demand_basis != "forecast":
            parts.append(f"uses {assumptions.demand_basis} instead of the forecast")
        warnings.append(
            Finding(
                code="ASSUMPTION_OVERRIDE",
                severity="soft",
                message="Projection " + " and ".join(parts) + ".",
            )
        )
    return warnings


def _info(projection: Projection) -> list[Finding]:
    stockout = projection.stockout_day_without_order
    if stockout is not None and stockout < projection.arrival_day:
        return [
            Finding(
                code="STOCKOUT_BEFORE_ARRIVAL",
                severity="info",
                message=(
                    f"Stock runs out on day {stockout}, before any new order can arrive "
                    f"on day {projection.arrival_day}."
                ),
                observed=stockout,
                limit=projection.arrival_day,
            )
        ]
    return []


def evaluate_quantity(
    ctx: PurchasingContext,
    quantity: int,
    *,
    assumptions: Assumptions | None = None,
    recommended_qty: int | None = None,
    config: PolicyConfig = DEFAULT_POLICY,
) -> QuantityEvaluation:
    """Project `quantity` and check it against every constraint and approval rule."""
    assumptions = assumptions or Assumptions()
    projection = im.project(ctx, quantity, assumptions)

    hard = _hard_violations(ctx, quantity, projection)
    soft = _soft_warnings(ctx, quantity, projection, assumptions, recommended_qty, config)
    data_quality = data_quality_flags(ctx, config)

    approval: list[Finding] = []
    if projection.order_value > config.auto_approve_max_value:
        approval.append(
            Finding(
                code="VALUE_ABOVE_AUTO_APPROVE_LIMIT",
                severity="soft",
                message=(
                    f"Order value ${projection.order_value:,.2f} is above the "
                    f"${config.auto_approve_max_value:,.2f} auto-approve limit."
                ),
                observed=projection.order_value,
                limit=config.auto_approve_max_value,
            )
        )
    approval.extend(soft)
    approval.extend(data_quality)

    return QuantityEvaluation(
        quantity=quantity,
        assumptions=assumptions,
        projection=projection,
        limits=order_limits(ctx, assumptions),
        hard_violations=hard,
        soft_warnings=soft,
        data_quality_flags=data_quality,
        info=_info(projection),
        approval_reasons=approval,
    )


def submission_approval_reasons(
    evaluation: QuantityEvaluation,
    confidence: float | None,
    config: PolicyConfig = DEFAULT_POLICY,
) -> list[Finding]:
    """Approval reasons at submit time, adding the agent's own stated confidence."""
    reasons = list(evaluation.approval_reasons)
    if confidence is not None and confidence < config.min_confidence_for_auto_submit:
        reasons.append(
            Finding(
                code="LOW_CONFIDENCE",
                severity="soft",
                message=(
                    f"Agent confidence {confidence:.2f} is below "
                    f"{config.min_confidence_for_auto_submit:.2f}."
                ),
                observed=confidence,
                limit=config.min_confidence_for_auto_submit,
            )
        )
    return reasons
