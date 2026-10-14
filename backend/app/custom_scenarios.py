"""Custom scenarios: a purchasing situation entered by a person instead of a YAML file.

`CustomScenarioInput` is the flat form a buyer fills in (or a model drafts from a description).
`to_world` turns it into the same `WorldSpec` the YAML scenarios use, so it is seeded and run
through exactly the same code. `from_world` goes the other way, so every demo scenario can be
used as a preset. Pure functions: no database, no LLM.

The agent sees the product, supplier and category names as plain text, so names are kept to a
single short line. Notes are the free-text channel; the tools mark them as untrusted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.engine.models import Decision
from app.scenarios import (
    BudgetSpec,
    DemandSpec,
    InventorySpec,
    MockAckSpec,
    NodeSpec,
    OpenPOSpec,
    ProductSpec,
    RecommendationSpec,
    SupplierSpec,
    TermsSpec,
    WorldSpec,
    load_scenarios,
)

SOURCE = "replenishment-engine-v1"
# Single line, letters, digits and a little punctuation: names reach the agent unwrapped.
NAME_PATTERN = r"^[\w .,&'()/%+-]+$"
# The baseline week (averages exactly 50/day); custom sales follow the same shape.
WEEKLY_SHAPE = (46, 52, 55, 49, 51, 47, 50)
MAX_OPEN_POS = 5


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenPOInput(_Strict):
    quantity: int = Field(ge=1, le=100_000, description="Units on the PO still to arrive")
    eta_in_days: int = Field(
        ge=-60, le=60, description="Days until it is due; negative means it is overdue"
    )
    status: Literal["confirmed", "submitted"] = Field(
        "confirmed", description="confirmed by the supplier, or only submitted"
    )
    created_days_ago: int = Field(7, ge=0, le=120, description="When the PO was placed")
    note: str | None = Field(
        None, max_length=300, description="Latest supplier or PO-system note about this PO"
    )

    @model_validator(mode="after")
    def _due_after_created(self) -> OpenPOInput:
        if self.eta_in_days < -self.created_days_ago:
            raise ValueError("a PO cannot be due before it was created")
        return self


class CustomScenarioInput(_Strict):
    # Product
    product_name: str = Field(min_length=2, max_length=60, pattern=NAME_PATTERN)
    category: str = Field(
        min_length=2, max_length=40, pattern=NAME_PATTERN, description="Budget category"
    )

    # Stock at the store
    on_hand: int = Field(ge=0, le=100_000, description="Units in stock now")
    reserved: int = Field(0, ge=0, le=100_000, description="Units already promised to orders")
    safety_stock: int = Field(ge=0, le=100_000, description="Buffer the store wants to keep")
    storage_capacity: int = Field(ge=1, le=200_000, description="Most units the store can hold")
    review_period_days: int = Field(ge=1, le=60, description="Days until the next order review")
    counted_days_ago: int | None = Field(
        ge=0, le=365, description="Days since the last stock count; null if never counted"
    )

    # Demand
    forecast_per_day: float = Field(ge=0, le=10_000, description="Forecast units per day")
    sales_per_day: int = Field(ge=0, le=10_000, description="Typical actual sales per day")
    recent_sales_per_day: int | None = Field(
        None,
        ge=0,
        le=10_000,
        description="Actual sales per day over the last 14 days, if different from typical",
    )

    # Purchase orders already on the way
    open_purchase_orders: list[OpenPOInput] = Field(default_factory=list, max_length=MAX_OPEN_POS)

    # Supplier: an existing one by id, or a new one described here
    supplier_id: str | None = Field(
        None, pattern=r"^[A-Z0-9-]{1,32}$", description="Existing supplier id, or null for new"
    )
    supplier_name: str | None = Field(None, min_length=2, max_length=60, pattern=NAME_PATTERN)
    supplier_on_time_rate: float | None = Field(None, ge=0, le=1)
    supplier_fill_rate: float | None = Field(None, ge=0, le=1)

    # Supplier terms
    lead_time_days: int = Field(ge=0, le=60, description="Days from order to delivery")
    moq: int = Field(ge=1, le=100_000, description="Minimum order quantity")
    case_pack: int = Field(ge=1, le=10_000, description="Orders must be a multiple of this")
    unit_cost: float = Field(ge=0.01, le=10_000, description="Price per unit in USD")
    supplier_note: str | None = Field(
        None, max_length=1000, description="Free text on the supplier portal"
    )

    # How the (simulated) supplier answers when a PO is sent
    supplier_response: Literal["confirm", "price_increase", "partial_fill"] = Field(
        "confirm",
        description="confirm as sent, confirm at a higher price, or confirm fewer units",
    )
    new_unit_cost: float | None = Field(
        None, ge=0.01, le=10_000, description="The higher price, for price_increase"
    )
    max_fill_quantity: int | None = Field(
        None, ge=1, le=100_000, description="Most units the supplier can ship, for partial_fill"
    )

    # Budget for the category this month
    budget_total: float = Field(ge=0, le=10_000_000)
    budget_committed: float = Field(ge=0, le=10_000_000, description="Already spent or committed")

    # The recommendation under review
    recommended_quantity: int = Field(ge=1, le=100_000)
    recommendation_days_old: int = Field(0, ge=0, le=30)
    # What the replenishment engine saw when it computed the quantity; null = same as now.
    seen_on_hand: int | None = Field(None, ge=0, le=100_000)
    seen_open_po_units: int | None = Field(None, ge=0, le=100_000)
    seen_daily_demand: float | None = Field(None, gt=0, le=10_000)

    @model_validator(mode="after")
    def _consistent(self) -> CustomScenarioInput:
        if self.reserved > self.on_hand:
            raise ValueError("reserved cannot be more than on hand")
        if self.supplier_id is None and (
            self.supplier_name is None
            or self.supplier_on_time_rate is None
            or self.supplier_fill_rate is None
        ):
            raise ValueError("a new supplier needs a name, an on-time rate and a fill rate")
        if self.supplier_response == "price_increase" and (
            self.new_unit_cost is None or self.new_unit_cost <= self.unit_cost
        ):
            raise ValueError("price_increase needs a new_unit_cost above unit_cost")
        if self.supplier_response == "partial_fill" and self.max_fill_quantity is None:
            raise ValueError("partial_fill needs a max_fill_quantity")
        return self


class ReviewerCall(_Strict):
    """What the person expects before the run. Stored apart and never shown to the agent."""

    decision: Decision
    quantity_min: int | None = Field(None, ge=0, le=100_000)
    quantity_max: int | None = Field(None, ge=0, le=100_000)
    note: str | None = Field(None, max_length=300)

    @model_validator(mode="after")
    def _range(self) -> ReviewerCall:
        low, high = self.quantity_min, self.quantity_max
        if (low is None) != (high is None):
            raise ValueError("give both ends of the quantity range, or neither")
        if low is not None and high is not None and low > high:
            raise ValueError("quantity_min is above quantity_max")
        return self


@dataclass(frozen=True)
class CustomIds:
    """Every id a custom scenario needs, from its number. `PO-C001-1` style ids are not
    numeric, so they leave the agent's `PO-5001` numbering alone."""

    number: int

    @property
    def tag(self) -> str:
        return f"C{self.number:03d}"

    @property
    def sku(self) -> str:
        return f"SKU-{self.tag}"

    @property
    def recommendation_id(self) -> str:
        return f"REC-{self.tag}"

    @property
    def scenario(self) -> str:
        return f"custom-{self.number:03d}"

    @property
    def supplier_id(self) -> str:
        return f"SUP-{self.tag}"

    def po_id(self, index: int) -> str:
        return f"PO-{self.tag}-{index}"


