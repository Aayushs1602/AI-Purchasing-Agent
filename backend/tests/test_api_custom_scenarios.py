"""Custom scenarios over HTTP: presets, live preview, create and run."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def make_client(tmp_path):
    def _make(llm=None, **overrides):
        overrides.setdefault("gemini_api_key", "")  # never build a real model client in tests
        settings = Settings(
            database_url=f"sqlite:///{(tmp_path / 'custom.db').as_posix()}", **overrides
        )
        return TestClient(create_app(settings, llm=llm))

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
