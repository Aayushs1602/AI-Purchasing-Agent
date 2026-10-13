"""Baseline purchasing world where the recommended 800 units is exactly right.

demand 50/day, lead time 4, review period 14, safety stock 200
  -> order-up-to = 50 * (4 + 14) + 200 = 1,100
on hand 300, nothing on order -> need = 800 (40 cases of 20, above the MOQ of 400)
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.engine.models import OpenPurchaseOrder, PurchasingContext, SupplierTerms

TODAY = date(2026, 10, 1)
RECOMMENDED_QTY = 800


def make_context(supplier: dict[str, Any] | None = None, **overrides: Any) -> PurchasingContext:
    terms = {
        "supplier_id": "SUP-T",
        "supplier_name": "Test Supplier",
        "lead_time_days": 4,
        "moq": 400,
        "case_pack": 20,
        "unit_cost": 2.50,
    }
    terms.update(supplier or {})
    fields: dict[str, Any] = {
        "sku": "SKU-T",
        "node_id": "DS-T",
        "today": TODAY,
        "on_hand": 300,
        "reserved": 0,
        "safety_stock": 200,
        "inventory_counted_at": TODAY - timedelta(days=2),
        "review_period_days": 14,
        "daily_forecast": [50.0] * 60,
        "recent_daily_sales": [50.0] * 28,
        "open_purchase_orders": [],
        "supplier": SupplierTerms(**terms),
        "budget_available": 5000.0,
        "storage_capacity": 1200,
    }
    fields.update(overrides)
    return PurchasingContext(**fields)


def open_po(
    quantity: int, eta_in_days: int, po_id: str = "PO-1", status: str = "confirmed", note=None
) -> OpenPurchaseOrder:
    return OpenPurchaseOrder(
        po_id=po_id,
        supplier_id="SUP-T",
        quantity=quantity,
        eta=TODAY + timedelta(days=eta_in_days),
        status=status,
        note=note,
    )
