from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import make_client


def test_health_needs_no_configuration():
    client = TestClient(create_app(Settings()))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_missing_secrets_by_name():
    client = TestClient(create_app(Settings()))
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert set(body["missing"]) == {
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "GEMINI_API_KEY",
    }


def test_ready_passes_when_configured():
    response = make_client().get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
