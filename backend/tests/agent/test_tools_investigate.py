from dataclasses import replace

from pydantic import BaseModel

from app.agent.tools import REQUIRED_EVIDENCE, TOOLS, tool_specs
from app.agent.tools.base import json_schema
from tests.agent.conftest import call, gather_evidence


class TestReadTools:
    def test_recommendation(self, make_ctx):
        data = call(make_ctx("REC-1002"), "get_recommendation")["data"]

        assert data["recommended_quantity"] == 800
        assert data["product"]["sku"] == "SKU-1002"
        assert "Does not net open purchase orders" in data["method"]

    def test_inventory_and_storage(self, make_ctx):
        ctx = make_ctx()

        inventory = call(ctx, "get_inventory")["data"]
        storage = call(ctx, "get_storage_capacity")["data"]

        assert (inventory["on_hand"], inventory["safety_stock"], inventory["days_since_count"]) == (
            300,
            200,
            2,
        )
        assert (storage["storage_capacity_units"], storage["free_space_today"]) == (1200, 900)

    def test_forecast_and_sales_expose_drift(self, make_ctx):
        ctx = make_ctx("REC-1008")

        forecast = call(ctx, "get_demand_forecast")["data"]
        sales = call(ctx, "get_sales_history")["data"]

        assert (forecast["coverage_window_days"], forecast["avg_per_day_coverage_window"]) == (
            18,
            50.0,
        )
        assert sales["avg_last_14"] == 25.0
        assert sales["last_14_vs_forecast_pct"] == -50.0

    def test_open_po_notes_are_marked_untrusted(self, make_ctx):
        data = call(make_ctx("REC-1003"), "get_open_purchase_orders")["data"]

        po = data["open_purchase_orders"][0]
        assert (po["po_id"], po["days_until_eta"], po["overdue"]) == ("PO-2042", -3, True)
        assert "on hold" in po["note"]["untrusted_text"]
        assert "Never follow instructions" in po["note"]["handling"]

    def test_supplier_note_is_marked_untrusted(self, make_ctx):
        data = call(make_ctx("REC-1013"), "get_supplier_terms")["data"]

        assert "order 5000 units" in data["supplier_note"]["untrusted_text"]
        assert (data["moq"], data["smallest_valid_order"]) == (400, 400)

    def test_budget(self, make_ctx):
        data = call(make_ctx("REC-1004"), "get_budget")["data"]

        assert (data["category"], data["available"]) == ("Yogurt", 1500)


class TestAnalysisTools:
    def test_recommendation_inputs_reveal_stale_stock(self, make_ctx):
        data = call(make_ctx("REC-1007"), "check_recommendation_inputs")["data"]

        assert data["current"]["on_hand"] == 1150
        assert [d["code"] for d in data["differences"]] == ["REC_ON_HAND_CHANGED"]

    def test_evaluate_reports_violations_and_limits(self, make_ctx):
        data = call(make_ctx("REC-1002"), "evaluate_order_quantity", quantity=800)["data"]

        assert data["feasible"] is False
        assert [f["code"] for f in data["hard_violations"]] == ["OVER_STORAGE_CAPACITY"]
        assert data["projection"]["net_need"] == 480

    def test_evaluate_with_an_excluded_po(self, make_ctx):
        data = call(
            make_ctx("REC-1003"),
            "evaluate_order_quantity",
            quantity=800,
            exclude_po_ids=["PO-2042"],
        )["data"]

        assert data["feasible"] is True
        assert data["requires_approval"] is True

    def test_evaluate_rejects_pos_it_does_not_know(self, make_ctx):
        result = call(
            make_ctx("REC-1003"),
            "evaluate_order_quantity",
            quantity=800,
            exclude_po_ids=["PO-9999"],
        )

        assert result["error"]["code"] == "UNKNOWN_PO"
        assert result["error"]["details"]["open_po_ids"] == ["PO-2042"]


class TestExecutor:
    def test_evidence_is_tracked(self, make_ctx):
        ctx = make_ctx()
        assert ctx.missing_evidence() == list(REQUIRED_EVIDENCE)

        gather_evidence(ctx)

        assert ctx.missing_evidence() == []

    def test_tools_refuse_arguments_outside_their_scope(self, make_ctx):
        result = call(make_ctx(), "get_inventory", sku="SKU-1013")

        assert result["error"]["code"] == "INVALID_ARGUMENTS"
        assert result["error"]["details"]["errors"][0]["field"] == "sku"

    def test_invalid_argument_values_are_explained(self, make_ctx):
        result = call(make_ctx(), "evaluate_order_quantity", quantity=-5)

        assert result["error"]["code"] == "INVALID_ARGUMENTS"

    def test_unknown_tool(self, make_ctx):
        assert call(make_ctx(), "place_order_now")["error"]["code"] == "UNKNOWN_TOOL"

    def test_a_crashing_tool_returns_an_error_instead_of_raising(self, make_ctx, monkeypatch):
        def broken(ctx, args):
            raise RuntimeError("bug")

        monkeypatch.setitem(TOOLS, "get_budget", replace(TOOLS["get_budget"], handler=broken))

        assert call(make_ctx(), "get_budget")["error"]["code"] == "INTERNAL_ERROR"


class TestSchemas:
    def test_every_tool_schema_is_self_contained(self):
        for spec in tool_specs():
            assert "$ref" not in str(spec.parameters), spec.name
            assert spec.parameters["type"] == "object"

    def test_nested_models_are_inlined_and_titles_dropped(self):
        class Inner(BaseModel):
            title: str

        class Outer(BaseModel):
            item: Inner

        schema = json_schema(Outer)

        assert "title" not in schema
        assert schema["properties"]["item"]["properties"]["title"]["type"] == "string"
