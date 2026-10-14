"""API response models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.engine.models import PurchasingContext
from app.engine.policy import Finding, QuantityEvaluation


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    recommendation_id: str
    status: str
    provider: str
    model: str
    decision: str | None
    final_quantity: int | None
    confidence: float | None
    llm_calls: int
    prompt_tokens: int
    output_tokens: int
    started_at: datetime
    finished_at: datetime | None


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
    latest_run: RunSummary | None = None
    # A PO still in play for this recommendation; while it exists, no new run can start.
    live_po: PurchaseOrderOut | None = None
    custom: bool = False  # built in the scenario builder rather than a demo scenario


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


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    kind: str
    name: str | None
    payload: dict[str, Any]
    duration_ms: int | None
    created_at: datetime


class EscalationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    recommendation_id: str
    severity: str
    reason: str
    questions: list[str]
    options: list[str]
    status: str
    created_on: date
    resolved_by: str | None
    resolution: str | None
    resolved_on: date | None


class PausedFor(BaseModel):
    po_id: str
    reasons: list[dict[str, Any]]


class ReviewerVerdict(BaseModel):
    """The answer the person expected before the run, next to what the agent decided."""

    decision: str
    quantity_min: int | None
    quantity_max: int | None
    note: str | None
    decision_match: bool | None  # None until the agent has decided
    quantity_match: bool | None  # None without a range or a decision
    agrees: bool | None


class RunDetail(RunSummary):
    rationale: str | None
    key_factors: list[dict[str, Any]] | None
    risks: list[str] | None
    evidence: list[str]
    error: str | None
    validation_issues: list[str] | None
    fallback_proposal: dict[str, Any] | None
    paused_for: PausedFor | None
    purchase_orders: list[PurchaseOrderOut]
    escalations: list[EscalationOut]
    steps: list[StepOut]
    reviewer_call: ReviewerVerdict | None = None


class RunStarted(BaseModel):
    run_id: str
    status: str


# RecommendationSummary refers to PurchaseOrderOut, defined further down.
RecommendationSummary.model_rebuild()
RecommendationDetail.model_rebuild()
