"""SEC-26 P2-1：api_response 不再吞掉 AIOpsException 的 error_code，返回体 data.error_code 可见（全部离线）"""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.decorators import AIOpsHTTPException, api_response
from app.api.middleware.error_handler import setup_error_handlers
from app.common.exceptions import (
    AutoFixError,
    FeatureDisabledError,
    NamespaceNotAllowedError,
    ServiceUnavailableError,
    ValidationError,
    WorkflowApprovalError,
)
from app.config.settings import config
from tests.unit.test_autofix_sec26_repair_bootstrap import REPAIR_BODY, make_app
from tests.unit.test_autofix_sec26_run_lockdown import assert_no_writes, make_service, mock_k8s


def test_feature_disabled_error_code_in_response(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "repair_enabled", False)
    monkeypatch.setattr(config.autofix, "bootstrap_enabled", False)
    client = make_app(monkeypatch, make_service(mock_k8s()))
    for path, body, switch in (("/autofix/repair", REPAIR_BODY, "autofix.repair_enabled"), ("/autofix/bootstrap", None, "autofix.bootstrap_enabled")):
        response = client.post(path, json=body)
        assert response.status_code == 403
        data = response.json()["data"]
        assert data["error_code"] == "AUTOFIX_FEATURE_DISABLED"
        assert data["details"] == {"switch": switch}
        # data.detail 仍是 message 字符串（与旧契约一致）
        assert isinstance(data["detail"], str) and switch in data["detail"]


def test_namespace_not_allowed_error_code_in_response(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "bootstrap_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", [])
    client = make_app(monkeypatch, make_service(mock_k8s()))
    response = client.post("/autofix/bootstrap")
    assert response.status_code == 403
    data = response.json()["data"]
    assert data["error_code"] == "AUTOFIX_NAMESPACE_NOT_ALLOWED"
    assert data["details"] == {"namespace": "test", "switch": "autofix.allowed_namespaces"}


def test_validation_error_code_in_response(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    k8s = mock_k8s()
    client = make_app(monkeypatch, make_service(k8s))
    response = client.post("/autofix/repair", json={**REPAIR_BODY, "force": True})
    assert response.status_code == 400
    assert response.json()["data"]["error_code"] == "VALIDATION_ERROR"
    assert_no_writes(k8s)


def test_workflow_approval_error_code_in_response(monkeypatch) -> None:
    client = make_app(monkeypatch, make_service(mock_k8s()))
    response = client.post("/autofix/workflow/confirm", json={"plan_id": "0" * 32, "approved_action_ids": ["x"]})
    assert response.status_code == 404
    assert response.json()["data"]["error_code"] == "WORKFLOW_APPROVAL_ERROR"


def test_autofix_error_code_kept_but_message_masked_for_5xx(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config, "debug", False)
    service = make_service(mock_k8s())
    monkeypatch.setattr(service, "fix_kubernetes_deployment", AsyncMock(side_effect=AutoFixError("internal detail")))
    client = make_app(monkeypatch, service)
    response = client.post("/autofix/repair", json=REPAIR_BODY)
    assert response.status_code == 502
    payload = response.json()
    assert payload["data"]["error_code"] == "AUTOFIX_ERROR"
    assert "internal detail" not in response.text
    assert "details" not in payload["data"]


@pytest.mark.parametrize(
    "exc,status,code",
    [
        (FeatureDisabledError("off", switch="autofix.repair_enabled"), 403, "AUTOFIX_FEATURE_DISABLED"),
        (NamespaceNotAllowedError("ns", namespace="test"), 403, "AUTOFIX_NAMESPACE_NOT_ALLOWED"),
        (WorkflowApprovalError(403, "denied"), 403, "WORKFLOW_APPROVAL_ERROR"),
        (ValidationError("f", "bad"), 400, "VALIDATION_ERROR"),
        (ServiceUnavailableError("autofix"), 503, "SERVICE_UNAVAILABLE"),
        (AutoFixError("x"), 502, "AUTOFIX_ERROR"),
    ],
)
def test_api_response_preserves_error_code_for_each_domain_error(exc, status, code) -> None:
    app = FastAPI()
    setup_error_handlers(app)

    @app.get("/boom")
    @api_response("probe")
    async def boom():
        raise exc

    response = TestClient(app).get("/boom")
    assert response.status_code == status
    assert response.json()["data"]["error_code"] == code


async def test_api_response_raises_http_exception_carrying_error_code() -> None:
    @api_response("probe")
    async def boom():
        raise FeatureDisabledError("off", switch="autofix.bootstrap_enabled")

    with pytest.raises(AIOpsHTTPException) as raised:
        await boom()
    assert raised.value.status_code == 403
    assert raised.value.error_code == "AUTOFIX_FEATURE_DISABLED"
    assert raised.value.error_details == {"switch": "autofix.bootstrap_enabled"}
