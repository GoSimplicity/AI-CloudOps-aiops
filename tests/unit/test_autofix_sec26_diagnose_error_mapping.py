"""SEC-26 第二层审查 P3-3：/autofix/diagnose 只有 ValidationError 走 400，其它领域异常走统一的 5xx 映射且响应不带 str(e)（全部离线）"""

from unittest.mock import AsyncMock

import pytest

from app.common.exceptions import AIOpsException, AutoFixError, RCAError, ServiceUnavailableError, ValidationError
from app.config.settings import config
from tests.unit.test_autofix_sec26_error_sanitization import service_with_fixer
from tests.unit.test_autofix_sec26_repair_bootstrap import make_app
from tests.unit.test_autofix_sec26_run_lockdown import mock_k8s

SECRET = "token=sk-offline-leak-marker kubeconfig-server=https://10.0.0.1:6443"
BODY = {"deployment": "api", "namespace": "default"}


@pytest.fixture(autouse=True)
def production_mode(monkeypatch):
    monkeypatch.setattr(config, "debug", False)


def diagnose_raising(monkeypatch, exc):
    service = service_with_fixer(mock_k8s())
    monkeypatch.setattr(service, "initialize", AsyncMock())
    monkeypatch.setattr(service, "diagnose_kubernetes_issues", AsyncMock(side_effect=exc))
    return make_app(monkeypatch, service).post("/autofix/diagnose", json=BODY)


@pytest.mark.parametrize(
    "exc,status,error_code",
    [
        (AutoFixError(SECRET, details={"raw": SECRET}), 502, "AUTOFIX_ERROR"),
        (RCAError(SECRET), 502, "RCA_ERROR"),
        (ServiceUnavailableError(SECRET, details={"raw": SECRET}), 503, "SERVICE_UNAVAILABLE"),
        (AIOpsException(SECRET, details={"raw": SECRET}), 500, None),
    ],
    ids=["autofix-502", "rca-502", "unavailable-503", "generic-500"],
)
def test_diagnose_domain_errors_use_unified_5xx_mapping_without_leaking(monkeypatch, exc, status, error_code) -> None:
    response = diagnose_raising(monkeypatch, exc)
    assert response.status_code == status, response.text
    assert SECRET not in response.text
    assert str(exc) not in response.text
    if error_code:
        assert response.json()["data"]["error_code"] == error_code


def test_diagnose_validation_error_stays_400(monkeypatch) -> None:
    response = diagnose_raising(monkeypatch, ValidationError("namespace", "格式无效"))
    assert response.status_code == 400, response.text
    data = response.json()["data"]
    assert data["error_code"] == "VALIDATION_ERROR"
    assert "格式无效" in response.text


def test_diagnose_unknown_error_is_500_internal_generic(monkeypatch) -> None:
    # 合并说明：sec33 分支承接 SEC-26 s1，未预期的非领域异常为 500 AUTOFIX_INTERNAL_ERROR
    # （K8s ApiException / 上游超时仍为 502 AUTOFIX_ERROR），这里按 s1 契约断言
    response = diagnose_raising(monkeypatch, RuntimeError(SECRET))
    assert response.status_code == 500
    assert SECRET not in response.text
    assert response.json()["data"]["error_code"] == "AUTOFIX_INTERNAL_ERROR"
