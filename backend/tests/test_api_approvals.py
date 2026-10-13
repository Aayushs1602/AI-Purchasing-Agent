"""Approving and declining POs over HTTP, including resuming a paused agent run."""

import pytest
from fastapi.testclient import TestClient

from app.agent.llm.scripted import ScriptedClient
from app.background import InlineExecutor
from app.config import Settings
from app.main import create_app
from app.models import Recommendation
from app.services.purchase_orders import PurchaseOrderService
from tests.agent.test_orchestrator import READS, create_po, finalize, submit

PAUSES = [READS, create_po(800, 3600), submit(800, 3600)]
ESCALATE = [
    {
        "name": "escalate_to_human",
        "args": {
            "reason": "The buyer declined the coffee order.",
            "questions": ["Wait for the promo price?"],
        },
    }
]


@pytest.fixture
def make_client(tmp_path):
    def _make(script=None, **overrides):
        settings = Settings(
            database_url=f"sqlite:///{(tmp_path / 'approvals.db').as_posix()}", **overrides
        )
        llm = ScriptedClient(script) if script is not None else None
        return TestClient(create_app(settings, llm=llm, executor=InlineExecutor()))

    return _make


def start(client, rec_id):
    return client.post(f"/api/recommendations/{rec_id}/runs").json()["run_id"]


def test_pending_po_is_listed_with_its_reasons(make_client):
    with make_client(PAUSES) as client:
        run_id = start(client, "REC-1012")

        items = client.get("/api/approvals").json()

    assert len(items) == 1
    item = items[0]
    assert (item["purchase_order"]["id"], item["run_id"], item["run_status"]) == (
        "PO-5001",
        run_id,
        "awaiting_approval",
    )
    assert item["product_name"] == "Ground Coffee 250g"
    assert item["reasons"][0]["code"] == "VALUE_ABOVE_AUTO_APPROVE_LIMIT"


def test_approving_resumes_the_run(make_client):
    script = [*PAUSES, finalize("accept", 800, factor="order_value", value=3600)]
    with make_client(script) as client:
        run_id = start(client, "REC-1012")

        outcome = client.post(
            "/api/purchase-orders/PO-5001/approve",
            json={"approver": "buyer:ana", "comment": "ok for promo"},
        ).json()
        run = client.get(f"/api/runs/{run_id}").json()

        assert client.get("/api/approvals").json() == []

    assert (outcome["handled_by"], outcome["run_id"]) == ("agent_run", run_id)
    assert (run["status"], run["decision"]) == ("completed", "accept")
    po = run["purchase_orders"][0]
    assert (po["status"], po["approval_by"], po["approval_comment"]) == (
        "confirmed",
        "buyer:ana",
        "ok for promo",
    )


def test_declining_lets_the_agent_choose_another_path(make_client):
    script = [*PAUSES, ESCALATE, finalize("investigate", 0, factor="order_value", value=3600)]
    with make_client(script) as client:
        run_id = start(client, "REC-1012")

        client.post(
            "/api/purchase-orders/PO-5001/decline",
            json={"approver": "buyer:ana", "comment": "wait for promo price"},
        )
        run = client.get(f"/api/runs/{run_id}").json()

    assert (run["status"], run["decision"]) == ("escalated", "investigate")
    assert run["purchase_orders"][0]["status"] == "declined"
    assert run["escalations"][0]["questions"] == ["Wait for the promo price?"]


def test_decline_needs_a_reason(make_client):
    with make_client(PAUSES) as client:
        start(client, "REC-1012")

        response = client.post("/api/purchase-orders/PO-5001/decline", json={"approver": "ana"})

    assert response.status_code == 422


def test_only_pending_pos_can_be_decided(make_client):
    with make_client([]) as client:
        response = client.post(
            "/api/purchase-orders/PO-2041/approve", json={"approver": "buyer:ana"}
        )

    assert response.status_code == 409
    assert "confirmed, not waiting for approval" in response.json()["detail"]


def test_po_outside_any_run_is_decided_directly(make_client):
    with make_client([]) as client:
        app = client.app
        with app.state.session_factory() as session:
            service = PurchaseOrderService(session, today=app.state.settings.sim_today)
            rec = session.get(Recommendation, "REC-1012")
            po = service.create_draft(
                sku=rec.sku,
                node_id=rec.node_id,
                supplier_id=rec.supplier_id,
                quantity=800,
                created_by="buyer",
            ).po
            service.submit(po.id, actor="buyer")

        outcome = client.post(
            f"/api/purchase-orders/{po.id}/approve", json={"approver": "buyer:lead"}
        ).json()

    assert (outcome["handled_by"], outcome["po_status"]) == ("direct", "confirmed")


def test_paused_run_cannot_resume_without_an_llm(make_client):
    with make_client(PAUSES) as client:
        start(client, "REC-1012")

    with make_client(gemini_api_key="") as client:  # same database, no LLM configured
        response = client.post(
            "/api/purchase-orders/PO-5001/approve", json={"approver": "buyer:ana"}
        )

    assert response.status_code == 503
    assert "Cannot resume the agent run" in response.json()["detail"]
