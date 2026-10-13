"""API response models."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.engine.models import PurchasingContext
from app.engine.policy import Finding, QuantityEvaluation


class RecommendationSummary(BaseModel):
    id: str
    scenario: str
    title: str
    sku: str
    product_name: str
    category: str
    node_id: str
    supplier_id: str
    supplier_name: str
    quantity: int
    created_on: date
    status: str


class RecommendationDetail(RecommendationSummary):
    source: str
    method: str
    inputs: dict[str, Any]


class RecommendationContext(BaseModel):
    """Current data for a recommendation, and what the engine says about the recommended qty."""

    recommendation_id: str
    context: PurchasingContext
    evaluation: QuantityEvaluation
    recommendation_findings: list[Finding]
    supplier_note: str | None


class PurchaseOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sku: str
    node_id: str
    supplier_id: str
    quantity: int
    unit_cost: float
    status: str
    eta: date | None
    note: str | None
    created_on: date
    created_by: str
    recommendation_id: str | None
    revision: int
    confirmed_quantity: int | None
    confirmed_unit_cost: float | None
    outstanding_quantity: int
    value: float
    committed_value: float
    supplier_message: str | None
    assumptions: dict[str, Any] | None
    approval_status: str | None
    approval_reasons: list[dict[str, Any]] | None
    approval_by: str | None
    approval_comment: str | None


class PurchaseOrderEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    on: date
    event: str
    from_status: str | None
    to_status: str | None
    actor: str
    detail: dict[str, Any] | None


class PurchaseOrderDetail(PurchaseOrderOut):
    events: list[PurchaseOrderEventOut]
