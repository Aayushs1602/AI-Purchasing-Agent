from app.agent.llm.base import ToolCall
from app.agent.tools import execute_tool
from app.models import AgentRun, Escalation, PurchaseOrder
from tests.agent.conftest import call, create, expect, gather_evidence, submit

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


def po_id_of(result):
    return result["data"]["purchase_order"]["po_id"]


class TestEvidenceGate:
    def test_actions_are_refused_until_evidence_is_gathered(self, make_ctx):
        ctx = make_ctx()
        call(ctx, "get_inventory")

        result = create(ctx, 800)

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

        data = create(ctx, 800)["data"]

        assert data["purchase_order"]["status"] == "draft"
        assert data["validation"]["feasible"] is True
        assert data["outcome_check"]["status"] == "AS_EXPECTED"
        assert ctx.session.get(PurchaseOrder, "PO-5001").run_id == ctx.run.id

    def test_expectation_is_required(self, make_ctx):
        ctx = make_ctx()
        gather_evidence(ctx)

        result = call(ctx, "create_purchase_order", quantity=800)

        assert result["error"]["code"] == "INVALID_ARGUMENTS"
        assert result["error"]["details"]["errors"][0]["field"] == "expectation"

    def test_wrong_expectation_is_reported_on_the_draft(self, make_ctx):
        ctx = make_ctx()
        gather_evidence(ctx)

        check = create(ctx, 800, cost=1500)["data"]["outcome_check"]

        assert check["status"] == "UNEXPECTED_OUTCOME"
        assert [d["field"] for d in check["differences"]] == ["total_cost"]

    def test_replayed_call_does_not_duplicate_the_po(self, make_ctx):
        ctx = make_ctx()
        gather_evidence(ctx)
        args = {"quantity": 800, "expectation": expect(800, 2000)}
        same_call = ToolCall(id="call-7", name="create_purchase_order", args=args)

        first = execute_tool(ctx, same_call)
        second = execute_tool(ctx, same_call)

        assert po_id_of(first) == po_id_of(second)

    def test_second_po_for_the_recommendation_is_refused(self, make_ctx):
        ctx = make_ctx()
        gather_evidence(ctx)
        create(ctx, 800)

        args = {"quantity": 600, "expectation": expect(600, 1500)}
        result = execute_tool(ctx, ToolCall(id="another", name="create_purchase_order", args=args))

        assert result["error"]["code"] == "LIVE_PO_EXISTS"

    def test_hard_violation_is_refused_with_the_limits(self, make_ctx):
        ctx = make_ctx("REC-1002")
        gather_evidence(ctx)

        result = create(ctx, 800)

        assert result["error"]["code"] == "HARD_CONSTRAINT_VIOLATED"
        details = result["error"]["details"]
        assert [v["code"] for v in details["hard_violations"]] == ["OVER_STORAGE_CAPACITY"]
        assert details["limits"]["max_storable_quantity"] == 780

    def test_excluding_an_unreliable_po(self, make_ctx):
        ctx = make_ctx("REC-1003")
        gather_evidence(ctx)

        data = create(ctx, 800, exclude_po_ids=["PO-2042"])["data"]

        assert data["purchase_order"]["assumptions"]["exclude_po_ids"] == ["PO-2042"]
        assert data["validation"]["requires_approval"] is True


class TestSubmit:
    def test_within_policy_goes_to_the_supplier_as_expected(self, make_ctx):
        ctx = make_ctx()
        gather_evidence(ctx)
        po_id = po_id_of(create(ctx, 800))

        data = submit(ctx, po_id, 800, 2000)["data"]

        assert data["outcome"] == "SENT_TO_SUPPLIER"
        assert data["purchase_order"]["status"] == "confirmed"
        assert data["supplier_ack"]["confirmed_quantity"] == 800
        assert "untrusted_text" in data["supplier_ack"]["message"]
        assert data["outcome_check"] == {
            "status": "AS_EXPECTED",
            "differences": [],
            "constraint_violations": [],
            "next_actions": [],
        }

    def test_high_value_waits_for_a_human(self, make_ctx):
        ctx = make_ctx("REC-1012")
        gather_evidence(ctx)
        po_id = po_id_of(create(ctx, 800, cost=3600))

        data = submit(ctx, po_id, 800, 3600)["data"]

        assert data["outcome"] == "PENDING_APPROVAL"
        assert [r["code"] for r in data["purchase_order"]["approval_reasons"]] == [
            "VALUE_ABOVE_AUTO_APPROVE_LIMIT"
        ]

    def test_price_increase_is_an_unexpected_outcome_that_can_be_fixed(self, make_ctx):
        ctx = make_ctx("REC-1010")
        gather_evidence(ctx)
        po_id = po_id_of(create(ctx, 800))

        sent = submit(ctx, po_id, 800, 2000)["data"]
        check = sent["outcome_check"]
        assert check["status"] == "UNEXPECTED_OUTCOME"
        assert [d["field"] for d in check["differences"]] == ["unit_cost", "total_cost"]
        assert check["constraint_violations"] == ["OVER_BUDGET"]
        assert not any("accept the deviation" in a for a in check["next_actions"])

        # What-if now treats a candidate as a replacement for the confirmed PO, not an extra
        # order on top of it (which would wrongly show no need and no budget).
        what_if = call(ctx, "evaluate_order_quantity", quantity=740)["data"]
        assert what_if["evaluated_as"].startswith(f"replacement for {po_id}")
        assert (what_if["feasible"], what_if["projection"]["net_need"]) == (True, 800)
        assert what_if["projection"]["order_value"] == 2146.0

        amended = call(
            ctx, "amend_purchase_order", po_id=po_id, quantity=740, expectation=expect(740, 2146)
        )["data"]
        assert amended["purchase_order"]["status"] == "draft"
        assert amended["outcome_check"]["status"] == "AS_EXPECTED"

        resent = submit(ctx, po_id, 740, 2146)["data"]
        assert resent["purchase_order"]["status"] == "confirmed"
        assert resent["outcome_check"]["status"] == "AS_EXPECTED"

    def test_partial_fill_is_an_unexpected_quantity(self, make_ctx):
        ctx = make_ctx("REC-1011")
        gather_evidence(ctx)
        po_id = po_id_of(create(ctx, 800))

        check = submit(ctx, po_id, 800, 2000)["data"]["outcome_check"]

        assert check["status"] == "UNEXPECTED_OUTCOME"
        assert check["differences"][0] == {
            "field": "quantity",
            "expected": 800,
            "actual": 500,
            "message": "PO has 500 units, expected 800.",
        }
        assert check["constraint_violations"] == []
        assert "accept the deviation and explain it in finalize_decision" in check["next_actions"]


class TestOwnership:
    def test_existing_pos_cannot_be_touched(self, make_ctx):
        ctx = make_ctx("REC-1002")
        gather_evidence(ctx)

        cancel = call(ctx, "cancel_purchase_order", po_id="PO-2041", reason="free up space")
        amend = call(
            ctx, "amend_purchase_order", po_id="PO-2041", quantity=100, expectation=expect(100, 250)
        )

        assert cancel["error"]["code"] == amend["error"]["code"] == "PERMISSION_DENIED"
        assert ctx.session.get(PurchaseOrder, "PO-2041").status == "confirmed"

    def test_own_po_can_be_cancelled(self, make_ctx):
        ctx = make_ctx()
        gather_evidence(ctx)
        po_id = po_id_of(create(ctx, 800))

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
        submit(ctx, po_id_of(create(ctx, 800)), 800, 2000)

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
