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


def confirmed(quantity):
    po = draft()
    po.confirmed_quantity, po.confirmed_unit_cost, po.eta = quantity, 2.5, date(2026, 10, 5)
    return po


def test_small_shortfall_can_be_accepted_by_the_agent():
    check = check_outcome(
        ActionExpectation(quantity=800, max_total_cost=2000),
        confirmed(720),
        evaluation(720),  # within 20% of the need: no approval rule fires
        sent=True,
    )

    assert [d.field for d in check.differences] == ["quantity"]
    assert check.needs_human_approval == []
    assert "accept the deviation and explain it in finalize_decision" in check.next_actions


def test_shortfall_that_needs_approval_must_go_to_a_human():
    check = check_outcome(
        ActionExpectation(quantity=800, max_total_cost=2000),
        confirmed(500),
        evaluation(500),
        sent=True,
    )

    assert check.needs_human_approval == ["UNDER_COVERAGE"]
    assert "accept the deviation and explain it in finalize_decision" not in check.next_actions
    assert check.next_actions[-1].startswith("accepting this deviation needs a human")
