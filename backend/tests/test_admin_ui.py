import re

import pytest
from fastapi.testclient import TestClient

from app.admin_ui import VIEWS
from app.config import Settings
from app.main import create_app


@pytest.fixture
def make_client(tmp_path):
    def _make(**overrides):
        url = f"sqlite:///{(tmp_path / 'admin.db').as_posix()}"
        return TestClient(create_app(Settings(database_url=url, **overrides)))

    return _make


@pytest.fixture
def client(make_client):
    with make_client() as client:
        yield client


@pytest.mark.parametrize("view", VIEWS, ids=lambda v: v.identity)
def test_every_list_page_renders(client, view):
    response = client.get(f"/admin/{view.identity}/list")

    assert response.status_code == 200


def test_list_shows_seeded_rows(client):
    page = client.get("/admin/recommendation/list").text

    assert "REC-1001" in page
    assert "open_po_on_hold" in page


def test_budget_list_shows_computed_available(client):
    page = client.get("/admin/budget/list").text

    assert "1500.0" in page  # yogurt budget: 20,000 total - 18,500 committed


def test_detail_page_works_for_composite_keys(client):
    page = client.get("/admin/inventory-record/list").text
    detail_link = re.search(r'href="([^"]*/admin/inventory-record/details/[^"]+)"', page)

    assert detail_link, "no detail link on inventory list"
    assert client.get(detail_link.group(1)).status_code == 200


def test_views_are_read_only(client):
    assert client.get("/admin/purchase-order/create").status_code in (403, 404)
    assert client.get("/admin/purchase-order/edit/PO-2041").status_code in (403, 404)


def test_admin_ui_can_be_disabled(make_client):
    with make_client(admin_ui_enabled=False) as client:
        assert client.get("/admin/recommendation/list").status_code == 404
