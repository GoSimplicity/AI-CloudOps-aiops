"""SEC-26 P3-e：/autofix/diagnose 真实链路（路由 → AutoFixService → execute_with_timeout → KubernetesService → SDK）
上游超时 / K8s 不可用 → 503 SERVICE_UNAVAILABLE；未知异常 → 500 AUTOFIX_INTERNAL_ERROR；只 mock 最底层 SDK 调用（全部离线）"""

import asyncio
from pathlib import Path
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from kubernetes.client.rest import ApiException
import pytest
import urllib3

from app.common.constants import ServiceConstants
from app.common.exceptions import (
    AutoFixError,
    AutoFixInternalError,
    FeatureDisabledError,
    NamespaceNotAllowedError,
    RequestTimeoutError,
    ServiceUnavailableError,
    ValidationError,
    WorkflowApprovalError,
)
from app.config.settings import config
from app.services import autofix_service as autofix_service_module, kubernetes as kubernetes_module
from tests.unit.test_autofix_sec26_error_sanitization import service_with_fixer
from tests.unit.test_autofix_sec26_repair_bootstrap import make_app

SECRET = "token=sk-offline-leak-marker kubeconfig-server=https://10.0.0.1:6443"
BODY = {"deployment": "api", "namespace": "default", "include_pods": False, "include_events": False}


@pytest.fixture(params=[False, True], ids=["prod", "debug"])
def debug_mode(request, monkeypatch):
    # debug=True 时错误处理器会回显 message/details：两种模式都不得出现异常内容
    monkeypatch.setattr(config, "debug", request.param)
    return request.param


def real_k8s(read_deployment) -> kubernetes_module.KubernetesService:
    k8s = kubernetes_module.KubernetesService.__new__(kubernetes_module.KubernetesService)
    k8s.initialized = True
    k8s.apps_v1 = SimpleNamespace(read_namespaced_deployment=read_deployment)
    return k8s


def post_diagnose(monkeypatch, read_deployment, body=BODY):
    service = service_with_fixer(real_k8s(read_deployment))
    return make_app(monkeypatch, service).post("/autofix/diagnose", json=body)


def raising(exc):
    def sdk(**kwargs):
        raise exc

    return sdk


def assert_no_leak(response) -> None:
    assert SECRET not in response.text
    assert "10.0.0.1" not in response.text


UNAVAILABLE = [
    urllib3.exceptions.ReadTimeoutError(None, "https://10.0.0.1:6443", SECRET),
    urllib3.exceptions.ConnectTimeoutError(SECRET),
    ApiException(status=503, reason=SECRET),
    ApiException(status=500, reason=SECRET),
    ApiException(status=504, reason=SECRET),
    ApiException(status=429, reason=SECRET),
    ApiException(status=0, reason=SECRET),
    ApiException(reason=SECRET),  # 无状态码（连接级失败）
]


@pytest.mark.parametrize(
    "exc",
    UNAVAILABLE,
    ids=["read-timeout", "connect-timeout", "api-503", "api-500", "api-504", "api-429", "api-0", "api-none"],
)
def test_diagnose_upstream_unavailable_is_503(monkeypatch, debug_mode, exc) -> None:
    response = post_diagnose(monkeypatch, raising(exc))
    assert response.status_code == 503, response.text
    data = response.json()["data"]
    assert data["error_code"] == "SERVICE_UNAVAILABLE"
    assert not data.get("details")
    assert_no_leak(response)


def test_diagnose_execute_with_timeout_deadline_is_503(monkeypatch, debug_mode) -> None:
    monkeypatch.setattr(ServiceConstants, "AUTOFIX_K8S_TIMEOUT", 0.05)

    def slow(**kwargs):
        time.sleep(0.3)
        return SimpleNamespace(to_dict=lambda: {})

    response = post_diagnose(monkeypatch, slow)
    assert response.status_code == 503, response.text
    assert response.json()["data"]["error_code"] == "SERVICE_UNAVAILABLE"
    assert "timeout" not in (response.json()["data"].get("details") or {})


@pytest.mark.parametrize("status", [404, 403, 401, 400, 409])
def test_diagnose_non_unavailable_api_exception_keeps_none_deployment(monkeypatch, status) -> None:
    # 对照：非“不可用”类 ApiException 仍按原行为视为查不到 Deployment（200，deployment=None）
    response = post_diagnose(monkeypatch, raising(ApiException(status=status, reason=SECRET)))
    assert response.status_code == 200, response.text
    assert_no_leak(response)


def test_diagnose_success_control(monkeypatch) -> None:
    response = post_diagnose(monkeypatch, lambda **kwargs: SimpleNamespace(to_dict=lambda: {"metadata": {"name": "api"}}))
    assert response.status_code == 200, response.text


def test_diagnose_unknown_error_is_500_internal(monkeypatch, debug_mode) -> None:
    class Boom:
        @staticmethod
        def now():
            raise RuntimeError(SECRET)

    monkeypatch.setattr(autofix_service_module, "datetime", Boom)
    response = post_diagnose(monkeypatch, lambda **kwargs: SimpleNamespace(to_dict=lambda: {}))
    assert response.status_code == 500, response.text
    assert response.json()["data"]["error_code"] == "AUTOFIX_INTERNAL_ERROR"
    assert_no_leak(response)


def test_diagnose_empty_namespace_is_400(monkeypatch) -> None:
    response = post_diagnose(monkeypatch, lambda **kwargs: None, body={**BODY, "namespace": ""})
    assert response.status_code == 400, response.text


