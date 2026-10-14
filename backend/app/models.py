"""Database schema: the purchasing world the agent investigates and acts on.

Dates are simulated-clock dates (see `Settings.sim_today`), so scenarios stay reproducible.
Models declare foreign keys but no ORM relationships: services query explicitly, and code that
inserts related rows in one session flushes parents first.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Bump whenever a table or column changes. On startup a database stamped with another
# version is rebuilt from the scenarios (demo data only; production would use migrations).
SCHEMA_VERSION = 8


class SchemaInfo(Base):
    __tablename__ = "schema_info"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)


class Node(Base):
    """A fulfillment node (dark store / warehouse)."""

    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(80))


class Product(Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(80))


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    on_time_rate: Mapped[float] = mapped_column(Float, doc="Share of POs delivered by ETA")
    fill_rate: Mapped[float] = mapped_column(Float, doc="Share of ordered units delivered")


class SupplierTerm(Base):
    """Commercial terms for one product from one supplier."""

    __tablename__ = "supplier_terms"
    __table_args__ = (UniqueConstraint("supplier_id", "sku"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.id"))
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    lead_time_days: Mapped[int] = mapped_column(Integer)
    moq: Mapped[int] = mapped_column(Integer)
    case_pack: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[float] = mapped_column(Float)
    # Free text from the supplier portal. Untrusted: shown to the agent as data, never as orders.
    note: Mapped[str | None] = mapped_column(Text, default=None)
    # How the mock supplier gateway will acknowledge POs for this product (demo/eval only).
    mock_ack: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)


class InventoryRecord(Base):
    __tablename__ = "inventory"

    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), primary_key=True)
    on_hand: Mapped[int] = mapped_column(Integer)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    safety_stock: Mapped[int] = mapped_column(Integer)
    storage_capacity: Mapped[int] = mapped_column(Integer, doc="Max units of this SKU at node")
    review_period_days: Mapped[int] = mapped_column(Integer)
    counted_on: Mapped[date | None] = mapped_column(Date, default=None)


class DemandForecast(Base):
    __tablename__ = "demand_forecasts"

    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    units: Mapped[float] = mapped_column(Float)


class SalesRecord(Base):
    __tablename__ = "sales_history"

    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    units: Mapped[int] = mapped_column(Integer)


class Budget(Base):
    """Purchasing budget for one category at one node for one month."""

    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("node_id", "category", "period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    category: Mapped[str] = mapped_column(String(80))
    period: Mapped[str] = mapped_column(String(7), doc="YYYY-MM")
    total: Mapped[float] = mapped_column(Float)
    committed: Mapped[float] = mapped_column(Float, doc="Spent or committed to submitted POs")

    @property
    def available(self) -> float:
        return round(self.total - self.committed, 2)


# POs whose units are still expected to arrive; they count toward inventory position.
OPEN_PO_STATUSES = ("submitted", "confirmed", "partially_confirmed")
# POs that are not finished (cancelled, declined or received); at most one per recommendation.
LIVE_PO_STATUSES = ("draft", "pending_approval", *OPEN_PO_STATUSES)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.id"))
    quantity: Mapped[int] = mapped_column(Integer, doc="Units ordered (current revision)")
    unit_cost: Mapped[float] = mapped_column(Float, doc="List price when ordered")
    status: Mapped[str] = mapped_column(String(24))
    eta: Mapped[date | None] = mapped_column(Date, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_on: Mapped[date] = mapped_column(Date)
    created_by: Mapped[str] = mapped_column(String(24), doc="system | buyer | agent")
    recommendation_id: Mapped[str | None] = mapped_column(
        ForeignKey("recommendations.id"), default=None
    )
    # The agent run that created this PO; agent tools may only touch their own run's POs.
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), default=None)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    # What the supplier actually accepted (None until acknowledged).
    confirmed_quantity: Mapped[int | None] = mapped_column(Integer, default=None)
    confirmed_unit_cost: Mapped[float | None] = mapped_column(Float, default=None)
    supplier_message: Mapped[str | None] = mapped_column(Text, default=None)
    # Amount this PO currently holds against its category budget.
    committed_value: Mapped[float] = mapped_column(Float, default=0.0)
    # Projection assumptions the PO was validated under (e.g. an excluded open PO).
    assumptions: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    approval_status: Mapped[str | None] = mapped_column(
        String(16), default=None, doc="pending | approved | declined; None if not needed"
    )
    approval_reasons: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    approval_by: Mapped[str | None] = mapped_column(String(64), default=None)
    approval_comment: Mapped[str | None] = mapped_column(Text, default=None)
    # Retried create calls with the same key return the existing PO instead of a duplicate.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, default=None)

    @property
    def outstanding_quantity(self) -> int:
        return self.confirmed_quantity if self.confirmed_quantity is not None else self.quantity

    @property
    def effective_unit_cost(self) -> float:
        return self.confirmed_unit_cost if self.confirmed_unit_cost is not None else self.unit_cost

    @property
    def value(self) -> float:
        return round(self.outstanding_quantity * self.effective_unit_cost, 2)


class PurchaseOrderEvent(Base):
    """Audit trail: one row per state change or supplier message on a PO."""

    __tablename__ = "purchase_order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_id: Mapped[str] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    on: Mapped[date] = mapped_column(Date)
    event: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(24), default=None)
    to_status: Mapped[str | None] = mapped_column(String(24), default=None)
    actor: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)


class Recommendation(Base):
    """A purchase suggestion from the replenishment system — the agent's input."""

    __tablename__ = "recommendations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    scenario: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(160))
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    created_on: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(Text, doc="How the source computed the quantity")
    # Snapshot of what the source saw, so staleness can be detected.
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="open")


