import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def make_client(tmp_path):
    def _make(**overrides):
        settings = Settings(
            database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}", **overrides
        )
        return TestClient(create_app(settings))

    return _make


@pytest.fixture
def client(make_client):
    with make_client() as client:  # entering runs the lifespan, which seeds the database
        yield client


def test_lists_all_seeded_recommendations(client):
    recs = client.get("/api/recommendations").json()

    assert len(recs) == 13
    first = recs[0]
    assert (first["id"], first["product_name"], first["quantity"]) == (
        "REC-1001",
        "Sparkling Water 500ml 6-pack",
        800,
    )


def test_recommendation_detail_includes_how_it_was_computed(client):
    rec = client.get("/api/recommendations/REC-1002").json()

    assert "Does not net open purchase orders" in rec["method"]
    assert rec["inputs"]["open_po_units"] == 0


def test_unknown_recommendation_is_404(client):
    assert client.get("/api/recommendations/REC-404").status_code == 404


def test_context_for_baseline(client):
    body = client.get("/api/recommendations/REC-1001/context").json()

    assert body["evaluation"]["projection"]["net_need"] == 800
    assert body["evaluation"]["feasible"] is True
    assert body["evaluation"]["requires_approval"] is False
    assert body["recommendation_findings"] == []


def test_context_shows_why_the_recommendation_is_wrong(client):
    body = client.get("/api/recommendations/REC-1002/context").json()

    assert body["evaluation"]["projection"]["net_need"] == 480
    assert [f["code"] for f in body["evaluation"]["hard_violations"]] == ["OVER_STORAGE_CAPACITY"]
    assert [f["code"] for f in body["recommendation_findings"]] == ["REC_OPEN_POS_CHANGED"]


def test_context_exposes_the_supplier_note(client):
    body = client.get("/api/recommendations/REC-1013/context").json()

    assert "order 5000 units" in body["supplier_note"]


def test_purchase_orders_filter(client):
    pos = client.get("/api/purchase-orders", params={"sku": "SKU-1002"}).json()

    assert [po["id"] for po in pos] == ["PO-2041"]


def test_reset_reseeds(client):
    assert client.post("/api/admin/reset").json() == {"scenarios": 13}
    assert len(client.get("/api/recommendations").json()) == 13


def test_reset_can_be_disabled(make_client):
    with make_client(allow_demo_reset=False) as client:
        assert client.post("/api/admin/reset").status_code == 403
