import pytest
from fastapi.testclient import TestClient

from app.agent.llm.scripted import ScriptedClient
from app.background import InlineExecutor
from app.config import Settings
from app.main import create_app
from tests.agent.test_orchestrator import READS, finalize

ESCALATE = [
    READS,
    [
        {
            "name": "escalate_to_human",
            "args": {
                "reason": "The MOQ of 1,000 costs more than the $2,000 budget left.",
                "questions": ["Raise the budget by $500?"],
                "options": ["Raise budget", "Negotiate MOQ"],
            },
        }
    ],
    finalize("investigate", 0, factor="moq", value=1000),
]


@pytest.fixture
def client(tmp_path):
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'esc.db').as_posix()}")
    app = create_app(settings, llm=ScriptedClient(ESCALATE), executor=InlineExecutor())
    with TestClient(app) as client:
        client.post("/api/recommendations/REC-1009/runs")
        yield client


def test_open_escalations_are_listed(client):
    open_items = client.get("/api/escalations", params={"status": "open"}).json()

    assert len(open_items) == 1
    item = open_items[0]
    assert (item["id"], item["recommendation_id"], item["status"]) == (
        "ESC-0001",
        "REC-1009",
        "open",
    )
    assert item["options"] == ["Raise budget", "Negotiate MOQ"]


def test_resolving_closes_it_and_releases_the_recommendation(client):
    resolved = client.post(
        "/api/escalations/ESC-0001/resolve",
        json={"resolved_by": "buyer:ana", "resolution": "Budget raised by $500; reorder MOQ."},
    ).json()

    assert (resolved["status"], resolved["resolved_by"]) == ("resolved", "buyer:ana")
    assert client.get("/api/escalations", params={"status": "open"}).json() == []
    rec = client.get("/api/recommendations/REC-1009").json()
    assert rec["status"] == "resolved"


def test_resolving_twice_or_unknown(client):
    body = {"resolved_by": "buyer:ana", "resolution": "Handled offline."}
    client.post("/api/escalations/ESC-0001/resolve", json=body)

    assert client.post("/api/escalations/ESC-0001/resolve", json=body).status_code == 409
    assert client.post("/api/escalations/ESC-9999/resolve", json=body).status_code == 404
