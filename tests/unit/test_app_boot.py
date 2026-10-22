"""Boot must remain independent of model, Redis and cluster availability."""

from fastapi.testclient import TestClient

from app.config.settings import config
from app.main import create_app


def test_lazy_boot_health_and_route_registration(monkeypatch):
    monkeypatch.setenv("AIOPS_PREWARM", "false")
    monkeypatch.setattr(config.security, "api_key", "offline-key")
    app = create_app()
    paths = set(app.openapi()["paths"])
    for prefix in ["predict", "rca", "autofix", "assistant", "cache", "inspection"]:
        assert any(path.startswith(f"/api/v1/{prefix}") for path in paths)
    # Repeat lifespan to verify shutdown state does not poison a later app instance.
    for _ in range(2):
        with TestClient(app) as client:
            assert client.get("/health").json() == {"status": "ok"}
            assert client.get("/openapi.json").status_code == 401
            response = client.get("/openapi.json", headers={"X-API-Key": "offline-key"})
            assert response.status_code == 200
