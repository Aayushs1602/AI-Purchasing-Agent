"""Inventory arithmetic: demand, inventory position, stock projection and order rounding.

Model: periodic review, order-up-to. An order placed today arrives after the supplier lead
time and must cover demand until the *next* order can arrive (lead time + review period),
plus safety stock. Unmet demand is lost (quick commerce: customers buy elsewhere), not
backordered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from statistics import fmean

from app.engine.models import Assumptions, OpenPurchaseOrder, Projection, PurchasingContext

SIMULATION_DAYS = 120
RECENT_SALES_WINDOW_DAYS = 14
_EPS = 1e-9


# --- Demand -----------------------------------------------------------------------------


def coverage_window_days(ctx: PurchasingContext) -> int:
    return ctx.supplier.lead_time_days + ctx.review_period_days


def recent_sales_rate(
    ctx: PurchasingContext, window: int = RECENT_SALES_WINDOW_DAYS
) -> float | None:
    sales = ctx.recent_daily_sales[-window:]
    return fmean(sales) if sales else None


def demand_series(
    ctx: PurchasingContext, assumptions: Assumptions, days: int = SIMULATION_DAYS
) -> list[float]:
    """Expected demand for each day from today, under the chosen demand basis."""
    if assumptions.demand_basis == "recent_actuals":
        rate = recent_sales_rate(ctx)
        if rate is None:
            raise ValueError("demand_basis 'recent_actuals' needs sales history")
        return [rate] * days

    forecast = ctx.daily_forecast
    if not forecast:
        raise ValueError("demand_basis 'forecast' needs a forecast")
    # Past the forecast horizon, assume demand continues at the forecast average.
    tail = fmean(forecast)
    return [forecast[d] if d < len(forecast) else tail for d in range(days)]


# --- Inventory position and need --------------------------------------------------------


def available_on_hand(ctx: PurchasingContext) -> int:
    return max(0, ctx.on_hand - ctx.reserved)


def counted_open_pos(ctx: PurchasingContext, assumptions: Assumptions) -> list[OpenPurchaseOrder]:
    excluded = set(assumptions.exclude_po_ids)
    return [po for po in ctx.open_purchase_orders if po.po_id not in excluded]


def inventory_position(ctx: PurchasingContext, assumptions: Assumptions) -> int:
    on_order = sum(po.quantity for po in counted_open_pos(ctx, assumptions))
    return available_on_hand(ctx) + on_order


def order_up_to_level(ctx: PurchasingContext, assumptions: Assumptions) -> int:
    window = coverage_window_days(ctx)
    window_demand = sum(demand_series(ctx, assumptions, window))
    return math.ceil(window_demand - _EPS) + ctx.safety_stock


def net_need(ctx: PurchasingContext, assumptions: Assumptions) -> int:
    return max(0, order_up_to_level(ctx, assumptions) - inventory_position(ctx, assumptions))


# --- Order rounding and limits ----------------------------------------------------------


def min_valid_quantity(moq: int, case_pack: int) -> int:
    """Smallest order that satisfies both the MOQ and the case pack."""
    return math.ceil(moq / case_pack) * case_pack


def round_up_to_valid(quantity: float, moq: int, case_pack: int) -> int:
    if quantity <= 0:
        return 0
    rounded = math.ceil(quantity / case_pack - _EPS) * case_pack
    return max(min_valid_quantity(moq, case_pack), rounded)


def round_down_to_valid(quantity: float, moq: int, case_pack: int) -> int:
    """Largest valid order not above `quantity`, or 0 if none exists."""
    rounded = math.floor(quantity / case_pack + _EPS) * case_pack
    return rounded if rounded >= moq else 0


def order_value(ctx: PurchasingContext, quantity: int) -> float:
    return round(quantity * ctx.supplier.unit_cost, 2)


def max_affordable_quantity(ctx: PurchasingContext) -> int:
    """Largest case multiple the remaining budget pays for (MOQ not applied)."""
    units = math.floor(ctx.budget_available / ctx.supplier.unit_cost + _EPS)
    return (units // ctx.supplier.case_pack) * ctx.supplier.case_pack


def max_storable_quantity(ctx: PurchasingContext, assumptions: Assumptions) -> int:
    """Largest case multiple that keeps projected stock within capacity (MOQ not applied)."""
    case_pack = ctx.supplier.case_pack
    lo, hi = 0, ctx.storage_capacity // case_pack + 1  # search over number of cases
    if _peak_after_arrival(ctx, assumptions, 0) > ctx.storage_capacity:
        return 0
    # Peak stock never decreases as the order grows, so binary search is safe.
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _peak_after_arrival(ctx, assumptions, mid * case_pack) <= ctx.storage_capacity:
            lo = mid
        else:
            hi = mid - 1
    return lo * case_pack


# --- Projection -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Simulation:
    start_of_day: list[float]  # stock after that day's arrivals, before sales
    unmet: list[float]


def _eta_offset(ctx: PurchasingContext, po: OpenPurchaseOrder) -> int:
    # Overdue POs are assumed to arrive today; data-quality checks flag them separately.
    return max(0, (po.eta - ctx.today).days)


def _simulate(
    ctx: PurchasingContext,
    assumptions: Assumptions,
    order_quantity: int,
    days: int = SIMULATION_DAYS,
) -> _Simulation:
    demand = demand_series(ctx, assumptions, days)
    arrivals = [0.0] * days
    for po in counted_open_pos(ctx, assumptions):
        offset = _eta_offset(ctx, po)
        if offset < days:
            arrivals[offset] += po.quantity
    lead = ctx.supplier.lead_time_days
    if order_quantity > 0 and lead < days:
        arrivals[lead] += order_quantity

    stock = float(available_on_hand(ctx))
    start_of_day, unmet = [], []
    for day in range(days):
        stock += arrivals[day]
        start_of_day.append(stock)
        sold = min(stock, demand[day])
        unmet.append(demand[day] - sold)
        stock -= sold
    return _Simulation(start_of_day=start_of_day, unmet=unmet)


def _peak_after_arrival(
    ctx: PurchasingContext, assumptions: Assumptions, order_quantity: int
) -> float:
    sim = _simulate(ctx, assumptions, order_quantity)
    return max(sim.start_of_day[ctx.supplier.lead_time_days :])


def _first_stockout_day(sim: _Simulation, from_day: int = 0) -> int | None:
    for day in range(from_day, len(sim.unmet)):
        if sim.unmet[day] > _EPS:
            return day
    return None


def project(
    ctx: PurchasingContext, order_quantity: int, assumptions: Assumptions | None = None
) -> Projection:
    """Simulate stock if `order_quantity` is ordered today, and summarise the outcome."""
    assumptions = assumptions or Assumptions()
    lead = ctx.supplier.lead_time_days
    window = coverage_window_days(ctx)

    with_order = _simulate(ctx, assumptions, order_quantity)
    without_order = _simulate(ctx, assumptions, 0)

    stockout_after_arrival = _first_stockout_day(with_order, from_day=lead)
    if stockout_after_arrival is None:
        cover_days = SIMULATION_DAYS - lead
    else:
        cover_days = stockout_after_arrival - lead
    after_receipt = with_order.start_of_day[lead]

    return Projection(
        order_quantity=order_quantity,
        order_value=order_value(ctx, order_quantity),
        demand_basis=assumptions.demand_basis,
        daily_demand_rate=round(fmean(demand_series(ctx, assumptions, window)), 2),
        coverage_window_days=window,
        inventory_position=inventory_position(ctx, assumptions),
        order_up_to_level=order_up_to_level(ctx, assumptions),
        net_need=net_need(ctx, assumptions),
        arrival_day=lead,
        arrival_date=ctx.today + timedelta(days=lead),
        on_hand_at_arrival=round(after_receipt - order_quantity),
        on_hand_after_receipt=round(after_receipt),
        peak_on_hand_after_arrival=round(max(with_order.start_of_day[lead:])),
        cover_days_after_receipt=cover_days,
        stockout_day_without_order=_first_stockout_day(without_order),
        units_short_in_window=math.ceil(sum(with_order.unmet[:window]) - _EPS),
    )
