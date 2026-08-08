"""
Phase 0 smoke test: confirms the FastAPI app boots, the lifespan hook loads
config successfully, and /healthz responds. No routing/rate-limit/budget
logic exists yet, so that's all there is to test at this stage.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_app_boots_and_healthz_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    # sample config.yaml ships with 3 teams
    assert body["config_teams"] == 3


def test_openapi_schema_available(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "LLM Gateway"
