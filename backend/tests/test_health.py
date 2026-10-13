from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health_reports_ok(tmp_path):
    # Tests never point at the developer's purchasing.db.
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'health.db').as_posix()}")
    client = TestClient(create_app(settings))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