def weekly_pattern(average: int) -> list[int]:
    """Seven daily sales figures with the baseline's weekly shape and exactly this average."""
    total = average * len(WEEKLY_SHAPE)
    shape_total = sum(WEEKLY_SHAPE)
    exact = [w * total for w in WEEKLY_SHAPE]  # each day is exact / shape_total
    days = [e // shape_total for e in exact]
    # Largest remainders get the units left over (ties go to the earlier day).
    order = sorted(range(len(days)), key=lambda i: (-(exact[i] % shape_total), i))
    for i in order[: total - sum(days)]:
        days[i] += 1
    return days


def method_text(window_days: int) -> str:
    """How the replenishment engine says it computed the quantity (shown to the agent)."""
    days = "day" if window_days == 1 else "days"
    return (
        f"Order-up-to: forecast demand over lead time + review period ({window_days} {days}) "
        "plus safety stock, minus on-hand stock. Does not net open purchase orders and does "
        "not check budget or storage capacity."
    )


def formula_quantity(inp: CustomScenarioInput) -> int:
    """What the replenishment engine's formula gives for what it saw (a hint for the form)."""
    window = inp.lead_time_days + inp.review_period_days
    rate = inp.seen_daily_demand if inp.seen_daily_demand is not None else inp.forecast_per_day
    on_hand = inp.seen_on_hand if inp.seen_on_hand is not None else inp.on_hand
    return max(0, math.ceil(rate * window) + inp.safety_stock - on_hand)


def to_world(
    inp: CustomScenarioInput,
    ids: CustomIds,
    *,
    node: NodeSpec,
    supplier: SupplierSpec,
    category: str,
) -> WorldSpec:
    """The input as a `WorldSpec`. The caller resolves the supplier and a free budget category."""
    if inp.supplier_response == "price_increase":
        mock_ack = MockAckSpec(behaviour="price_increase", new_unit_cost=inp.new_unit_cost)
    elif inp.supplier_response == "partial_fill":
        mock_ack = MockAckSpec(behaviour="partial_fill", max_quantity=inp.max_fill_quantity)
    else:
        mock_ack = MockAckSpec()

    open_units = sum(po.quantity for po in inp.open_purchase_orders)
    return WorldSpec(
        node=node,
        product=ProductSpec(sku=ids.sku, name=inp.product_name, category=category),
        supplier=supplier,
        terms=TermsSpec(
            lead_time_days=inp.lead_time_days,
            moq=inp.moq,
            case_pack=inp.case_pack,
            unit_cost=inp.unit_cost,
            note=inp.supplier_note or None,
            mock_ack=mock_ack,
        ),
        inventory=InventorySpec(
            on_hand=inp.on_hand,
            reserved=inp.reserved,
            safety_stock=inp.safety_stock,
            storage_capacity=inp.storage_capacity,
            review_period_days=inp.review_period_days,
            counted_days_ago=inp.counted_days_ago,
        ),
        demand=DemandSpec(
            forecast_per_day=inp.forecast_per_day,
            sales_weekly_pattern=weekly_pattern(inp.sales_per_day),
            recent_14d_weekly_pattern=None
            if inp.recent_sales_per_day is None
            else weekly_pattern(inp.recent_sales_per_day),
        ),
        open_purchase_orders=[
            OpenPOSpec(
                id=ids.po_id(i),
                quantity=po.quantity,
                eta_in_days=po.eta_in_days,
                status=po.status,
                created_days_ago=po.created_days_ago,
                note=po.note or None,
            )
            for i, po in enumerate(inp.open_purchase_orders, start=1)
        ],
        budget=BudgetSpec(total=inp.budget_total, committed=inp.budget_committed),
        recommendation=RecommendationSpec(
            id=ids.recommendation_id,
            quantity=inp.recommended_quantity,
            created_days_ago=inp.recommendation_days_old,
            source=SOURCE,
            method=method_text(inp.lead_time_days + inp.review_period_days),
            inputs={
                "on_hand": inp.on_hand if inp.seen_on_hand is None else inp.seen_on_hand,
                "open_po_units": open_units
                if inp.seen_open_po_units is None
                else inp.seen_open_po_units,
                "daily_demand_rate": inp.forecast_per_day
                if inp.seen_daily_demand is None
                else inp.seen_daily_demand,
            },
        ),
    )


def from_world(world: WorldSpec) -> CustomScenarioInput:
    """A scenario world as form input (to use the demo scenarios as presets)."""
    demand, terms, inv = world.demand, world.terms, world.inventory
    if (demand.forecast_days, demand.sales_days) != (60, 28):
        raise ValueError("custom scenarios use 60 forecast days and 28 sales days")
    sales = fmean(demand.sales_weekly_pattern)
    recent = fmean(demand.recent_14d_weekly_pattern) if demand.recent_14d_weekly_pattern else None
    if sales != int(sales) or (recent is not None and recent != int(recent)):
        raise ValueError("custom scenarios need whole-number average sales")
    if any(po.created_by != "buyer" for po in world.open_purchase_orders):
        raise ValueError("custom scenarios only hold buyer-created open POs")

    seen = world.recommendation.inputs
    open_units = sum(po.quantity for po in world.open_purchase_orders)

    def changed(value, current):
        return None if value is None or value == current else value

    return CustomScenarioInput(
        product_name=world.product.name,
        category=world.product.category,
        on_hand=inv.on_hand,
        reserved=inv.reserved,
        safety_stock=inv.safety_stock,
        storage_capacity=inv.storage_capacity,
        review_period_days=inv.review_period_days,
        counted_days_ago=inv.counted_days_ago,
        forecast_per_day=demand.forecast_per_day,
        sales_per_day=int(sales),
        recent_sales_per_day=None if recent is None else int(recent),
        open_purchase_orders=[
            OpenPOInput(
                quantity=po.quantity,
                eta_in_days=po.eta_in_days,
                status=po.status,
                created_days_ago=po.created_days_ago,
                note=po.note,
            )
            for po in world.open_purchase_orders
        ],
        supplier_id=world.supplier.id,
        lead_time_days=terms.lead_time_days,
        moq=terms.moq,
        case_pack=terms.case_pack,
        unit_cost=terms.unit_cost,
        supplier_note=terms.note,
        supplier_response=terms.mock_ack.behaviour,
        new_unit_cost=terms.mock_ack.new_unit_cost,
        max_fill_quantity=terms.mock_ack.max_quantity,
        budget_total=world.budget.total,
        budget_committed=world.budget.committed,
        recommended_quantity=world.recommendation.quantity,
        recommendation_days_old=world.recommendation.created_days_ago,
        seen_on_hand=changed(seen.get("on_hand"), inv.on_hand),
        seen_open_po_units=changed(seen.get("open_po_units"), open_units),
        seen_daily_demand=changed(seen.get("daily_demand_rate"), demand.forecast_per_day),
    )


class Preset(BaseModel):
    key: str
    title: str
    summary: str
    input: CustomScenarioInput


def presets(scenarios_dir: Path) -> list[Preset]:
    """The demo scenarios as starting points for the form (their labels are left out)."""
    return [
        Preset(key=s.id, title=s.title, summary=s.summary, input=from_world(s.world))
        for s in load_scenarios(scenarios_dir)
    ]
