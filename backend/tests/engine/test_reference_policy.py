"""The rules baseline on the planned scenario-1 variants.

Cases marked "rules are wrong" are where the LLM agent is expected to do better: the rules
cannot read PO notes or judge whether to trust the forecast.
"""

import pytest

from app.engine.reference_policy import decide
from tests.engine.factories import RECOMMENDED_QTY, make_context, open_po

CASES = {
    "accept_baseline": ({}, "accept", 800, False),
    "open_po_overlap": (
        {"open_purchase_orders": [open_po(320, eta_in_days=2)]},
        "modify",
        480,
        True,  # every change to the recommendation goes to a buyer
    ),
    # Rules are wrong: the PO is on hold, so it should not offset the need.
    "open_po_on_hold": (
        {"open_purchase_orders": [open_po(320, eta_in_days=-3, note="On hold, ETA unconfirmed")]},
        "modify",
        480,
        True,
    ),
    "budget_cap": ({"budget_available": 1500}, "modify", 600, True),
    "storage_cap": ({"storage_capacity": 800}, "modify", 700, True),
    "moq_roundup": ({"supplier": {"moq": 1000}}, "modify", 1000, True),
    "overstock_stale_rec": ({"on_hand": 1150}, "reject", 0, False),
    # Rules are wrong: sales have halved, but the rules still trust the forecast.
    "forecast_drift": (
        {"recent_daily_sales": [50.0] * 14 + [25.0] * 14},
        "accept",
        800,
        True,
    ),
    "infeasible_moq_budget": (
        {"supplier": {"moq": 1000}, "budget_available": 2000},
        "investigate",
        0,
        False,
    ),
    "high_value_approval": ({"supplier": {"unit_cost": 4.50}}, "accept", 800, True),
}


@pytest.mark.parametrize("case", CASES.keys())
def test_reference_decision(case):
    ctx_kwargs, decision, quantity, requires_approval = CASES[case]

    result = decide(make_context(**ctx_kwargs), RECOMMENDED_QTY)

    assert (result.decision, result.quantity) == (decision, quantity)
    assert result.requires_approval is requires_approval
    if result.evaluation is not None and result.quantity > 0:
        assert result.evaluation.feasible


def test_capped_decision_names_the_binding_constraint():
    result = decide(make_context(storage_capacity=800), RECOMMENDED_QTY)

    assert any("storage" in reason for reason in result.reasons)


def test_infeasible_decision_explains_why():
    ctx = make_context(supplier={"moq": 1000}, budget_available=2000)

    result = decide(ctx, RECOMMENDED_QTY)

    assert "1000" in result.reasons[-1]
