"""YAML -> database -> engine: each seeded world produces the numbers it was designed around."""

import pytest

from app.config import get_settings
from app.engine.reference_policy import decide
from app.models import Recommendation
from app.scenarios import load_scenarios
from app.seed import seed_scenarios
from app.services.context import ContextNotFoundError, build_context, context_for_recommendation

TODAY = get_settings().sim_today

# What plain rules decide for each scenario (not the agent's expected answer).
RULES_BASELINE = {
    "accept_baseline": ("accept", 800),
    "open_po_overlap": ("modify", 480),
    "open_po_on_hold": ("modify", 480),  # rules count the held PO
    "budget_cap": ("modify", 600),
    "storage_cap": ("modify", 700),
    "moq_roundup": ("modify", 1000),
    "overstock_stale_rec": ("reject", 0),
    "forecast_drift": ("accept", 800),  # rules trust the forecast
    "infeasible_moq_budget": ("investigate", 0),
    "supplier_price_increase": ("accept", 800),  # price rise only shows up after submit
    "supplier_partial_fill": ("accept", 800),
    "high_value_approval": ("accept", 800),
    "prompt_injection_note": ("accept", 800),
}


@pytest.fixture(scope="module")
def scenarios():
    return load_scenarios(get_settings().scenarios_dir)


@pytest.fixture
def seeded(session, scenarios):
    seed_scenarios(session, scenarios, TODAY)
    return session


def test_every_scenario_has_a_rules_baseline(scenarios):
    assert {s.id for s in scenarios} == set(RULES_BASELINE)


@pytest.mark.parametrize("scenario_id", RULES_BASELINE)
def test_rules_baseline_from_seeded_data(seeded, scenario_id):
    rec = seeded.query(Recommendation).filter_by(scenario=scenario_id).one()

    result = decide(context_for_recommendation(seeded, rec, TODAY), rec.quantity)

    assert (result.decision, result.quantity) == RULES_BASELINE[scenario_id]


def test_context_contents_for_baseline(seeded):
    ctx = build_context(
        seeded, sku="SKU-1001", node_id="DS-BOG-01", supplier_id="SUP-ANDES", today=TODAY
    )

    assert ctx.on_hand == 300
    assert ctx.budget_available == 5000
    assert len(ctx.daily_forecast) == 60
    assert len(ctx.recent_daily_sales) == 28
    assert ctx.supplier.moq == 400


def test_missing_terms_raise(seeded):
    with pytest.raises(ContextNotFoundError, match="supplier terms"):
        build_context(
            seeded, sku="SKU-1001", node_id="DS-BOG-01", supplier_id="SUP-FRIO", today=TODAY
        )
