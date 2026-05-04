from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_device_json():
    response = client.get("/api/device")
    assert response.status_code == 200
    data = response.json()
    assert "hostname" in data
    assert "model" in data
    assert "kernel" in data
    assert isinstance(data["cpu_count"], int)
    assert isinstance(data["memory_total_mb"], int)
    assert isinstance(data["disk_total_gb"], float)


def test_device_page():
    response = client.get("/device")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Информация об устройстве" in response.text
