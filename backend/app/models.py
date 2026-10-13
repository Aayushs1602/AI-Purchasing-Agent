"""Database schema: the purchasing world the agent investigates and acts on.

Dates are simulated-clock dates (see `Settings.sim_today`), so scenarios stay reproducible.
Models declare foreign keys but no ORM relationships: services query explicitly, and code that
inserts related rows in one session flushes parents first.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import JSON, Date, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


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


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.id"))
    quantity: Mapped[int] = mapped_column(Integer, doc="Units ordered and not yet received")
    unit_cost: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24))
    eta: Mapped[date | None] = mapped_column(Date, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_on: Mapped[date] = mapped_column(Date)
    created_by: Mapped[str] = mapped_column(String(24), doc="system | buyer | agent")
    recommendation_id: Mapped[str | None] = mapped_column(
        ForeignKey("recommendations.id"), default=None
    )


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
