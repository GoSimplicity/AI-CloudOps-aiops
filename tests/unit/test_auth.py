from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.middleware.auth import setup_auth
from app.config.settings import config


def make_client(monkeypatch, key="test-key", env="test"):
    monkeypatch.setattr(config.security, "api_key", key)
    monkeypatch.setattr(config, "env", env)
    app = FastAPI()
    setup_auth(app)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/tools/execute")
    def execute():
        return {"executed": True}

    return TestClient(app)


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"Authorization": "Basic test-key"}, {"X-API-Key": "非ASCII"}])
def test_denies_unauthenticated_operations(monkeypatch, headers):
    client = make_client(monkeypatch)
    # HTTP header values are bytes on the wire.
    headers = {k: v.encode("utf-8") for k, v in headers.items()}
    assert client.post("/tools/execute", headers=headers).status_code == 401


@pytest.mark.parametrize("headers", [{"X-API-Key": "test-key"}, {"Authorization": "Bearer test-key"}])
def test_allows_service_credentials(monkeypatch, headers):
    assert make_client(monkeypatch).post("/tools/execute", headers=headers).json() == {"executed": True}


def test_public_health_does_not_exempt_operations(monkeypatch):
    client = make_client(monkeypatch)
    assert client.get("/health").status_code == 200
    assert client.post("/health").status_code == 401
    assert client.get("/docs").status_code == 401


@pytest.mark.parametrize("key", ["", "short"])
def test_production_requires_strong_key(monkeypatch, key):
    with pytest.raises(ValueError, match="AIOPS_API_KEY"):
        make_client(monkeypatch, key, "production")
