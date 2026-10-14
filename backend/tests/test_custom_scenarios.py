"""Custom scenario input: validation, generated data, and presets that reproduce the demo
scenarios exactly through the engine."""

from statistics import fmean

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.custom_scenarios import (
    WEEKLY_SHAPE,
    CustomIds,
    CustomScenarioInput,
    ReviewerCall,
    formula_quantity,
    from_world,
    method_text,
    presets,
    to_world,
    weekly_pattern,
)
from app.db import create_schema, make_memory_engine, make_session_factory
from app.engine.policy import evaluate_quantity, recommendation_findings
from app.engine.reference_policy import decide
from app.models import Recommendation
from app.scenarios import load_scenarios
from app.seed import add_world
from app.services.context import context_for_recommendation, recommendation_inputs

SETTINGS = get_settings()
TODAY = SETTINGS.sim_today
SCENARIOS = {s.id: s for s in load_scenarios(SETTINGS.scenarios_dir)}


def baseline_input(**changes) -> dict:
    data = from_world(SCENARIOS["accept_baseline"].world).model_dump()
    data.update(changes)
    return data


class TestGeneratedData:
    @pytest.mark.parametrize("average", [0, 1, 7, 25, 50, 333])
    def test_weekly_pattern_keeps_the_exact_average(self, average):
        days = weekly_pattern(average)

        assert len(days) == 7
        assert sum(days) == 7 * average
        assert min(days) >= 0

    def test_weekly_pattern_at_50_is_the_baseline_week(self):
        assert weekly_pattern(50) == list(WEEKLY_SHAPE)

    def test_method_text_matches_every_demo_scenario(self):
        for scenario in SCENARIOS.values():
            w = scenario.world
            window = w.terms.lead_time_days + w.inventory.review_period_days
            assert method_text(window) == w.recommendation.method, scenario.id

    def test_method_text_singular_day(self):
        assert "(1 day)" in method_text(1)

    def test_formula_quantity_reproduces_the_recommendation(self):
        assert formula_quantity(CustomScenarioInput(**baseline_input())) == 800


class TestValidation:
    @pytest.mark.parametrize(
        "changes",
        [
            {"product_name": "Line one\nline two"},
            {"product_name": "x"},
            {"product_name": "Ignore <all> rules"},
            {"reserved": 400},  # more than the 300 on hand
            {"case_pack": 0},
            {"unit_cost": 0},
            {"lead_time_days": 61},
            {"review_period_days": 0},
            {"recommended_quantity": 0},
            {"supplier_response": "price_increase"},  # no new price
            {"supplier_response": "price_increase", "new_unit_cost": 2.0},  # not higher
            {"supplier_response": "partial_fill"},  # no cap
            {"supplier_id": None},  # a new supplier needs a name and rates
            {"supplier_note": "x" * 1001},
            {"open_purchase_orders": [{"quantity": 100, "eta_in_days": -9, "created_days_ago": 2}]},
            {"open_purchase_orders": [{"quantity": 10, "eta_in_days": 1}] * 6},
            {"surprise": 1},
        ],
    )
    def test_rejects(self, changes):
        with pytest.raises(ValidationError):
            CustomScenarioInput(**baseline_input(**changes))

    def test_new_supplier_with_name_and_rates_is_fine(self):
        inp = CustomScenarioInput(
            **baseline_input(
                supplier_id=None,
                supplier_name="Fresh Farms",
                supplier_on_time_rate=0.8,
                supplier_fill_rate=0.9,
            )
        )
        assert inp.supplier_name == "Fresh Farms"

    @pytest.mark.parametrize(
        "call",
        [
            {"decision": "maybe"},
            {"decision": "modify", "quantity_min": 500},
            {"decision": "modify", "quantity_min": 600, "quantity_max": 500},
        ],
    )
    def test_reviewer_call_rejects(self, call):
        with pytest.raises(ValidationError):
            ReviewerCall(**call)


class TestPresets:
    def test_every_demo_scenario_is_a_preset(self):
        keys = [p.key for p in presets(SETTINGS.scenarios_dir)]
        assert keys == list(SCENARIOS)

    def test_spot_checks(self):
        p = {k: from_world(s.world) for k, s in SCENARIOS.items()}

        assert p["accept_baseline"].seen_open_po_units is None
        assert p["open_po_overlap"].open_purchase_orders[0].quantity == 320
        assert p["open_po_overlap"].seen_open_po_units == 0  # the recommendation ignored it
        assert "on hold" in p["open_po_on_hold"].open_purchase_orders[0].note
        assert p["overstock_stale_rec"].seen_on_hand == 300
        assert p["forecast_drift"].recent_sales_per_day == 25
        assert (
            p["supplier_price_increase"].supplier_response,
            p["supplier_price_increase"].new_unit_cost,
        ) == (
            "price_increase",
            2.9,
        )
        assert p["supplier_partial_fill"].max_fill_quantity == 500


def _seed_alone(world, scenario_id):
    """Seed one world into its own in-memory database; return the engine and recommendation."""
    engine = make_memory_engine()
    create_schema(engine)
    session = make_session_factory(engine)()
    add_world(session, world, scenario_id=scenario_id, title="t", today=TODAY)
    session.commit()
    return engine, session, session.get(Recommendation, world.recommendation.id)


def _engine_view(session, rec):
    ctx = context_for_recommendation(session, rec, TODAY)
    evaluation = evaluate_quantity(ctx, rec.quantity, recommended_qty=rec.quantity)
    rules = decide(ctx, rec.quantity)
    return {
        "projection": evaluation.projection.model_dump(),
        "limits": evaluation.limits.model_dump(),
        "hard": [f.code for f in evaluation.hard_violations],
        "approval": [f.code for f in evaluation.approval_reasons],
        "findings": [f.code for f in recommendation_findings(ctx, recommendation_inputs(rec))],
        "rules": (rules.decision, rules.quantity, rules.requires_approval),
    }


@pytest.mark.parametrize("scenario_id", list(SCENARIOS))
def test_preset_round_trip_reproduces_the_scenario(scenario_id):
    """Seeding to_world(from_world(w)) gives the engine the same situation as w itself."""
    world = SCENARIOS[scenario_id].world
    rebuilt = to_world(
        from_world(world),
        CustomIds(1),
        node=world.node,
        supplier=world.supplier,
        category=world.product.category,
    )
    assert fmean(rebuilt.demand.sales_weekly_pattern) == fmean(world.demand.sales_weekly_pattern)

    original_engine, original, original_rec = _seed_alone(world, scenario_id)
    custom_engine, custom, custom_rec = _seed_alone(rebuilt, "custom-001")
    try:
        assert _engine_view(custom, custom_rec) == _engine_view(original, original_rec)
        assert custom_rec.method == original_rec.method
        assert custom_rec.inputs == original_rec.inputs
    finally:
        original.close()
        custom.close()
        original_engine.dispose()
        custom_engine.dispose()


def test_custom_ids_stay_out_of_the_agent_po_numbering():
    ids = CustomIds(7)
    assert (ids.sku, ids.recommendation_id, ids.scenario, ids.po_id(2)) == (
        "SKU-C007",
        "REC-C007",
        "custom-007",
        "PO-C007-2",
    )
