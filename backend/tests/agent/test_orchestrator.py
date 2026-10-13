"""The agent loop end to end, driven by a scripted LLM against the seeded scenarios."""

import pytest
from sqlalchemy import select

from app.agent.llm.scripted import ScriptedClient
from app.agent.orchestrator import AgentConfig, AgentRunner, RunConflictError
from app.db import make_session_factory
from app.models import AgentRun, AgentStep, Escalation, PurchaseOrder, Recommendation
from tests.agent.conftest import TODAY

READS = [
    {"name": name}
    for name in (
        "get_recommendation",
        "get_inventory",
        "get_storage_capacity",
        "get_demand_forecast",
        "get_sales_history",
        "get_open_purchase_orders",
        "get_supplier_terms",
        "get_budget",
    )
]


def created_po_id(results):
    return next(
        r.payload["data"]["purchase_order"]["po_id"]
        for r in results
        if r.name == "create_purchase_order"
    )


def expectation(quantity, cost):
    return {"quantity": quantity, "max_total_cost": cost}


def create_po(quantity, cost=None):
    cost = cost if cost is not None else quantity * 2.5
    return [
        {
            "name": "create_purchase_order",
            "args": {"quantity": quantity, "expectation": expectation(quantity, cost)},
        }
    ]


def submit(quantity, cost, confidence=0.9):
    return lambda results: [
        {
            "name": "submit_purchase_order",
            "args": {
                "po_id": created_po_id(results),
                "confidence": confidence,
                "expectation": expectation(quantity, cost),
            },
        }
    ]


def finalize(decision, quantity, factor="net_need", value=800):
    return [
        {
            "name": "finalize_decision",
            "args": {
                "decision": decision,
                "final_quantity": quantity,
                "rationale": f"Scripted decision: {decision} {quantity} units.",
                "key_factors": [{"factor": factor, "value": value, "impact": "drove it"}],
                "confidence": 0.85,
            },
        }
    ]


@pytest.fixture
def run_agent(engine, seeded):
    factory = make_session_factory(engine)

    def _run(rec_id, script, **config):
        client = ScriptedClient(script)
        runner = AgentRunner(factory, client, today=TODAY, config=AgentConfig(**config))
        run_id = runner.run(rec_id)
        session = factory()
        return session, session.get(AgentRun, run_id), client

    return _run


def steps(session, run_id, kind=None):
    query = select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.seq)
    if kind:
        query = query.where(AgentStep.kind == kind)
    return session.scalars(query).all()


def run_po(session, run_id):
    return session.scalar(select(PurchaseOrder).where(PurchaseOrder.run_id == run_id))


class TestHappyPaths:
    def test_accept(self, run_agent):
        session, run, client = run_agent(
            "REC-1001",
            [
                READS,
                [{"name": "evaluate_order_quantity", "args": {"quantity": 800}}],
                create_po(800),
                submit(800, 2000),
                finalize("accept", 800),
            ],
        )

        assert (run.status, run.decision, run.final_quantity) == ("completed", "accept", 800)
        assert run_po(session, run.id).status == "confirmed"
        assert run.llm_calls == 5
        assert len(run.evidence) == 7
        assert session.get(Recommendation, "REC-1001").status == "decided"
        assert "get_budget" in client.sessions[0].system_prompt
        assert [s.name for s in steps(session, run.id, "system")] == ["kickoff", "settled"]

    def test_modify_after_seeing_a_violation_waits_for_a_buyer(self, run_agent):
        session, run, _ = run_agent(
            "REC-1002",
            [
                READS,
                [
                    {"name": "evaluate_order_quantity", "args": {"quantity": 800}},
                    {"name": "evaluate_order_quantity", "args": {"quantity": 480}},
                ],
                create_po(480),
                submit(480, 1200),
            ],
        )

        # Every change to the recommendation pauses for a buyer (resume: test_approvals.py).
        po = run_po(session, run.id)
        assert run.status == "awaiting_approval"
        assert (po.status, po.outstanding_quantity) == ("pending_approval", 480)
        assert [r["code"] for r in po.approval_reasons] == ["QUANTITY_CHANGED"]
        first_eval = steps(session, run.id, "tool")[8]
        assert first_eval.payload["result"]["data"]["feasible"] is False

    def test_investigate_escalates(self, run_agent):
        session, run, _ = run_agent(
            "REC-1009",
            [
                READS,
                [
                    {
                        "name": "escalate_to_human",
                        "args": {
                            "reason": "MOQ 1,000 costs more than the $2,000 budget left.",
                            "questions": ["Raise the budget by $500?"],
                        },
                    }
                ],
                finalize("investigate", 0, factor="moq", value=1000),
            ],
        )

        assert (run.status, run.decision) == ("escalated", "investigate")
        assert session.scalar(select(Escalation).where(Escalation.run_id == run.id))
        assert run_po(session, run.id) is None

    def test_pending_approval_pauses_the_recommendation(self, run_agent):
        session, run, _ = run_agent(
            "REC-1012",
            [
                READS,
                create_po(800, 3600),
                submit(800, 3600),
                finalize("accept", 800, factor="order_value", value=3600),
            ],
        )

        assert run.status == "awaiting_approval"
        assert run.finished_at is None
        assert run_po(session, run.id).status == "pending_approval"
        assert session.get(Recommendation, "REC-1012").status == "awaiting_approval"

    def test_parallel_calls_with_finalize_run_finalize_last(self, run_agent):
        session, run, _ = run_agent(
            "REC-1007",
            [READS, [*finalize("reject", 0, factor="on_hand", value=1150), {"name": "get_budget"}]],
        )

        assert run.status == "completed"
        assert steps(session, run.id, "tool")[-1].name == "finalize_decision"


