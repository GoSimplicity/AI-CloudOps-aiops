"""使用真实 ASGI 应用和统一错误封装，不连接模型"""

import pytest

pytestmark = pytest.mark.integration


def test_assistant_query_400_on_missing_question(client):
    response = client.post("/api/v1/assistant/query", json={})
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_authentication_covers_application_routes(client):
    response = client.get("/api/v1/assistant/info", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401
    assert client.get("/health").json() == {"status": "ok"}
