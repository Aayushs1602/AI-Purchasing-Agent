"""Scenario fixtures: declarative purchasing worlds, loaded from `backend/scenarios/*.yaml`.

`_baseline.yaml` describes a world where the recommended 800 units is exactly right. Each
numbered file deep-merges a few overrides on top (the scenario's twist) and states the
hand-labelled `expect` block that evaluations grade against. Labels are written by a human,
not derived from the engine, so the engine is never graded against itself.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Decision = Literal["accept", "modify", "reject", "investigate"]
# Factors a decision rationale can cite; evals check the binding one is cited.
Factor = Literal[
    "net_need",
    "on_hand",
    "open_pos",
    "forecast_drift",
    "moq",
    "case_pack",
    "unit_cost",
    "budget",
    "storage",
    "cover_days",
    "order_value",
    "supplier_fill",
    "supplier_note",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeSpec(_Strict):
    id: str
    name: str
    city: str


class ProductSpec(_Strict):
    sku: str
    name: str
    category: str


class SupplierSpec(_Strict):
    id: str
    name: str
    on_time_rate: float = Field(ge=0, le=1)
    fill_rate: float = Field(ge=0, le=1)


class MockAckSpec(_Strict):
    """How the mock supplier responds when a PO is submitted."""

    behaviour: Literal["confirm", "price_increase", "partial_fill"] = "confirm"
    new_unit_cost: float | None = None
    max_quantity: int | None = None


class TermsSpec(_Strict):
    lead_time_days: int
    moq: int
    case_pack: int
    unit_cost: float
    note: str | None = None
    mock_ack: MockAckSpec = Field(default_factory=MockAckSpec)


class InventorySpec(_Strict):
    on_hand: int
    reserved: int = 0
    safety_stock: int
    storage_capacity: int
    review_period_days: int
    counted_days_ago: int | None


class DemandSpec(_Strict):
    forecast_per_day: float
    forecast_days: int = 60
    sales_days: int = 28
    sales_weekly_pattern: list[int] = Field(min_length=7, max_length=7)
    # Replaces the pattern for the most recent 14 days, to model a shift in demand.
    recent_14d_weekly_pattern: list[int] | None = Field(default=None, min_length=7, max_length=7)


class OpenPOSpec(_Strict):
    id: str
    quantity: int
    eta_in_days: int
    status: str
    created_days_ago: int = 7
    created_by: str = "buyer"
    note: str | None = None


class BudgetSpec(_Strict):
    total: float
    committed: float


class RecommendationSpec(_Strict):
    id: str
    quantity: int
    created_days_ago: int = 0
    source: str
    method: str
    inputs: dict[str, float | int | None]


class WorldSpec(_Strict):
    node: NodeSpec
    product: ProductSpec
    supplier: SupplierSpec
    terms: TermsSpec
    inventory: InventorySpec
    demand: DemandSpec
    open_purchase_orders: list[OpenPOSpec] = Field(default_factory=list)
    budget: BudgetSpec
    recommendation: RecommendationSpec


class ExpectedOutcome(_Strict):
    decision: Decision
    # Inclusive range for the quantity of the PO the run leaves live (0 = no PO).
    po_quantity: tuple[int, int] = (0, 0)
    approval: Literal["required", "not_required", "any"] = "any"

    @model_validator(mode="after")
    def _check_range(self) -> ExpectedOutcome:
        low, high = self.po_quantity
        if not 0 <= low <= high:
            raise ValueError(f"invalid po_quantity range {self.po_quantity}")
        return self


class Expectation(_Strict):
    acceptable: list[ExpectedOutcome] = Field(min_length=1)
    must_cite: list[Factor] = Field(default_factory=list)
    rationale: str


class Scenario(_Strict):
    id: str
    title: str
    summary: str
    world: WorldSpec
    expect: Expectation


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge dicts; lists and scalars in `overrides` replace the base value."""
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a mapping at the top level")
    return data


def load_scenarios(directory: Path) -> list[Scenario]:
    baseline = _read_yaml(directory / "_baseline.yaml")["world"]
    scenarios = []
    for path in sorted(directory.glob("[0-9]*.yaml")):
        raw = _read_yaml(path)
        raw["world"] = deep_merge(baseline, raw.get("world", {}))
        try:
            scenarios.append(Scenario.model_validate(raw))
        except ValueError as exc:
            raise ValueError(f"{path.name}: {exc}") from exc
    _check_unique(scenarios)
    return scenarios


def _check_unique(scenarios: list[Scenario]) -> None:
    """Scenarios share one database, so each needs its own SKU, budget line and recommendation."""
    keys = {
        "scenario id": [s.id for s in scenarios],
        "sku": [s.world.product.sku for s in scenarios],
        "budget category": [s.world.product.category for s in scenarios],
        "recommendation id": [s.world.recommendation.id for s in scenarios],
        "purchase order id": [po.id for s in scenarios for po in s.world.open_purchase_orders],
    }
    for label, values in keys.items():
        duplicates = {v for v in values if values.count(v) > 1}
        if duplicates:
            raise ValueError(f"duplicate {label}: {sorted(duplicates)}")

    suppliers: dict[str, SupplierSpec] = {}
    for scenario in scenarios:
        supplier = scenario.world.supplier
        if supplier.id in suppliers and suppliers[supplier.id] != supplier:
            raise ValueError(f"supplier {supplier.id} is defined differently in {scenario.id}")
        suppliers[supplier.id] = supplier
