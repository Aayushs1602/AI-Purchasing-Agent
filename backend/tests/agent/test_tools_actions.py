from app.agent.llm.base import ToolCall
from app.agent.tools import execute_tool
from app.models import AgentRun, Escalation, PurchaseOrder
from tests.agent.conftest import call, gather_evidence

FACTORS = [{"factor": "net_need", "value": 800, "impact": "need matches the recommendation"}]


def finalize_args(**overrides):
    args = {
        "decision": "accept",
        "final_quantity": 800,
        "rationale": "Need is exactly 800 and every constraint has headroom.",
        "key_factors": FACTORS,
        "confidence": 0.9,
    }
    args.update(overrides)
    return args


class TestEvidenceGate:
    def test_actions_are_refused_until_evidence_is_gathered(self, make_ctx):
        ctx = make_ctx()
        call(ctx, "get_inventory")

        result = call(ctx, "create_purchase_order", quantity=800)

        assert result["error"]["code"] == "EVIDENCE_MISSING"
        assert "budget" in result["error"]["details"]["missing"]
        assert "inventory" not in result["error"]["details"]["missing"]

    def test_finalize_is_gated_too(self, make_ctx):
        result = call(make_ctx(), "finalize_decision", **finalize_args())

        assert result["error"]["code"] == "EVIDENCE_MISSING"


class TestCreate:
    def test_valid_draft_is_tied_to_the_run(self, make_ctx):
        ctx = make_ctx()
        gather_evidence(ctx)

        data = call(ctx, "create_purchase_order", quantity=800)["data"]

        assert data["purchase_order"]["status"] == "draft"
        assert data["validation"]["feasible"] is True
        assert ctx.session.get(PurchaseOrder, "PO-5001").run_id == ctx.run.id

    def test_replayed_call_does_not_duplicate_the_po(self, make_ctx):
        ctx = make_ctx()
        gather_evidence(ctx)
        same_call = ToolCall(id="call-7", name="create_purchase_order", args={"quantity": 800})

        first = execute_tool(ctx, same_call)
        second = execute_tool(ctx, same_call)

        assert first["data"]["purchase_order"]["po_id"] == second["data"]["purchase_order"]["po_id"]

    def test_second_po_for_the_recommendation_is_refused(self, make_ctx):
        ctx = make_ctx()
        gather_evidence(ctx)
        call(ctx, "create_purchase_order", quantity=800)

        result = execute_tool(
            ctx, ToolCall(id="another", name="create_purchase_order", args={"quantity": 600})
        )

        assert result["error"]["code"] == "LIVE_PO_EXISTS"

    def test_hard_violation_is_refused_with_the_limits(self, make_ctx):
        ctx = make_ctx("REC-1002")
        gather_evidence(ctx)

        result = call(ctx, "create_purchase_order", quantity=800)

        assert result["error"]["code"] == "HARD_CONSTRAINT_VIOLATED"
        details = result["error"]["details"]
        assert [v["code"] for v in details["hard_violations"]] == ["OVER_STORAGE_CAPACITY"]
        assert details["limits"]["max_storable_quantity"] == 780

    def test_excluding_an_unreliable_po(self, make_ctx):
        ctx = make_ctx("REC-1003")
        gather_evidence(ctx)

        data = call(ctx, "create_purchase_order", quantity=800, exclude_po_ids=["PO-2042"])["data"]

        assert data["purchase_order"]["assumptions"]["exclude_po_ids"] == ["PO-2042"]
        assert data["validation"]["requires_approval"] is True


