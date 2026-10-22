from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import autofix, rca


@pytest.mark.parametrize("module, status, healthy", [(autofix, None, False), (rca, "unhealthy", False), (rca, "degraded", False)])
def test_unavailable_dependencies_are_not_ready(monkeypatch, module, status, healthy):
    service = AsyncMock()
    service.health_check.return_value = healthy
    service.get_health_status.return_value = {"status": status}
    name = "get_autofix_service" if module is autofix else "get_rca_service"
    monkeypatch.setattr(module, name, AsyncMock(return_value=service))
    app = FastAPI()
    app.include_router(module.router, prefix="/service")
    assert TestClient(app).get("/service/ready").status_code == 503


@pytest.mark.parametrize("module", [autofix, rca])
def test_available_dependencies_are_ready(monkeypatch, module):
    service = AsyncMock()
    service.health_check.return_value = True
    service.get_health_status.return_value = {"status": "healthy"}
    name = "get_autofix_service" if module is autofix else "get_rca_service"
    monkeypatch.setattr(module, name, AsyncMock(return_value=service))
    app = FastAPI()
    app.include_router(module.router, prefix="/service")
    response = TestClient(app).get("/service/ready")
    assert response.status_code == 200
    assert response.json()["data"]["ready"] is True
