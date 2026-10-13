"""Approval pause and resume, end to end with a scripted LLM."""

import pytest
from sqlalchemy import select

from app.agent.llm.scripted import ScriptedClient
from app.agent.orchestrator import AgentRunner, RunNotPausedError
from app.db import make_session_factory
from app.models import AgentRun, AgentStep, Budget, PurchaseOrder, Recommendation
from tests.agent.conftest import TODAY
from tests.agent.test_orchestrator import READS, create_po, finalize, submit


def held_result(results):
    """The paused submit's result, as delivered to the model on resume."""
    return next(r.payload for r in results if r.name == "submit_purchase_order")


@pytest.fixture
def factory(engine, seeded):
    return make_session_factory(engine)


def start(factory, rec_id, script):
    client = ScriptedClient(script)
    runner = AgentRunner(factory, client, today=TODAY)
    run_id = runner.run(rec_id)
    return runner, run_id, client


def load(factory, run_id):
    session = factory()
    run = session.get(AgentRun, run_id)
    po = session.scalar(select(PurchaseOrder).where(PurchaseOrder.run_id == run_id))
    return session, run, po


def step_names(session, run_id, kind):
    return session.scalars(
        select(AgentStep.name)
        .where(AgentStep.run_id == run_id, AgentStep.kind == kind)
        .order_by(AgentStep.seq)
    ).all()


class TestPause:
    def test_gated_submit_pauses_without_answering_the_model(self, factory):
        _, run_id, client = start(
            factory, "REC-1012", [READS, create_po(800, 3600), submit(800, 3600)]
        )

        session, run, po = load(factory, run_id)
        assert run.status == "awaiting_approval"
        assert po.status == "pending_approval"
        assert run.paused_state["po_id"] == po.id
        assert run.paused_state["expectation"] == {"quantity": 800, "max_total_cost": 3600}
        assert run.finished_at is None
        assert session.get(Recommendation, "REC-1012").status == "awaiting_approval"
        # The model received READS and create results, but not the held submit result.
        assert len(client.sessions[0].received_results) == 2
        assert "paused_for_approval" in step_names(session, run_id, "system")

    def test_finalize_in_the_same_turn_is_deferred(self, factory):
        def submit_and_finalize(results):
            return [
                *submit(800, 3600)(results),
                *finalize("accept", 800, factor="order_value", value=3600),
            ]

        _, run_id, _ = start(
            factory, "REC-1012", [READS, create_po(800, 3600), submit_and_finalize]
        )

        session, run, _ = load(factory, run_id)
        assert run.status == "awaiting_approval"
        assert run.decision is None
        deferred = next(r for r in run.paused_state["results"] if r["name"] == "finalize_decision")
        assert deferred["payload"]["error"]["code"] == "FINALIZE_DEFERRED"


