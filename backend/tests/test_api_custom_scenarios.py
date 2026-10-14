"""Custom scenarios over HTTP: presets, live preview, create and run."""

import json

import pytest
from fastapi.testclient import TestClient

from app.agent.llm.recording import ReplayClient
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
MARKER = "ZX-CALL-MARKER"


@pytest.fixture
def make_client(tmp_path):
    def _make(llm=None, **overrides):
        overrides.setdefault("gemini_api_key", "")  # never build a real model client in tests
        settings = Settings(
            database_url=f"sqlite:///{(tmp_path / 'custom.db').as_posix()}", **overrides
        )
        return TestClient(create_app(settings, llm=llm, executor=InlineExecutor()))

    return _make


@pytest.fixture
def client(make_client):
    with make_client() as c:
        yield c


def preset(client, key):
    options = client.get("/api/custom-scenarios/presets").json()
    return next(p["input"] for p in options["presets"] if p["key"] == key)


def preview(client, data):
    response = client.post("/api/custom-scenarios/preview", json=data)
    assert response.status_code == 200, response.text
    return response.json()


class TestPresets:
    def test_lists_every_demo_scenario_and_the_suppliers(self, client):
        options = client.get("/api/custom-scenarios/presets").json()

        assert len(options["presets"]) == 14
        assert options["presets"][0]["key"] == "accept_baseline"
        assert "expect" not in options["presets"][0]  # labels stay with the graders
        assert len(options["suppliers"]) == 7
        assert options["today"] == "2026-10-01"


class TestPreview:
    def test_baseline(self, client):
        result = preview(client, preset(client, "accept_baseline"))

        assert result["problems"] == []
        p = result["preview"]
        assert p["evaluation"]["feasible"] is True
        assert p["evaluation"]["projection"]["net_need"] == 800
        assert p["evaluation"]["approval_reasons"] == []
        assert (p["rules"]["decision"], p["rules"]["quantity"]) == ("accept", 800)
        assert p["formula_quantity"] == 800

    def test_open_po_overlap(self, client):
        p = preview(client, preset(client, "open_po_overlap"))["preview"]

        assert p["evaluation"]["projection"]["net_need"] == 480
        assert [f["code"] for f in p["evaluation"]["hard_violations"]] == ["OVER_STORAGE_CAPACITY"]
        assert [f["code"] for f in p["recommendation_findings"]] == ["REC_OPEN_POS_CHANGED"]
        assert (p["rules"]["decision"], p["rules"]["quantity"]) == ("modify", 480)

    def test_edits_change_the_answer(self, client):
        data = preset(client, "accept_baseline") | {"budget_committed": 18500}  # $1,500 left

        p = preview(client, data)["preview"]

        assert [f["code"] for f in p["evaluation"]["hard_violations"]] == ["OVER_BUDGET"]
        assert (p["rules"]["decision"], p["rules"]["quantity"]) == ("modify", 600)

    def test_invalid_input_comes_back_as_problems(self, client):
        data = preset(client, "accept_baseline") | {"case_pack": 0, "reserved": 999}

        result = preview(client, data)

        assert result["preview"] is None
        fields = {p["field"] for p in result["problems"]}
        assert "case_pack" in fields

    def test_unknown_supplier_is_a_problem(self, client):
        data = preset(client, "accept_baseline") | {"supplier_id": "SUP-NOPE"}

        assert preview(client, data)["problems"] == [
            {"field": "supplier_id", "message": "Unknown supplier"}
        ]

    def test_new_supplier(self, client):
        data = preset(client, "accept_baseline") | {
            "supplier_id": None,
            "supplier_name": "Fresh Farms",
            "supplier_on_time_rate": 0.7,
            "supplier_fill_rate": 0.9,
        }

        assert preview(client, data)["problems"] == []

    def test_preview_does_not_touch_the_database(self, client):
        before = client.get("/api/recommendations").json()
        preview(client, preset(client, "open_po_overlap"))

        assert client.get("/api/recommendations").json() == before
        assert len(before) == 14


def create(client, data, **body):
    return client.post("/api/custom-scenarios", json={"input": data, **body})


