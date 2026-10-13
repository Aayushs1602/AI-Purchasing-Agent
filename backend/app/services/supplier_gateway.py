"""Supplier order API.

`SupplierGateway` is the seam where a real supplier integration (EDI, REST API, portal)
would plug in. `MockSupplierGateway` stands in for it with deterministic behaviour set per
product in `supplier_terms.mock_ack`, so a scenario can make the supplier answer differently
from what the agent expects:

- confirm:         everything accepted as ordered
- price_increase:  accepted, but at a higher unit cost (which also becomes the new list price)
- partial_fill:    only up to `max_quantity` units accepted
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal, Protocol

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SupplierTerm


class OrderRequest(BaseModel):
    po_id: str
    supplier_id: str
    sku: str
    quantity: int
    unit_cost: float
    requested_on: date


class SupplierAck(BaseModel):
    po_id: str
    status: Literal["confirmed", "partially_confirmed"]
    confirmed_quantity: int
    confirmed_unit_cost: float
    eta: date
    message: str
    # Set when the supplier changed its price for future orders too.
    new_list_price: float | None = None


class SupplierGatewayError(RuntimeError):
    """The supplier could not be reached or did not understand the order."""


class SupplierGateway(Protocol):
    def send_order(self, order: OrderRequest) -> SupplierAck: ...


class MockSupplierGateway:
    def __init__(self, session: Session):
        self._session = session

    def send_order(self, order: OrderRequest) -> SupplierAck:
        terms = self._session.scalar(
            select(SupplierTerm).where(
                SupplierTerm.supplier_id == order.supplier_id, SupplierTerm.sku == order.sku
            )
        )
        if terms is None:
            raise SupplierGatewayError(f"{order.supplier_id} does not supply {order.sku}")

        config = terms.mock_ack or {"behaviour": "confirm"}
        behaviour = config.get("behaviour", "confirm")
        quantity, unit_cost = order.quantity, order.unit_cost
        status: Literal["confirmed", "partially_confirmed"] = "confirmed"
        message = f"Order confirmed: {quantity} units at ${unit_cost:.2f}."
        new_list_price = None

        if behaviour == "price_increase":
            new_cost = float(config["new_unit_cost"])
            if unit_cost < new_cost:
                unit_cost = new_list_price = new_cost
                message = (
                    f"Order confirmed at a revised price of ${new_cost:.2f}/unit "
                    f"(was ${order.unit_cost:.2f}) due to higher input costs."
                )
        elif behaviour == "partial_fill":
            cap = int(config["max_quantity"])
            if quantity > cap:
                quantity, status = cap, "partially_confirmed"
                message = (
                    f"Only {cap} of {order.quantity} units can be supplied this cycle; "
                    f"the remaining {order.quantity - cap} are not available."
                )
        elif behaviour != "confirm":
            raise SupplierGatewayError(f"Unknown mock behaviour '{behaviour}'")

        return SupplierAck(
            po_id=order.po_id,
            status=status,
            confirmed_quantity=quantity,
            confirmed_unit_cost=unit_cost,
            eta=order.requested_on + timedelta(days=terms.lead_time_days),
            message=message,
            new_list_price=new_list_price,
        )
