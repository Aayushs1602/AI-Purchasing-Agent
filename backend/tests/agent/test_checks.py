from datetime import date

from app.agent.checks import ActionExpectation, check_outcome
from app.engine.policy import evaluate_quantity
from app.models import PurchaseOrder
from tests.engine.factories import make_context

TODAY = date(2026, 10, 1)


def draft(quantity=800, unit_cost=2.5):
    return PurchaseOrder(id="PO-1", quantity=quantity, unit_cost=unit_cost, status="draft")


def evaluation(quantity=800):
    return evaluate_quantity(make_context(), quantity)  # arrives day 4, 18 days of cover


def test_matching_draft_is_as_expected():
    check = check_outcome(
        ActionExpectation(quantity=800, max_total_cost=2000, min_cover_days=15, max_cover_days=20),
        draft(),
        evaluation(),
        sent=False,
    )

    assert (check.status, check.differences) == ("AS_EXPECTED", [])


def test_late_arrival_and_cover_outside_the_range_are_reported():
    check = check_outcome(
        ActionExpectation(
            quantity=800,
            max_total_cost=2000,
            arrive_by=date(2026, 10, 3),
            min_cover_days=20,
        ),
        draft(),
        evaluation(),
        sent=False,
    )

    assert check.status == "UNEXPECTED_OUTCOME"
    assert [d.field for d in check.differences] == ["arrival", "cover_days"]
    assert check.differences[0].actual == "2026-10-05"
    assert check.next_actions[0].startswith("amend_purchase_order")


def test_confirmed_values_are_compared_once_sent():
    po = draft()
    po.confirmed_quantity, po.confirmed_unit_cost, po.eta = 500, 2.5, date(2026, 10, 5)

    check = check_outcome(
        ActionExpectation(quantity=800, max_total_cost=2000), po, evaluation(500), sent=True
    )

    assert [d.field for d in check.differences] == ["quantity"]
    assert "accept the deviation and explain it in finalize_decision" in check.next_actions