class TestResume:
    def test_approval_sends_the_po_and_the_agent_finishes(self, factory):
        seen = {}

        def after_approval(results):
            seen["result"] = held_result(results)
            return finalize("accept", 800, factor="order_value", value=3600)

        runner, run_id, _ = start(
            factory,
            "REC-1012",
            [READS, create_po(800, 3600), submit(800, 3600), after_approval],
        )

        runner.resume(run_id, approve=True, approver="buyer:ana", comment="Approved for promo")

        session, run, po = load(factory, run_id)
        data = seen["result"]["data"]
        assert data["outcome"] == "APPROVED_AND_SENT"
        assert data["approval"] == {
            "approved": True,
            "by": "buyer:ana",
            "comment": "Approved for promo",
        }
        assert data["outcome_check"]["status"] == "AS_EXPECTED"
        assert (run.status, run.decision, run.paused_state) == ("completed", "accept", None)
        assert (po.status, po.approval_by) == ("confirmed", "buyer:ana")
        assert session.get(Recommendation, "REC-1012").status == "decided"
        assert step_names(session, run_id, "system")[-2:] == ["resumed", "settled"]

    def test_a_changed_quantity_goes_out_only_after_approval(self, factory):
        def after_approval(results):
            assert held_result(results)["data"]["outcome"] == "APPROVED_AND_SENT"
            return finalize("modify", 480, factor="open_pos", value=320)

        runner, run_id, _ = start(
            factory,
            "REC-1002",
            [READS, create_po(480), submit(480, 1200), after_approval],
        )

        session, run, po = load(factory, run_id)
        assert (run.status, po.status) == ("awaiting_approval", "pending_approval")
        assert [r["code"] for r in po.approval_reasons] == ["QUANTITY_CHANGED"]

        runner.resume(run_id, approve=True, approver="buyer:ana")

        session, run, po = load(factory, run_id)
        assert (run.status, run.decision, run.final_quantity) == ("completed", "modify", 480)
        assert (po.status, po.approval_by) == ("confirmed", "buyer:ana")

    def test_decline_lets_the_agent_choose_another_path(self, factory):
        def after_decline(results):
            assert held_result(results)["data"]["outcome"] == "DECLINED"
            return [
                {
                    "name": "escalate_to_human",
                    "args": {
                        "reason": "The buyer declined the 800-unit coffee order.",
                        "questions": ["Should we wait for the promo price?"],
                    },
                }
            ]

        runner, run_id, _ = start(
            factory,
            "REC-1012",
            [
                READS,
                create_po(800, 3600),
                submit(800, 3600),
                after_decline,
                finalize("investigate", 0, factor="order_value", value=3600),
            ],
        )

        runner.resume(run_id, approve=False, approver="buyer:ana", comment="Wait for promo price")

        _, run, po = load(factory, run_id)
        assert (run.status, run.decision) == ("escalated", "investigate")
        assert (po.status, po.approval_comment) == ("declined", "Wait for promo price")

    def test_approval_that_no_longer_fits_is_reported_to_the_agent(self, factory):
        def after_failed_approval(results):
            error = held_result(results)["error"]
            assert error["code"] == "HARD_CONSTRAINT_VIOLATED"
            assert error["message"].startswith("The approval could not be applied")
            return [
                {
                    "name": "cancel_purchase_order",
                    "args": {"po_id": "PO-5001", "reason": "No budget"},
                },
                {
                    "name": "escalate_to_human",
                    "args": {
                        "reason": "Budget was spent while the PO waited for approval.",
                        "questions": ["Can the coffee budget be topped up?"],
                    },
                },
            ]

        runner, run_id, _ = start(
            factory,
            "REC-1012",
            [
                READS,
                create_po(800, 3600),
                submit(800, 3600),
                after_failed_approval,
                finalize("investigate", 0, factor="order_value", value=3600),
            ],
        )
        with factory() as session:  # someone spends the budget while the PO waits
            budget = session.scalar(select(Budget).where(Budget.category == "Coffee"))
            budget.committed = budget.total - 1000
            session.commit()

        runner.resume(run_id, approve=True, approver="buyer:ana")

        _, run, po = load(factory, run_id)
        assert (run.status, po.status) == ("escalated", "cancelled")

    def test_price_rise_found_after_approval_counts_as_a_deviation(self, factory):
        runner, run_id, _ = start(
            factory,
            "REC-1010",
            [
                READS,
                create_po(800),
                lambda results: [
                    {
                        "name": "submit_purchase_order",
                        "args": {
                            "po_id": "PO-5001",
                            "confidence": 0.5,  # low confidence: needs approval
                            "expectation": {"quantity": 800, "max_total_cost": 2000},
                        },
                    }
                ],
                lambda results: [],  # stop here; we only inspect the delivered result
            ],
        )

        runner.resume(run_id, approve=True, approver="buyer:ana")

        session, run, _ = load(factory, run_id)
        resumed = session.scalars(
            select(AgentStep)
            .where(AgentStep.run_id == run_id, AgentStep.name == "submit_purchase_order")
            .order_by(AgentStep.seq)
        ).all()[-1]
        check = resumed.payload["result"]["data"]["outcome_check"]
        assert check["status"] == "UNEXPECTED_OUTCOME"
        assert run.agent_state["deviations"] == {"PO-5001": 1}

    def test_only_paused_runs_can_be_resumed(self, factory):
        runner, run_id, _ = start(
            factory, "REC-1007", [READS, finalize("reject", 0, "on_hand", 1150)]
        )

        with pytest.raises(RunNotPausedError):
            runner.resume(run_id, approve=True, approver="buyer:ana")
