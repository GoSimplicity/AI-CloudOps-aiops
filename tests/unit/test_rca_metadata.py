from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import autofix, rca
from app.services.factory import ServiceFactory
from app.services.rca_service import RCAService


def client():
    app = FastAPI()
    app.include_router(rca.router, prefix="/rca")
    app.include_router(autofix.router, prefix="/autofix")
    return TestClient(app)


def test_metadata_without_dependencies_is_readable_and_not_healthy(monkeypatch):
    forbidden = AsyncMock(side_effect=AssertionError("Must not initialize dependencies"))
    monkeypatch.setattr(rca, "get_rca_service", forbidden)
    monkeypatch.setattr(autofix, "get_autofix_service", forbidden)
    monkeypatch.setattr(ServiceFactory, "_instances", {})
    test_client = client()
    stats = test_client.get("/rca/cache/stats")
    assert stats.status_code == 200
    assert stats.json()["data"]["available"] is False
    assert stats.json()["data"]["healthy"] is False
    assert stats.json()["data"]["total_keys"] is None
    assert test_client.get("/rca/config").status_code == 200
    for path in ["/rca/info", "/autofix/info"]:
        response = test_client.get(path)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["initialized"] is False
        assert data["status"] == "not_initialized"
        assert all(value is None for value in data["components"].values())
    forbidden.assert_not_called()


@pytest.mark.parametrize("connected", [True, False])
def test_existing_cache_reports_real_connection_without_rca_initialization(monkeypatch, connected):
    service = RCAService()
    service._cache_manager = Mock()
    service._cache_manager.health_check.return_value = {
        "status": "healthy" if connected else "unhealthy", "redis_connected": connected,
    }
    service._cache_manager.get_stats.return_value = {"hit_rate": 0.75, "total_keys": 4}
    monkeypatch.setattr(ServiceFactory, "_instances", {"rca": service})
    response = client().get("/rca/cache/stats")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["available"] is connected
    assert data["healthy"] is connected
    assert service.is_initialized() is False
    if not connected:
        service._cache_manager.get_stats.assert_not_called()


def test_cache_errors_do_not_expose_connection_details(monkeypatch):
    service = RCAService()
    service._cache_manager = Mock()
    service._cache_manager.health_check.side_effect = RuntimeError("redis://secret-password@private-host")
    monkeypatch.setattr(ServiceFactory, "_instances", {"rca": service})
    response = client().get("/rca/cache/stats")
    assert response.status_code == 200
    assert response.json()["data"]["available"] is False
    assert "secret-password" not in response.text
