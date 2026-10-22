from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.middleware.error_handler import setup_error_handlers
from app.api.routes import assistant
from app.config.settings import config
from app.services.assistant_service import OptimizedAssistantService
from app.services.factory import ServiceFactory


def metadata_client():
    app = FastAPI()
    app.include_router(assistant.router, prefix="/assistant")
    setup_error_handlers(app)
    return TestClient(app)


def test_metadata_does_not_initialize_dependencies(monkeypatch):
    getter = AsyncMock(side_effect=AssertionError("Metadata must not initialize services"))
    monkeypatch.setattr(assistant, "get_assistant_service", getter)
    monkeypatch.setattr(ServiceFactory, "_instances", {})
    monkeypatch.setattr(config.llm, "api_key", "")
    client = metadata_client()
    info = client.get("/assistant/info")
    settings = client.get("/assistant/config")
    assert info.status_code == settings.status_code == 200
    assert info.json()["data"]["initialized"] is False
    assert info.json()["data"]["status"] == "not_initialized"
    assert settings.json()["data"]["config"]["model_config"]["provider"]
    getter.assert_not_called()


def test_refresh_without_provider_returns_service_unavailable(monkeypatch):
    monkeypatch.setattr(config.llm, "provider", "openai")
    monkeypatch.setattr(config.llm, "api_key", "")
    monkeypatch.setattr(config, "debug", False)
    service = OptimizedAssistantService()
    monkeypatch.setattr(assistant, "get_assistant_service", AsyncMock(return_value=service))
    response = metadata_client().post("/assistant/refresh")
    assert response.status_code == 503
    assert "Traceback" not in response.text
    assert service.is_initialized() is False


async def test_embedding_probe_is_async_and_does_not_invent_dimension():
    from unittest.mock import Mock

    import pytest

    from app.core.agents.enterprise_assistant import _get_embedding_dimension

    embedding = Mock()
    embedding.aembed_query = AsyncMock(return_value=[0.1, 0.2, 0.3])
    assert await _get_embedding_dimension(embedding) == 3
    embedding.embed_query.assert_not_called()
    embedding.aembed_query.side_effect = ConnectionError("provider disconnected")
    with pytest.raises(RuntimeError, match="Embedding model is unavailable"):
        await _get_embedding_dimension(embedding)
