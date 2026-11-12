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


@pytest.mark.parametrize("metrics_healthy, expected", [(False, 503), (True, 200)])
def test_prediction_readiness_requires_metrics(monkeypatch, metrics_healthy, expected):
    from unittest.mock import Mock

    from app.api.routes import predict
    from app.services.prometheus import PrometheusService

    service = AsyncMock()
    service.is_initialized = Mock(return_value=True)
    service.health_check.return_value = True
    monkeypatch.setattr(predict, "get_prediction_service", AsyncMock(return_value=service))
    monkeypatch.setattr(PrometheusService, "is_healthy", lambda self: metrics_healthy)
    app = FastAPI()
    app.include_router(predict.router, prefix="/service")
    assert TestClient(app).get("/service/ready").status_code == expected


async def test_rag_readiness_checks_dependencies():
    from unittest.mock import Mock

    from app.core.agents.enterprise_assistant import RAGAssistant

    rag = object.__new__(RAGAssistant)
    rag.vector_store = Mock()
    rag.vector_store.health_check.return_value = {"status": "unhealthy", "index_available": False}
    rag.llm_service = AsyncMock()
    rag.llm_service.is_healthy.return_value = True
    rag.cache_manager = None
    rag._stats = {}
    assert (await rag.health_check())["status"] == "unhealthy"
    rag.vector_store.health_check.return_value = {"status": "healthy", "index_available": True}
    assert (await rag.health_check())["status"] == "healthy"


async def test_llm_readiness_uses_metadata_not_generation(monkeypatch):
    from unittest.mock import Mock

    from app.config.settings import config
    from app.services.llm import LLMService

    service = object.__new__(LLMService)
    service.provider = "openai"
    service.client = Mock()
    monkeypatch.setattr(config.llm, "api_key", "offline-test")
    assert await service._check_openai_health() is True
    service.client.with_options.return_value.models.list.assert_called_once()
    service.client.chat.completions.create.assert_not_called()
    monkeypatch.setattr(config.llm, "api_key", "")
    assert await service._check_openai_health() is False


def _mount(monkeypatch, module, service):
    name = {
        "assistant": "get_assistant_service",
        "predict": "get_prediction_service",
        "rca": "get_rca_service",
    }[module.__name__.rsplit(".", 1)[-1]]
    monkeypatch.setattr(module, name, AsyncMock(return_value=service))
    app = FastAPI()
    app.include_router(module.router, prefix="/service")
    return TestClient(app)


def test_rca_ready_reports_collector_status(monkeypatch):
    from app.api.routes import rca

    service = AsyncMock()
    service.get_health_status.return_value = {"status": "healthy"}
    client = _mount(monkeypatch, rca, service)
    response = client.get("/service/ready")
    assert response.status_code == 200
    assert response.json()["data"]["ready"] is True
    assert response.json()["data"]["service"] == "rca"

    service.get_health_status.return_value = {"status": "degraded"}
    ready = client.get("/service/ready")
    health = client.get("/service/health")
    assert ready.status_code == 503
    assert health.status_code == ready.status_code
    assert health.json() == ready.json()


def test_rca_health_returns_a_single_ready_payload(monkeypatch):
    from app.api.routes import rca

    service = AsyncMock()
    service.get_health_status.return_value = {"status": "healthy"}
    client = _mount(monkeypatch, rca, service)
    response = client.get("/service/health")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ready"] is True
    assert data["service"] == "rca"
    assert "data" not in data


def test_assistant_ready_requires_a_healthy_mode(monkeypatch):
    from app.api.routes import assistant

    service = AsyncMock()
    service.get_service_health_info_with_mode.return_value = {"modes": {"rag": {"status": "healthy"}, "mcp": {"status": "down"}}}
    client = _mount(monkeypatch, assistant, service)
    response = client.get("/service/ready")
    assert response.status_code == 200
    assert response.json()["data"]["service"] == "assistant"
    assert response.json()["data"]["ready"] is True

    service.get_service_health_info_with_mode.return_value = {"modes": {"rag": {"status": "down"}, "mcp": {"status": "down"}}}
    ready = client.get("/service/ready")
    health = client.get("/service/health")
    assert ready.status_code == 503
    assert health.status_code == ready.status_code
    assert health.json() == ready.json()

    service.get_service_health_info_with_mode.side_effect = RuntimeError("assistant-health-offline")
    ready = client.get("/service/ready")
    health = client.get("/service/health")
    assert ready.status_code == 503
    assert health.status_code == ready.status_code
    assert health.json() == ready.json()
    assert "assistant-health-offline" not in ready.text
    assert "assistant-health-offline" not in health.text


def test_assistant_health_returns_a_single_ready_payload(monkeypatch):
    from app.api.routes import assistant

    service = AsyncMock()
    service.get_service_health_info_with_mode.return_value = {"modes": {"rag": {"status": "healthy"}, "mcp": {"status": "down"}}}
    client = _mount(monkeypatch, assistant, service)
    response = client.get("/service/health")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["service"] == "assistant"
    assert data["ready"] is True
    assert "data" not in data


@pytest.mark.parametrize("path", ["/service/health", "/service/ready"])
def test_prediction_health_stays_unavailable_without_metrics(monkeypatch, path):
    from unittest.mock import Mock

    from app.api.routes import predict
    from app.services.prometheus import PrometheusService

    service = AsyncMock()
    service.is_initialized = Mock(return_value=True)
    service.health_check.return_value = True
    monkeypatch.setattr(PrometheusService, "is_healthy", lambda self: False)
    client = _mount(monkeypatch, predict, service)
    response = client.get(path)
    assert response.status_code == 503
    assert response.json()["detail"] == "服务 prediction 当前不可用"
