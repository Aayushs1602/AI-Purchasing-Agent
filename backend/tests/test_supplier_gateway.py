from datetime import date

import pytest

from app.models import Node, Product, Supplier, SupplierTerm
from app.services.supplier_gateway import MockSupplierGateway, OrderRequest, SupplierGatewayError

TODAY = date(2026, 10, 1)


@pytest.fixture
def gateway(session):
    session.add_all(
        [
            Node(id="DS-1", name="Store", city="Bogota"),
            Supplier(id="SUP-1", name="Supplier", on_time_rate=0.95, fill_rate=0.97),
            Product(sku="CONFIRM", name="a", category="a"),
            Product(sku="PRICE", name="b", category="b"),
            Product(sku="PARTIAL", name="c", category="c"),
            Product(sku="BROKEN", name="d", category="d"),
        ]
    )
    session.flush()
    terms = dict(supplier_id="SUP-1", lead_time_days=4, moq=400, case_pack=20, unit_cost=2.5)
    session.add_all(
        [
            SupplierTerm(sku="CONFIRM", mock_ack={"behaviour": "confirm"}, **terms),
            SupplierTerm(
                sku="PRICE", mock_ack={"behaviour": "price_increase", "new_unit_cost": 2.9}, **terms
            ),
            SupplierTerm(
                sku="PARTIAL", mock_ack={"behaviour": "partial_fill", "max_quantity": 500}, **terms
            ),
            SupplierTerm(sku="BROKEN", mock_ack={"behaviour": "teleport"}, **terms),
        ]
    )
    session.commit()
    return MockSupplierGateway(session)


def order(sku, quantity=800, unit_cost=2.5):
    return OrderRequest(
        po_id="PO-1",
        supplier_id="SUP-1",
        sku=sku,
        quantity=quantity,
        unit_cost=unit_cost,
        requested_on=TODAY,
    )


def test_confirm_accepts_the_order_as_sent(gateway):
    ack = gateway.send_order(order("CONFIRM"))

    assert (ack.status, ack.confirmed_quantity, ack.confirmed_unit_cost) == ("confirmed", 800, 2.5)
    assert ack.eta == date(2026, 10, 5)  # today + 4-day lead time
    assert ack.new_list_price is None


def test_price_increase_confirms_at_the_new_price(gateway):
    ack = gateway.send_order(order("PRICE"))

    assert (ack.status, ack.confirmed_quantity, ack.confirmed_unit_cost) == ("confirmed", 800, 2.9)
    assert ack.new_list_price == 2.9
    assert "2.90" in ack.message


def test_price_increase_is_not_reapplied_once_the_order_uses_the_new_price(gateway):
    ack = gateway.send_order(order("PRICE", quantity=740, unit_cost=2.9))

    assert (ack.confirmed_quantity, ack.confirmed_unit_cost) == (740, 2.9)
    assert ack.new_list_price is None


def test_partial_fill_caps_the_quantity(gateway):
    ack = gateway.send_order(order("PARTIAL"))

    assert (ack.status, ack.confirmed_quantity) == ("partially_confirmed", 500)
    assert "300" in ack.message


def test_partial_fill_confirms_orders_within_the_cap(gateway):
    ack = gateway.send_order(order("PARTIAL", quantity=500))

    assert (ack.status, ack.confirmed_quantity) == ("confirmed", 500)


def test_unknown_behaviour_or_product_fails_loudly(gateway):
    with pytest.raises(SupplierGatewayError, match="teleport"):
        gateway.send_order(order("BROKEN"))
    with pytest.raises(SupplierGatewayError, match="does not supply"):
        gateway.send_order(order("MISSING"))
