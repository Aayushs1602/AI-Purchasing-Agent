"""Input and output models for the purchasing engine."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

DemandBasis = Literal["forecast", "recent_actuals"]


class SupplierTerms(BaseModel):
    supplier_id: str
    supplier_name: str
    lead_time_days: int = Field(ge=0, le=60)
    moq: int = Field(ge=1, description="Minimum order quantity, in units")
    case_pack: int = Field(ge=1, description="Orders must be a multiple of this many units")
    unit_cost: float = Field(gt=0)


class OpenPurchaseOrder(BaseModel):
    po_id: str
    supplier_id: str
    quantity: int = Field(ge=0, description="Units still to be received")
    eta: date
    status: str
    note: str | None = None


class PurchasingContext(BaseModel):
    """Everything the engine needs to judge an order for one SKU at one node, as of `today`."""

    sku: str
    node_id: str
    today: date
    on_hand: int = Field(ge=0)
    reserved: int = Field(default=0, ge=0, description="Units already promised to orders")
    safety_stock: int = Field(ge=0)
    inventory_counted_at: date | None = None
    review_period_days: int = Field(ge=1, description="Days until the next ordering opportunity")
    daily_forecast: list[float] = Field(description="Forecast units per day; index 0 is today")
    recent_daily_sales: list[float] = Field(
        default_factory=list, description="Actual units sold per day, oldest first"
    )
    open_purchase_orders: list[OpenPurchaseOrder] = Field(default_factory=list)
    supplier: SupplierTerms
    budget_available: float = Field(ge=0)
    storage_capacity: int = Field(ge=0, description="Max units of this SKU the node can hold")


class Assumptions(BaseModel):
    """Choices about which data to trust. Defaults count every open PO and use the forecast.

    The agent may override them (e.g. ignore a PO that is on hold), but any override is
    surfaced as a finding so a human approves it.
    """

    exclude_po_ids: list[str] = Field(default_factory=list)
    demand_basis: DemandBasis = "forecast"

    def is_default(self) -> bool:
        return not self.exclude_po_ids and self.demand_basis == "forecast"


class Projection(BaseModel):
    """Day-by-day simulation of stock for a candidate order quantity."""

    order_quantity: int
    order_value: float
    demand_basis: DemandBasis
    daily_demand_rate: float = Field(description="Average daily demand over the coverage window")
    coverage_window_days: int = Field(description="Lead time + review period")
    inventory_position: int = Field(description="Available on hand + counted open POs")
    order_up_to_level: int = Field(description="Demand over the coverage window + safety stock")
    net_need: int = Field(description="Units needed to reach the order-up-to level")
    arrival_day: int
    arrival_date: date
    on_hand_at_arrival: int = Field(description="Projected stock on arrival day, before receipt")
    on_hand_after_receipt: int
    peak_on_hand_after_arrival: int
    cover_days_after_receipt: int
    stockout_day_without_order: int | None = Field(
        description="First day (from today) with unmet demand if nothing is ordered"
    )
    units_short_in_window: int = Field(
        description="Unmet demand within the coverage window even with this order"
    )