class CustomScenario(Base):
    """A recommendation a person built in the scenario builder instead of a YAML file.

    Holds the reviewed form (to duplicate it) and the person's own expected answer. No agent
    code reads this table, so the agent cannot see that answer.
    """

    __tablename__ = "custom_scenarios"

    recommendation_id: Mapped[str] = mapped_column(
        ForeignKey("recommendations.id"), primary_key=True
    )
    number: Mapped[int] = mapped_column(Integer, unique=True)  # every id is derived from it
    preset: Mapped[str | None] = mapped_column(String(64), default=None)
    input: Mapped[dict[str, Any]] = mapped_column(JSON)
    reviewer_call: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentRun(Base):
    """One agent review of one recommendation, from kickoff to final decision."""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    recommendation_id: Mapped[str] = mapped_column(ForeignKey("recommendations.id"), index=True)
    status: Mapped[str] = mapped_column(
        String(24),
        doc="queued | running | awaiting_approval | completed | escalated | failed | needs_review",
    )
    provider: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str | None] = mapped_column(String(16), default=None)
    final_quantity: Mapped[int | None] = mapped_column(Integer, default=None)
    confidence: Mapped[float | None] = mapped_column(Float, default=None)
    rationale: Mapped[str | None] = mapped_column(Text, default=None)
    key_factors: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    risks: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    # Evidence categories the agent fetched, checked by the evidence gate and by evals.
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # Problems the final-decision checks still found after the agent's retry (needs_review).
    validation_issues: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    # Degraded mode: the rules-only proposal recorded for a human when the LLM was unavailable.
    fallback_proposal: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # Provider-native conversation, kept so a paused run can resume where it stopped.
    history: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    # While awaiting approval: the submit call being held and the other results of its turn.
    paused_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # Loop state that must survive a pause (circuit breaker counts, numbers the agent saw).
    agent_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # Wall-clock times (real, not simulated): they measure how long the agent took.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class AgentStep(Base):
    """Trace of a run: every model turn, tool call, guardrail intervention and system note."""

    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), doc="llm | tool | guardrail | system")
    name: Mapped[str | None] = mapped_column(String(64), default=None)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Escalation(Base):
    """A question the agent hands to a human instead of acting."""

    __tablename__ = "escalations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    recommendation_id: Mapped[str] = mapped_column(ForeignKey("recommendations.id"))
    severity: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    questions: Mapped[list[str]] = mapped_column(JSON)
    options: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_on: Mapped[date] = mapped_column(Date)
    resolved_by: Mapped[str | None] = mapped_column(String(64), default=None)
    resolution: Mapped[str | None] = mapped_column(Text, default=None)
    resolved_on: Mapped[date | None] = mapped_column(Date, default=None)
