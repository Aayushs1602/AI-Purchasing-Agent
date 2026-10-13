from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Budget, Node, Product, PurchaseOrder, Supplier


def test_budget_available_is_total_minus_committed(session):
    session.add(Node(id="DS-1", name="Test store", city="Bogotá"))
    session.flush()  # no ORM relationships, so parents are flushed before children
    session.add(
        Budget(node_id="DS-1", category="Beverages", period="2026-10", total=20000, committed=15000)
    )
    session.commit()

    budget = session.query(Budget).one()
    assert budget.available == 5000


def test_foreign_keys_are_enforced(session):
    session.add(Node(id="DS-1", name="Test store", city="Bogotá"))
    session.add(Supplier(id="SUP-1", name="Supplier", on_time_rate=0.95, fill_rate=0.98))
    session.add(Product(sku="SKU-1", name="Water", category="Beverages"))
    session.commit()

    session.add(
        PurchaseOrder(
            id="PO-1",
            sku="SKU-UNKNOWN",
            node_id="DS-1",
            supplier_id="SUP-1",
            quantity=100,
            unit_cost=2.5,
            status="confirmed",
            created_on=date(2026, 10, 1),
            created_by="buyer",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
