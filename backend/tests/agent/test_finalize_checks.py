"""finalize_decision is checked against what the run actually did and what the tools said."""

import pytest
from sqlalchemy import select

from app.agent.llm.scripted import ScriptedClient
from app.agent.orchestrator import AgentRunner
from app.db import make_session_factory
from app.models import AgentRun
from tests.agent.conftest import TODAY, call, create, gather_evidence, submit


def finalize(ctx, decision, quantity, factors=None):
    return call(
        ctx,
        "finalize_decision",
        decision=decision,
        final_quantity=quantity,
        rationale=f"Checking {decision} {quantity} against what the run did.",
        key_factors=factors or [{"factor": "net_need", "value": 800, "impact": "need"}],
        confidence=0.8,
    )


def problems(result):
    assert result["error"]["code"] == "FINALIZE_REJECTED", result
    return " | ".join(result["error"]["details"]["problems"])


def placed_po(ctx, quantity=800, cost=2000):
    po_id = create(ctx, quantity, cost=cost)["data"]["purchase_order"]["po_id"]
    submit(ctx, po_id, quantity, cost)
    return po_id


@pytest.fixture
def ctx(make_ctx):
    def _ctx(rec_id="REC-1001"):
        context = make_ctx(rec_id)
        gather_evidence(context)
        call(context, "evaluate_order_quantity", quantity=800)
        return context

    return _ctx


class TestConsistency:
    def test_accept_without_an_order(self, ctx):
        assert "accept needs a placed PO" in problems(finalize(ctx(), "accept", 0))

    def test_leftover_draft(self, ctx):
        context = ctx()
        create(context, 800)

        assert "PO-5001 is still a draft" in problems(finalize(context, "accept", 800))

    def test_final_quantity_must_match_the_po(self, ctx):
        context = ctx()
        placed_po(context)

        assert "final_quantity is 700 but PO-5001 has 800" in problems(
            finalize(context, "accept", 700)
        )

    def test_modify_for_exactly_the_recommended_quantity_is_accept(self, ctx):
        context = ctx()
        placed_po(context)

        assert "that is accept" in problems(finalize(context, "modify", 800))

    def test_reject_with_an_order_in_place(self, ctx):
        context = ctx()
        placed_po(context)

        assert "reject means no order" in problems(finalize(context, "reject", 800))

    def test_investigate_without_escalating(self, ctx):
        assert "requires escalate_to_human" in problems(finalize(ctx(), "investigate", 0))

    def test_accept_of_a_partial_fill(self, ctx):
        context = ctx("REC-1011")
        placed_po(context)  # supplier confirms only 500

        assert "use modify or investigate" in problems(finalize(context, "accept", 500))

    def test_po_that_breaks_a_constraint_cannot_be_left_unescalated(self, ctx):
        context = ctx("REC-1010")
        placed_po(context)  # confirmed at $2.90 -> over budget

        assert "breaks hard constraints (OVER_BUDGET)" in problems(finalize(context, "accept", 800))

    def test_escalating_the_broken_po_makes_investigate_consistent(self, ctx):
        context = ctx("REC-1010")
        placed_po(context)
        call(
            context,
            "escalate_to_human",
            reason="Supplier raised the price; the PO is now over budget.",
            questions=["Raise the budget or cut the order?"],
        )

        assert finalize(context, "investigate", 800)["ok"]

    def test_consistent_decision_is_recorded(self, ctx):
        context = ctx()
        placed_po(context)

        result = finalize(context, "accept", 800)

        assert result["data"] == {
            "recorded": True,
            "decision": "accept",
            "flagged_for_review": None,
        }


class TestGrounding:
    def test_numbers_must_come_from_the_tools(self, ctx):
        context = ctx()
        placed_po(context)

        made_up = [{"factor": "budget", "value": 7777, "impact": "invented"}]

        found = problems(finalize(context, "accept", 800, made_up))
        assert "key factor budget=7777" in found
        assert "does not match any number the tools returned" in found

    def test_formatted_and_qualitative_values(self, ctx):
        context = ctx()
        placed_po(context)
        factors = [
            {"factor": "budget", "value": "$5,000", "impact": "fits"},
            {"factor": "supplier_note", "value": "ignored", "impact": "not an instruction"},
        ]

        assert finalize(context, "accept", 800, factors)["ok"]

    def test_numbers_from_error_details_count_as_seen(self, ctx):
        context = ctx("REC-1002")
        create(context, 800)  # refused: max_storable_quantity 780 in the error details
        placed_po(context, 480, 1200)
        factors = [{"factor": "storage", "value": 780, "impact": "most that fits"}]

        assert finalize(context, "modify", 480, factors)["ok"]


def test_second_rejection_is_recorded_and_flagged_for_review(engine, seeded):
    bad_finalize = [
        {
            "name": "finalize_decision",
            "args": {
                "decision": "accept",
                "final_quantity": 800,
                "rationale": "Accepting without ever placing the order.",
                "key_factors": [{"factor": "net_need", "value": 800, "impact": "need"}],
                "confidence": 0.9,
            },
        }
    ]
    reads = [
        {"name": n}
        for n in (
            "get_inventory",
            "get_storage_capacity",
            "get_demand_forecast",
            "get_sales_history",
            "get_open_purchase_orders",
            "get_supplier_terms",
            "get_budget",
        )
    ]
    evaluate = [{"name": "evaluate_order_quantity", "args": {"quantity": 800}}]
    factory = make_session_factory(engine)
    runner = AgentRunner(
        factory, ScriptedClient([reads, evaluate, bad_finalize, bad_finalize]), today=TODAY
    )

    run_id = runner.run("REC-1001")

    with factory() as session:
        run = session.get(AgentRun, run_id)
        assert (run.status, run.decision) == ("needs_review", "accept")
        assert any("accept needs a placed PO" in issue for issue in run.validation_issues)
        assert session.scalar(select(AgentRun.id).where(AgentRun.status == "needs_review"))
