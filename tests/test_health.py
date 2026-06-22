"""Smoke test for the admin health endpoint."""

from fastapi.testclient import TestClient

from iosforge.admin.app import create_app


def test_health_ok() -> None:
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