class TestSubmit:
    def test_within_policy_goes_to_the_supplier(self, make_ctx):
        ctx = make_ctx()
        gather_evidence(ctx)
        po_id = call(ctx, "create_purchase_order", quantity=800)["data"]["purchase_order"]["po_id"]

        data = call(ctx, "submit_purchase_order", po_id=po_id, confidence=0.9)["data"]

        assert data["outcome"] == "SENT_TO_SUPPLIER"
        assert data["purchase_order"]["status"] == "confirmed"
        assert data["supplier_ack"]["confirmed_quantity"] == 800
        assert "untrusted_text" in data["supplier_ack"]["message"]

    def test_high_value_waits_for_a_human(self, make_ctx):
        ctx = make_ctx("REC-1012")
        gather_evidence(ctx)
        po_id = call(ctx, "create_purchase_order", quantity=800)["data"]["purchase_order"]["po_id"]

        data = call(ctx, "submit_purchase_order", po_id=po_id, confidence=0.9)["data"]

        assert data["outcome"] == "PENDING_APPROVAL"
        assert [r["code"] for r in data["purchase_order"]["approval_reasons"]] == [
            "VALUE_ABOVE_AUTO_APPROVE_LIMIT"
        ]

    def test_price_increase_shows_up_in_the_revalidation_and_can_be_fixed(self, make_ctx):
        ctx = make_ctx("REC-1010")
        gather_evidence(ctx)
        po_id = call(ctx, "create_purchase_order", quantity=800)["data"]["purchase_order"]["po_id"]

        sent = call(ctx, "submit_purchase_order", po_id=po_id, confidence=0.9)["data"]
        assert sent["supplier_ack"]["confirmed_unit_cost"] == 2.9
        assert [v["code"] for v in sent["validation"]["hard_violations"]] == ["OVER_BUDGET"]

        amended = call(ctx, "amend_purchase_order", po_id=po_id, quantity=740)["data"]
        assert amended["purchase_order"]["status"] == "draft"

        resent = call(ctx, "submit_purchase_order", po_id=po_id, confidence=0.8)["data"]
        assert resent["purchase_order"]["status"] == "confirmed"
        assert resent["validation"]["feasible"] is True


class TestOwnership:
    def test_existing_pos_cannot_be_touched(self, make_ctx):
        ctx = make_ctx("REC-1002")
        gather_evidence(ctx)

        cancel = call(ctx, "cancel_purchase_order", po_id="PO-2041", reason="free up space")
        amend = call(ctx, "amend_purchase_order", po_id="PO-2041", quantity=100)

        assert cancel["error"]["code"] == amend["error"]["code"] == "PERMISSION_DENIED"
        assert ctx.session.get(PurchaseOrder, "PO-2041").status == "confirmed"

    def test_own_po_can_be_cancelled(self, make_ctx):
        ctx = make_ctx()
        gather_evidence(ctx)
        po_id = call(ctx, "create_purchase_order", quantity=800)["data"]["purchase_order"]["po_id"]

        data = call(ctx, "cancel_purchase_order", po_id=po_id, reason="changed my mind")["data"]

        assert data["purchase_order"]["status"] == "cancelled"


class TestEscalateAndFinalize:
    def test_escalation_is_recorded(self, make_ctx):
        ctx = make_ctx("REC-1009")
        gather_evidence(ctx)

        data = call(
            ctx,
            "escalate_to_human",
            reason="The MOQ of 1,000 costs more than the $2,000 left in the budget.",
            questions=["Can the budget be raised by $500?"],
            options=["Raise budget", "Negotiate MOQ", "Use another supplier"],
            severity="high",
        )["data"]

        escalation = ctx.session.get(Escalation, data["escalation_id"])
        assert (escalation.recommendation_id, escalation.severity) == ("REC-1009", "high")
        assert len(escalation.options) == 3

    def test_finalize_records_the_decision_once(self, make_ctx):
        ctx = make_ctx()
        gather_evidence(ctx)

        assert call(ctx, "finalize_decision", **finalize_args())["data"]["recorded"]
        again = call(ctx, "finalize_decision", **finalize_args())

        run = ctx.session.get(AgentRun, ctx.run.id)
        assert (run.decision, run.final_quantity, run.confidence) == ("accept", 800, 0.9)
        assert run.key_factors[0]["factor"] == "net_need"
        assert again["error"]["code"] == "ALREADY_FINALIZED"

    def test_key_factors_must_come_from_the_fixed_list(self, make_ctx):
        ctx = make_ctx()
        gather_evidence(ctx)
        bad = [{"factor": "vibes", "value": 1, "impact": "felt right"}]

        result = call(ctx, "finalize_decision", **finalize_args(key_factors=bad))

        assert result["error"]["code"] == "INVALID_ARGUMENTS"
