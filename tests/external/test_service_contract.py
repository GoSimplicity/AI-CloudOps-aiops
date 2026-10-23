"""只读真实服务验收，不调用业务模型、通知或集群变更"""

import pytest

pytestmark = pytest.mark.external


def test_liveness(external_client):
    response = external_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_invalid_key_is_rejected(external_client):
    response = external_client.get("/openapi.json", headers={"X-API-Key": "deliberately-invalid-key"})
    assert response.status_code == 401


def test_authenticated_openapi(external_client):
    response = external_client.get("/openapi.json")
    assert response.status_code == 200
    assert "/api/v1/assistant/query" in response.json()["paths"]


def test_invalid_prediction_rejected_before_dependencies(external_client):
    response = external_client.post("/api/v1/predict/qps", json={"current_qps": 1, "unexpected": True})
    assert response.status_code == 400
    assert response.json()["code"] == 400
