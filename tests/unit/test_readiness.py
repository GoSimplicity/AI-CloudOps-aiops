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
