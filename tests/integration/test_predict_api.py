"""预测路由的输入、依赖状态及错误契约集成"""

from unittest.mock import AsyncMock, Mock

import pytest

from app.api.routes import predict

pytestmark = pytest.mark.integration


def test_prediction_readiness_reports_dependency_failure(client, monkeypatch):
    service = AsyncMock()
    service.is_initialized = Mock(return_value=True)
    service.health_check.return_value = False
    monkeypatch.setattr(predict, "get_prediction_service", AsyncMock(return_value=service))
    from app.services.prometheus import PrometheusService

    monkeypatch.setattr(PrometheusService, "is_healthy", lambda self: False)
    response = client.get("/api/v1/predict/ready")
    assert response.status_code == 503
    assert response.json()["code"] == 503


def test_invalid_qps_does_not_initialize_dependencies(client, monkeypatch):
    getter = AsyncMock(side_effect=AssertionError("无效请求不得初始化业务依赖"))
    monkeypatch.setattr(predict, "get_prediction_service", getter)
    response = client.post("/api/v1/predict/qps", json={"current_qps": 1, "prediction_type_invalid": True})
    assert response.status_code == 400
    assert response.json()["code"] == 400
    getter.assert_not_awaited()
