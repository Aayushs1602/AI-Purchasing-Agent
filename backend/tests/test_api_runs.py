"""Agent runs over HTTP, with a scripted LLM and an inline executor (deterministic)."""

import pytest
from fastapi.testclient import TestClient

from app.agent.llm.scripted import ScriptedClient
from app.background import InlineExecutor
from app.config import Settings
from app.main import create_app
from tests.agent.test_orchestrator import READS, create_po, finalize, submit

ACCEPT = [
    READS,
    [{"name": "evaluate_order_quantity", "args": {"quantity": 800}}],
    create_po(800),
    submit(800, 2000),
    finalize("accept", 800),
]
HIGH_VALUE = [READS, create_po(800, 3600), submit(800, 3600)]


@pytest.fixture
def make_client(tmp_path):
    def _make(script=None, **overrides):
        settings = Settings(
            database_url=f"sqlite:///{(tmp_path / 'runs.db').as_posix()}", **overrides
        )
        llm = ScriptedClient(script) if script is not None else None
        return TestClient(create_app(settings, llm=llm, executor=InlineExecutor()))

    return _make


def start(client, rec_id):
    response = client.post(f"/api/recommendations/{rec_id}/runs")
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def test_run_executes_and_can_be_inspected(make_client):
    with make_client(ACCEPT) as client:
        run_id = start(client, "REC-1001")

        run = client.get(f"/api/runs/{run_id}").json()

    assert (run["status"], run["decision"], run["final_quantity"]) == ("completed", "accept", 800)
    assert run["llm_calls"] == 5
    assert len(run["evidence"]) == 7
    assert [po["status"] for po in run["purchase_orders"]] == ["confirmed"]
    assert run["steps"][0]["name"] == "kickoff"
    assert run["steps"][-1]["name"] == "settled"


def test_polling_returns_only_new_steps(make_client):
    with make_client(ACCEPT) as client:
        run_id = start(client, "REC-1001")
        all_steps = client.get(f"/api/runs/{run_id}").json()["steps"]

        newer = client.get(f"/api/runs/{run_id}", params={"steps_after": 20}).json()["steps"]

    assert [s["seq"] for s in newer] == [s["seq"] for s in all_steps if s["seq"] > 20]


def test_recommendations_show_their_latest_run(make_client):
    with make_client(ACCEPT) as client:
        run_id = start(client, "REC-1001")

        recs = {r["id"]: r for r in client.get("/api/recommendations").json()}

    assert recs["REC-1001"]["status"] == "decided"
    assert recs["REC-1001"]["latest_run"]["id"] == run_id
    assert recs["REC-1002"]["latest_run"] is None


def test_runs_can_be_listed_by_recommendation(make_client):
    with make_client(ACCEPT) as client:
        run_id = start(client, "REC-1001")

        listed = client.get("/api/runs", params={"recommendation_id": "REC-1001"}).json()

    assert [r["id"] for r in listed] == [run_id]


def test_paused_run_reports_what_it_waits_for(make_client):
    with make_client(HIGH_VALUE) as client:
        run_id = start(client, "REC-1012")

        run = client.get(f"/api/runs/{run_id}").json()

    assert run["status"] == "awaiting_approval"
    assert run["paused_for"]["po_id"] == "PO-5001"
    assert run["paused_for"]["reasons"][0]["code"] == "VALUE_ABOVE_AUTO_APPROVE_LIMIT"


def test_second_run_while_one_is_active_is_a_conflict(make_client):
    with make_client(HIGH_VALUE) as client:
        start(client, "REC-1012")  # pauses for approval: still active

        response = client.post("/api/recommendations/REC-1012/runs")

    assert response.status_code == 409


def test_unknown_recommendation_or_run(make_client):
    with make_client([]) as client:
        assert client.post("/api/recommendations/REC-404/runs").status_code == 404
        assert client.get("/api/runs/RUN-404").status_code == 404


def test_without_an_llm_runs_are_disabled_but_data_still_works(make_client):
    with make_client(gemini_api_key="") as client:
        response = client.post("/api/recommendations/REC-1001/runs")
        health = client.get("/api/health").json()

        assert response.status_code == 503
        assert "GEMINI_API_KEY" in response.json()["detail"]
        assert health["agent"] == "disabled"
        assert client.get("/api/recommendations").status_code == 200