class TestGuardrails:
    def test_acting_before_investigating_is_refused_and_recorded(self, run_agent):
        session, run, client = run_agent(
            "REC-1001",
            [
                create_po(800),
                READS,
                create_po(800),
                submit(800, 2000),
                finalize("accept", 800),
            ],
        )

        first = steps(session, run.id, "tool")[0]
        assert first.payload["result"]["error"]["code"] == "EVIDENCE_MISSING"
        assert client.sessions[0].received_results[0][0].payload["ok"] is False
        assert run.status == "completed"

    def test_model_that_stops_talking_is_nudged_once_then_failed(self, run_agent):
        session, run, _ = run_agent("REC-1001", ["I think 800 is fine.", "Yes, 800."])

        assert run.status == "failed"
        assert "without calling finalize_decision" in run.error
        names = [s.name for s in steps(session, run.id, "guardrail")]
        assert names == ["nudge", "stopped_without_decision"]
        assert session.get(Recommendation, "REC-1001").status == "open"

    def test_call_budget_stops_a_looping_model(self, run_agent):
        session, run, _ = run_agent("REC-1001", [READS] * 10, max_llm_calls=3)

        assert run.status == "failed"
        assert run.llm_calls == 3
        assert "3 model calls" in run.error

    def test_llm_failure_hands_a_rules_proposal_to_a_human(self, run_agent):
        session, run, _ = run_agent("REC-1002", [READS])  # script runs out on the second call

        assert run.status == "needs_review"
        assert run.error.startswith("LLM unavailable")
        assert run.decision is None  # the agent did not decide; rules only propose
        assert (run.fallback_proposal["decision"], run.fallback_proposal["quantity"]) == (
            "modify",
            480,
        )
        escalation = session.get(Escalation, run.fallback_proposal["escalation_id"])
        assert escalation.reason.startswith("[degraded mode]")
        assert run_po(session, run.id) is None  # nothing was ordered on the proposal
        assert run.history  # conversation so far is kept for debugging
        assert run.finished_at is not None
        assert session.get(Recommendation, "REC-1002").status == "needs_review"

    def test_llm_failure_after_ordering_mentions_the_po(self, run_agent):
        session, run, _ = run_agent("REC-1001", [READS, create_po(800)])

        escalation = session.get(Escalation, run.fallback_proposal["escalation_id"])
        assert "PO-5001 from this run is draft" in escalation.reason

    def test_fallback_can_be_switched_off(self, run_agent):
        _, run, _ = run_agent("REC-1001", [READS], fallback_to_rules=False)

        assert run.status == "failed"
        assert run.fallback_proposal is None

    def test_only_one_active_run_per_recommendation(self, engine, seeded):
        runner = AgentRunner(make_session_factory(engine), ScriptedClient([]), today=TODAY)
        runner.create_run("REC-1001")

        with pytest.raises(RunConflictError):
            runner.create_run("REC-1001")

    def test_no_new_run_while_an_earlier_order_is_live(self, engine, run_agent):
        run_agent("REC-1001", [READS, create_po(800), submit(800, 2000), finalize("accept", 800)])
        runner = AgentRunner(make_session_factory(engine), ScriptedClient([]), today=TODAY)

        # A second review would count the first run's PO as stock on order and find no need.
        with pytest.raises(RunConflictError, match=r"already has PO-\d+ \(800 units, confirmed\)"):
            runner.create_run("REC-1001")