def test_get_deployment_default_behaviour_unchanged_for_other_callers() -> None:
    # raise_unavailable 默认 False：k8s_fixer / remediation 等其它调用方仍然拿到 None
    k8s = real_k8s(raising(ApiException(status=503, reason=SECRET)))
    assert asyncio.run(k8s.get_deployment("api", "default")) is None
    with pytest.raises(ApiException):
        asyncio.run(k8s.get_deployment("api", "default", raise_unavailable=True))


@pytest.mark.parametrize(
    "exc",
    [
        ValidationError("namespace", "bad"),
        AutoFixError("x"),
        AutoFixInternalError("x"),
        FeatureDisabledError("x", "autofix.repair_enabled"),
        NamespaceNotAllowedError("x", "kube-system"),
        WorkflowApprovalError(429, "x"),
        RequestTimeoutError("x"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_service_reraises_sanitized_domain_errors_unchanged(monkeypatch, exc) -> None:
    service = service_with_fixer(real_k8s(lambda **kwargs: None))
    monkeypatch.setattr(service, "execute_with_timeout", AsyncMock(side_effect=exc))
    with pytest.raises(type(exc)) as raised:
        await service.diagnose_kubernetes_issues("api", "default")
    assert raised.value is exc


async def test_service_reraises_service_unavailable_without_details(monkeypatch) -> None:
    original = ServiceUnavailableError("autofix", details={"error": SECRET})
    service = service_with_fixer(real_k8s(lambda **kwargs: None))
    monkeypatch.setattr(service, "execute_with_timeout", AsyncMock(side_effect=original))
    with pytest.raises(ServiceUnavailableError) as raised:
        await service.diagnose_kubernetes_issues("api", "default")
    assert raised.value.error_code == "SERVICE_UNAVAILABLE"
    assert raised.value.details == {}
    assert SECRET not in raised.value.message


@pytest.mark.parametrize(
    "exc",
    [ApiException(status=503, reason=SECRET), urllib3.exceptions.ReadTimeoutError(None, "u", SECRET), TimeoutError(SECRET)],
    ids=["api", "urllib3-timeout", "timeout"],
)
async def test_service_maps_unwrapped_upstream_errors_to_503(monkeypatch, exc) -> None:
    service = service_with_fixer(real_k8s(lambda **kwargs: None))
    monkeypatch.setattr(service, "execute_with_timeout", AsyncMock(side_effect=exc))
    with pytest.raises(ServiceUnavailableError) as raised:
        await service.diagnose_kubernetes_issues("api", "default")
    assert raised.value.details == {}


async def test_service_maps_unknown_error_to_internal_500(monkeypatch) -> None:
    service = service_with_fixer(real_k8s(lambda **kwargs: None))
    monkeypatch.setattr(service, "execute_with_timeout", AsyncMock(side_effect=KeyError(SECRET)))
    with pytest.raises(AutoFixInternalError) as raised:
        await service.diagnose_kubernetes_issues("api", "default")
    assert raised.value.error_code == "AUTOFIX_INTERNAL_ERROR"
    assert SECRET not in raised.value.message


@pytest.mark.parametrize(
    "exc,status,code",
    [
        (urllib3.exceptions.ReadTimeoutError(None, "u", SECRET), 503, "SERVICE_UNAVAILABLE"),
        (ApiException(status=500, reason=SECRET), 503, "SERVICE_UNAVAILABLE"),
        (RuntimeError(SECRET), 500, "AUTOFIX_INTERNAL_ERROR"),
    ],
    ids=["timeout", "api", "unknown"],
)
def test_route_fallback_mapping_for_errors_escaping_service(monkeypatch, debug_mode, exc, status, code) -> None:
    # 路由层兜底：服务层之外逃逸的非领域异常同样按 503 / 500 映射（不再是 502）
    service = service_with_fixer(real_k8s(lambda **kwargs: None))
    monkeypatch.setattr(service, "diagnose_kubernetes_issues", AsyncMock(side_effect=exc))
    response = make_app(monkeypatch, service).post("/autofix/diagnose", json=BODY)
    assert response.status_code == status, response.text
    assert response.json()["data"]["error_code"] == code
    assert_no_leak(response)


# ---------------------------------------------------------------- 文档（P3-e / A3）

REPO = Path(__file__).resolve().parents[2]


def _github_anchor(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def test_autofix_guide_diagnose_section_documents_503_and_500() -> None:
    guide = (REPO / "docs/autofix_guide.md").read_text(encoding="utf-8")
    section = guide.split("#### diagnose 错误映射", 1)[1].split("\n#### ", 1)[0]
    assert "503 `SERVICE_UNAVAILABLE`" in section
    assert "500 `AUTOFIX_INTERNAL_ERROR`" in section
    assert "仍可能出现的 502" in section
    assert "超时返回 502" not in guide
    assert "上游超时）返回 502" not in guide


def test_sec33_doc_links_diagnose_500_to_existing_guide_heading() -> None:
    doc = (REPO / "docs/release/SEC-33-mcp-write-gate.md").read_text(encoding="utf-8")
    assert "diagnose 未知异常返回 500" in doc
    links = re.findall(r"\]\(\.\./autofix_guide\.md#([^)]+)\)", doc)
    assert links
    guide = (REPO / "docs/autofix_guide.md").read_text(encoding="utf-8")
    anchors = {_github_anchor(line.lstrip("#")) for line in guide.splitlines() if line.startswith("#")}
    for anchor in links:
        assert anchor in anchors, anchor
