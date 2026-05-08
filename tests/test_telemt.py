from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.auth import require_user_api
from app.main import app
from app.services.telemt import TelemtUnavailable, build_snapshot, parse_metrics

FIXTURE = Path(__file__).resolve().parents[1] / "telemt.metrics.example"
client = TestClient(app)

_FAKE_USER = {"id": 1, "username": "test", "status": "approved"}


@pytest.fixture
def metrics_text() -> str:
    return FIXTURE.read_text()


@pytest.fixture
def authed(monkeypatch: pytest.MonkeyPatch) -> None:
    app.dependency_overrides[require_user_api] = lambda: _FAKE_USER
    monkeypatch.setattr("app.api.routes.telemt.current_user", lambda _req: _FAKE_USER)
    yield
    app.dependency_overrides.clear()


def test_parser_handles_comments_and_labels(metrics_text: str) -> None:
    m = parse_metrics(metrics_text)
    # comment lines must be ignored
    assert all(not k.startswith("#") for k in m)
    # scalar
    assert m["telemt_uptime_seconds"][0][1] == 562.1
    # labelled — info gauge
    samples = m["telemt_build_info"]
    assert samples[0][0]["version"] == "3.4.10"
    assert samples[0][1] == 1.0


def test_parser_handles_multiple_label_samples(metrics_text: str) -> None:
    samples = parse_metrics(metrics_text)["telemt_buffer_pool_buffers_total"]
    by_kind = {s[0]["kind"]: s[1] for s in samples}
    assert by_kind == {"pooled": 11, "allocated": 11, "in_use": 0}


def test_snapshot_extracts_curated_fields(metrics_text: str) -> None:
    s = build_snapshot(metrics_text)
    assert s.version == "3.4.10"
    assert s.uptime_seconds == 562.1
    assert s.connections_total == 640
    assert s.upstream_connect_success_total == 470
    assert s.upstream_connect_fail_total == 2
    assert s.me_writers_active == 44
    assert s.me_writers_target == 43
    assert s.me_endpoint_quarantine_unexpected_total == 5
    assert s.route_drop_no_conn_total == 40
    assert s.desync_total == 0


def test_snapshot_users(metrics_text: str) -> None:
    s = build_snapshot(metrics_text)
    assert len(s.users) == 1
    u = s.users[0]
    assert u.user == "hello"
    assert u.connections_current == 11
    assert u.connections_total == 636
    assert u.octets_from_client == 789500
    assert u.octets_to_client == 13214792
    assert u.unique_ips_current == 3
    assert u.unique_ips_recent_window == 2
    assert u.unique_ips_limit == 0


def test_snapshot_handles_missing_metrics() -> None:
    s = build_snapshot("# nothing here\n")
    assert s.version is None
    assert s.uptime_seconds == 0.0
    assert s.connections_total == 0
    assert s.users == []


def test_telemt_json_requires_auth() -> None:
    response = client.get("/api/telemt")
    assert response.status_code == 401


def test_telemt_page_redirects_when_unauthed() -> None:
    response = client.get("/telemt", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/telemt")


def test_telemt_json_when_url_unset(authed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom() -> None:
        raise TelemtUnavailable("TELEMT_METRICS_URL is not set")

    monkeypatch.setattr("app.api.routes.telemt.get_telemt_snapshot", boom)
    response = client.get("/api/telemt")
    assert response.status_code == 503
    assert "telemt metrics unavailable" in response.json()["detail"]


def test_telemt_json_returns_snapshot(
    authed: None, monkeypatch: pytest.MonkeyPatch, metrics_text: str
) -> None:
    snapshot = build_snapshot(metrics_text)

    async def fake() -> object:
        return snapshot

    monkeypatch.setattr("app.api.routes.telemt.get_telemt_snapshot", fake)
    response = client.get("/api/telemt")
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == "3.4.10"
    assert data["users"][0]["user"] == "hello"


def test_telemt_page_renders_with_error(authed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom() -> None:
        raise TelemtUnavailable("connection refused")

    monkeypatch.setattr("app.api.routes.telemt.get_telemt_snapshot", boom)
    response = client.get("/telemt")
    assert response.status_code == 200
    assert "connection refused" in response.text
    assert "Метрики недоступны" in response.text
