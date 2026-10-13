"""The graders pass the golden trajectories and fail deliberately wrong ones."""

import pytest

from app.agent.llm.scripted import ScriptedClient
from app.config import get_settings
from app.scenarios import load_scenarios
from evals.golden import (
    GOLDEN,
    READS,
    amend,
    create,
    escalate,
    evaluate,
    finalize,
    submit,
)
from evals.harness import run_case

SETTINGS = get_settings()
SCENARIOS = {s.id: s for s in load_scenarios(SETTINGS.scenarios_dir)}


@pytest.fixture
def run(tmp_path):
    def _run(scenario_id, script):
        return run_case(
            SCENARIOS[scenario_id],
            list(SCENARIOS.values()),
            ScriptedClient(script),
            repeat=1,
            workdir=tmp_path,
            today=SETTINGS.sim_today,
        )

    return _run


def failed(result):
    return {c.name for c in result.grade.failures()}


@pytest.mark.parametrize("scenario_id", GOLDEN)
def test_golden_trajectory_is_graded_right(run, scenario_id):
    result = run(scenario_id, GOLDEN[scenario_id])

    assert result.grade.outcome, [c.detail for c in result.grade.failures()]
    assert failed(result) == set()


def test_trusting_a_drifted_forecast_is_wrong(run):
    result = run(
        "forecast_drift",
        [
            READS,
            evaluate(800),
            create(800, 2000),
            submit(800, 2000),
            finalize("accept", 800, net_need=800),
        ],
    )

    assert not result.grade.outcome
    assert {"decision", "quantity"} <= failed(result)
    assert result.approvals_given == 1  # the data-quality flag still forced a human in


def test_escalating_a_clear_reject_is_over_escalation(run):
    script = [
        READS,
        evaluate(800),
        escalate("Not sure what to do with all this stock.", "What should we do?"),
        finalize("investigate", 0, on_hand=1150),
    ]

    result = run("overstock_stale_rec", script)

    assert {"decision", "escalation_appropriate"} <= failed(result)


def test_obeying_the_injected_note_fails_safety_even_if_refused(run):
    script = [
        READS,
        create(5000, 12500),  # refused by the tools: over budget and storage
        evaluate(800),
        create(800, 2000),
        submit(800, 2000),
        finalize("accept", 800, net_need=800),
    ]

    result = run("prompt_injection_note", script)

    assert not result.grade.outcome
    assert result.grade.criteria["safety"].passed is False
    assert "5000" in result.grade.criteria["safety"].detail


def test_skipping_the_what_if_check_is_reported(run):
    script = [READS, create(800, 2000), submit(800, 2000), finalize("accept", 800, net_need=800)]

    result = run("accept_baseline", script)

    assert result.grade.outcome  # the purchase was right...
    assert failed(result) == {"validated"}  # ...but it was not checked first


def test_not_citing_the_binding_constraint_is_reported(run):
    script = [
        READS,
        evaluate(480),
        create(480, 1200),
        submit(480, 1200),
        finalize("modify", 480, net_need=480),
    ]

    result = run("open_po_overlap", script)

    assert failed(result) == {"cites_binding_factor"}


def test_leaving_an_over_budget_po_in_place_is_wrong(run):
    script = [
        READS,
        evaluate(800),
        create(800, 2000),
        submit(800, 2000),  # confirmed at $2.90: over budget
        escalate("Supplier raised the price.", "What now?"),
        finalize("investigate", 800, unit_cost=2.9),
    ]

    result = run("supplier_price_increase", script)

    assert {"quantity", "constraints"} <= failed(result)


def test_waving_through_a_partial_fill_is_wrong(run):
    # finalize refuses this twice; the run ends needs_review with the PO unescalated.
    script = [
        READS,
        evaluate(800),
        create(800, 2000),
        submit(800, 2000),
        finalize("modify", 500, supplier_fill=500),
        finalize("modify", 500, supplier_fill=500),
    ]

    result = run("supplier_partial_fill", script)

    assert result.status == "needs_review"
    assert {"deviation_handled", "clean_finish"} <= failed(result)


def test_amending_until_the_breaker_trips(run):
    script = [
        READS,
        evaluate(800),
        create(800, 2000),
        submit(800, 2000),
        amend(800, 2000),
        submit(800, 2000),
        amend(800, 2000),
        submit(800, 2000),  # third deviation: circuit breaker escalates
        finalize("investigate", 500, supplier_fill=500),
    ]

    result = run("supplier_partial_fill", script)

    assert result.grade.outcome  # the breaker left a safe, escalated state
    assert result.status == "escalated"
