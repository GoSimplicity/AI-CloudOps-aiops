"""Boot must remain independent of model, Redis and cluster availability."""

from fastapi.testclient import TestClient

from app.config.settings import config
from app.core.prediction import model_manager as model_manager_module
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
            assert client.head("/health").status_code == 200
            assert client.get("/openapi.json").status_code == 401
            response = client.get("/openapi.json", headers={"X-API-Key": "offline-key"})
            assert response.status_code == 200


def test_process_health_stays_200_when_models_use_rule_fallback(tmp_path, monkeypatch):
    """没挂模型时预测走规则兜底，存活探针仍是免鉴权的 GET /health 且返回 200。"""
    monkeypatch.setattr(config.prediction, "model_base_path", str(tmp_path))
    model_manager_module._status_cache.clear()
    monkeypatch.setenv("AIOPS_PREWARM", "false")
    monkeypatch.setattr(config.security, "api_key", "offline-service-key-with-32-characters")
    app = create_app()
    with TestClient(app) as client:
        health = client.get("/health")
        info = client.get(
            "/api/v1/predict/info",
            headers={"X-API-Key": "offline-service-key-with-32-characters"},
        )
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert info.status_code == 200
    assert info.json()["data"]["prediction_models"]["rule_fallback"] is True


def test_liveness_returns_503_while_the_process_is_shutting_down(monkeypatch):
    from app.main import app_state

    monkeypatch.setenv("AIOPS_PREWARM", "false")
    monkeypatch.setattr(config.security, "api_key", "offline-service-key-with-32-characters")
    app = create_app()
    with TestClient(app) as client:
        app_state.is_shutting_down = True
        try:
            response = client.get("/health")
            head = client.head("/health")
        finally:
            app_state.is_shutting_down = False
    assert response.status_code == 503
    assert response.text == "Service is shutting down"
    assert response.headers["retry-after"] == "60"
    assert head.status_code == 503