class TestCreateAndRun:
    def test_runs_the_agent_and_compares_with_your_call(self, make_client):
        llm = ScriptedClient(ACCEPT)
        with make_client(llm=llm) as client:
            data = preset(client, "accept_baseline")
            response = create(
                client,
                data,
                title="My water test",
                reviewer_call={"decision": "accept", "quantity_min": 800, "quantity_max": 800},
            )
            assert response.status_code == 201, response.text
            created = response.json()
            run = client.get(f"/api/runs/{created['run_id']}").json()
            recs = {r["id"]: r for r in client.get("/api/recommendations").json()}

        assert created["recommendation_id"] == "REC-C001"
        assert (run["status"], run["decision"], run["final_quantity"]) == (
            "completed",
            "accept",
            800,
        )
        assert run["reviewer_call"]["agrees"] is True
        assert recs["REC-C001"]["custom"] is True
        assert recs["REC-C001"]["title"] == "My water test"
        assert recs["REC-1001"]["custom"] is False
        # The demo's budget line for this category exists, so the custom one is numbered.
        assert recs["REC-C001"]["category"] == f"{data['category']} 2"

    def test_a_different_call_shows_as_disagreement(self, make_client):
        with make_client(llm=ScriptedClient(ACCEPT)) as client:
            created = create(
                client, preset(client, "accept_baseline"), reviewer_call={"decision": "reject"}
            ).json()
            verdict = client.get(f"/api/runs/{created['run_id']}").json()["reviewer_call"]

        assert (verdict["decision_match"], verdict["quantity_match"], verdict["agrees"]) == (
            False,
            None,
            False,
        )

    def test_your_call_never_reaches_the_agent(self, make_client):
        llm = ScriptedClient(ACCEPT)
        with make_client(llm=llm) as client:
            created = create(
                client,
                preset(client, "accept_baseline"),
                reviewer_call={"decision": "reject", "note": MARKER},
            ).json()
            steps = client.get(f"/api/runs/{created['run_id']}").json()["steps"]

        seen_by_model = [
            {
                "system": s.system_prompt,
                "history": s.export_history(),
                "results": [[r.payload for r in batch] for batch in s.received_results],
            }
            for s in llm.sessions
        ]
        assert MARKER not in json.dumps(seen_by_model, default=str)
        assert MARKER not in json.dumps(steps)

    def test_create_only_and_duplicate(self, make_client):
        with make_client(llm=ScriptedClient([])) as client:
            data = preset(client, "open_po_on_hold")
            created = create(client, data, run=False, preset="open_po_on_hold").json()
            stored = client.get(f"/api/custom-scenarios/{created['recommendation_id']}").json()
            missing = client.get("/api/custom-scenarios/REC-1001")

        assert created["run_id"] is None
        assert stored["input"] == data
        assert stored["preset"] == "open_po_on_hold"
        assert missing.status_code == 404

    def test_invalid_form_is_refused(self, make_client):
        with make_client(llm=ScriptedClient([])) as client:
            data = preset(client, "accept_baseline") | {"moq": 0}
            response = create(client, data, run=False)

        assert response.status_code == 422
        assert "moq" in response.json()["detail"]

    def test_reset_removes_custom_scenarios(self, make_client):
        with make_client(llm=ScriptedClient([])) as client:
            create(client, preset(client, "accept_baseline"), run=False)
            assert len(client.get("/api/recommendations").json()) == 15

            client.post("/api/admin/reset")

            assert len(client.get("/api/recommendations").json()) == 14


class TestLimits:
    def test_disabled(self, make_client):
        with make_client(allow_custom_scenarios=False) as client:
            response = create(client, preset(client, "accept_baseline"), run=False)

        assert response.status_code == 403

    def test_cap(self, make_client):
        with make_client(max_custom_scenarios=1) as client:
            data = preset(client, "accept_baseline")
            assert create(client, data, run=False).status_code == 201

            response = create(client, data, run=False)

        assert response.status_code == 409
        assert "limit is 1" in response.json()["detail"]

    def test_replay_mode_cannot_run_a_custom_scenario(self, make_client, tmp_path):
        with make_client(llm=ReplayClient(tmp_path)) as client:
            data = preset(client, "accept_baseline")
            refused = create(client, data)
            created_only = create(client, data, run=False)
            health = client.get("/api/health").json()

        assert refused.status_code == 409
        assert "replay" in refused.json()["detail"]
        assert created_only.status_code == 201
        assert (health["llm_provider"], health["custom_scenarios"]) == ("replay", True)

    def test_running_needs_a_model(self, client):
        response = create(client, preset(client, "accept_baseline"))

        assert response.status_code == 503
        assert client.get("/api/health").json()["llm_provider"] is None
