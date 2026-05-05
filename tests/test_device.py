import pytest
from fastapi.testclient import TestClient

from app.api.auth import require_user_api
from app.main import app

client = TestClient(app)

_FAKE_USER = {"id": 1, "username": "test", "status": "approved"}


@pytest.fixture
def authed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass auth: API dep + HTML page check both return a fake approved user."""
    app.dependency_overrides[require_user_api] = lambda: _FAKE_USER
    monkeypatch.setattr("app.api.routes.device.current_user", lambda _req: _FAKE_USER)
    yield
    app.dependency_overrides.clear()


def test_device_json_requires_auth() -> None:
    response = client.get("/api/device")
    assert response.status_code == 401


def test_device_page_redirects_when_unauthed() -> None:
    response = client.get("/device", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/device")


def test_device_json(authed: None) -> None:
    response = client.get("/api/device")
    assert response.status_code == 200
    data = response.json()
    assert "hostname" in data
    assert "model" in data
    assert "kernel" in data
    assert isinstance(data["cpu_count"], int)
    assert isinstance(data["memory_total_mb"], int)
    assert isinstance(data["disk_total_gb"], float)


def test_device_page(authed: None) -> None:
    response = client.get("/device")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Информация об устройстве" in response.text
