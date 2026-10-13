from datetime import timedelta

import pytest

from app.engine.models import Assumptions
from app.engine.policy import (
    RecommendationInputs,
    data_quality_flags,
    evaluate_quantity,
    order_limits,
    recommendation_findings,
    submission_approval_reasons,
)
from tests.engine.factories import RECOMMENDED_QTY, TODAY, make_context, open_po


def codes(findings):
    return {f.code for f in findings}


class TestBaseline:
    def test_800_is_feasible_with_nothing_to_flag(self):
        ev = evaluate_quantity(make_context(), 800, recommended_qty=RECOMMENDED_QTY)

        assert ev.feasible
        assert not ev.requires_approval
        assert ev.hard_violations == ev.soft_warnings == ev.data_quality_flags == []

    def test_limits(self):
        limits = order_limits(make_context())

        assert limits.min_valid_quantity == 400
        assert limits.max_affordable_quantity == 2000  # $5,000 / $2.50
        assert limits.max_storable_quantity == 1100  # 1,200 capacity - 100 on hand at arrival
        assert limits.max_feasible_quantity == 1100


class TestHardViolations:
    @pytest.mark.parametrize(
        ("ctx_kwargs", "qty", "expected"),
        [
            ({}, 390, {"BELOW_MOQ", "NOT_CASE_MULTIPLE"}),
            ({}, 810, {"NOT_CASE_MULTIPLE"}),
            ({}, 0, {"QTY_NOT_POSITIVE"}),
            ({"budget_available": 1500}, 800, {"OVER_BUDGET"}),
            ({"storage_capacity": 800}, 800, {"OVER_STORAGE_CAPACITY"}),
        ],
    )
    def test_hard_violations(self, ctx_kwargs, qty, expected):
        ev = evaluate_quantity(make_context(**ctx_kwargs), qty)

        assert codes(ev.hard_violations) == expected
        assert not ev.feasible

    def test_open_po_overlap_makes_800_overflow_storage(self):
        ctx = make_context(open_purchase_orders=[open_po(320, eta_in_days=2)])

        assert "OVER_STORAGE_CAPACITY" in codes(evaluate_quantity(ctx, 800).hard_violations)
        assert evaluate_quantity(ctx, 480).feasible

    def test_budget_exactly_spent_is_allowed(self):
        assert evaluate_quantity(make_context(budget_available=2000), 800).feasible


class TestSoftWarningsAndApproval:
    def test_stale_recommendation_overstocks(self):
        ev = evaluate_quantity(make_context(on_hand=1150), 800)

        assert "OVER_STORAGE_CAPACITY" in codes(ev.hard_violations)
        assert {"OVERSTOCK", "NO_NET_NEED"} <= codes(ev.soft_warnings)

    def test_budget_capped_order_is_under_coverage(self):
        ev = evaluate_quantity(make_context(budget_available=1500), 600)

        assert ev.feasible
        assert codes(ev.soft_warnings) == {"UNDER_COVERAGE"}  # 600 < 80% of 800
        assert ev.requires_approval

    def test_storage_capped_order_within_tolerance_needs_no_approval(self):
        ev = evaluate_quantity(make_context(storage_capacity=800), 700)

        assert ev.feasible
        assert not ev.requires_approval  # 700 >= 640

    @pytest.mark.parametrize(("qty", "flagged"), [(960, False), (980, True), (1000, True)])
    def test_increase_over_recommendation_threshold(self, qty, flagged):
        ctx = make_context(supplier={"moq": 400})
        ev = evaluate_quantity(ctx, qty, recommended_qty=RECOMMENDED_QTY)

        assert ("ABOVE_RECOMMENDATION" in codes(ev.soft_warnings)) is flagged

    def test_moq_roundup_needs_approval(self):
        ctx = make_context(supplier={"moq": 1000})
        ev = evaluate_quantity(ctx, 1000, recommended_qty=RECOMMENDED_QTY)

        assert ev.feasible
        assert codes(ev.approval_reasons) == {"ABOVE_RECOMMENDATION"}

    def test_high_value_order_needs_approval(self):
        ev = evaluate_quantity(make_context(supplier={"unit_cost": 4.50}), 800)

        assert ev.feasible
        assert codes(ev.approval_reasons) == {"VALUE_ABOVE_AUTO_APPROVE_LIMIT"}

    def test_assumption_override_needs_approval(self):
        ctx = make_context(open_purchase_orders=[open_po(320, eta_in_days=-3, po_id="PO-HOLD")])
        ev = evaluate_quantity(ctx, 800, assumptions=Assumptions(exclude_po_ids=["PO-HOLD"]))

        assert ev.feasible
        assert "ASSUMPTION_OVERRIDE" in codes(ev.soft_warnings)
        assert "OVERDUE_OPEN_PO" in codes(ev.data_quality_flags)
        assert ev.requires_approval

    def test_low_confidence_adds_approval_reason(self):
        ev = evaluate_quantity(make_context(), 800)

        assert submission_approval_reasons(ev, confidence=0.9) == []
        assert codes(submission_approval_reasons(ev, confidence=0.5)) == {"LOW_CONFIDENCE"}

    def test_stockout_before_arrival_is_informational(self):
        ev = evaluate_quantity(make_context(on_hand=100), 1000)

        assert codes(ev.info) == {"STOCKOUT_BEFORE_ARRIVAL"}  # 100 units last 2 days, lead 4


class TestDataQuality:
    def test_clean_baseline(self):
        assert data_quality_flags(make_context()) == []

    def test_forecast_drift(self):
        ctx = make_context(recent_daily_sales=[50.0] * 14 + [25.0] * 14)

        flags = data_quality_flags(ctx)
        assert codes(flags) == {"FORECAST_DRIFT"}
        assert flags[0].observed == 25.0

    def test_small_drift_is_tolerated(self):
        ctx = make_context(recent_daily_sales=[40.0] * 28)  # -20%

        assert data_quality_flags(ctx) == []

    def test_overdue_po(self):
        ctx = make_context(open_purchase_orders=[open_po(320, eta_in_days=-3)])

        assert codes(data_quality_flags(ctx)) == {"OVERDUE_OPEN_PO"}

    def test_stale_inventory_count(self):
        ctx = make_context(inventory_counted_at=TODAY - timedelta(days=30))

        assert codes(data_quality_flags(ctx)) == {"STALE_INVENTORY_COUNT"}

    def test_missing_sales_history(self):
        assert codes(data_quality_flags(make_context(recent_daily_sales=[]))) == {
            "NO_SALES_HISTORY"
        }


class TestRecommendationFindings:
    def test_matching_inputs_produce_nothing(self):
        rec = RecommendationInputs(
            quantity=800, on_hand=300, open_po_units=0, daily_demand_rate=50.0
        )

        assert recommendation_findings(make_context(), rec) == []

    def test_stale_on_hand_and_ignored_open_po(self):
        ctx = make_context(on_hand=1150, open_purchase_orders=[open_po(320, eta_in_days=2)])
        rec = RecommendationInputs(quantity=800, on_hand=300, open_po_units=0)

        findings = recommendation_findings(ctx, rec)
        assert codes(findings) == {"REC_ON_HAND_CHANGED", "REC_OPEN_POS_CHANGED"}
        assert all(f.severity == "info" for f in findings)
