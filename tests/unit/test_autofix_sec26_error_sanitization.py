"""SEC-26 P3-1：autofix 响应不再携带 str(e)；服务端 logger.exception 保留堆栈；remediation 日志只记类型与 action_id（全部离线）"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.common.exceptions import AutoFixError, ServiceUnavailableError
from app.config.settings import config
from app.core.autofix.remediation import AutoFixRemediationEngine
from app.services import kubernetes as kubernetes_module
from app.services.autofix_service import AutoFixService
from app.services.workflow_approvals import WorkflowApprovals
from tests.unit.test_autofix_sec26_repair_bootstrap import make_app
from tests.unit.test_autofix_sec26_run_lockdown import mock_k8s

SECRET = "token=sk-offline-leak-marker kubeconfig-server=https://10.0.0.1:6443"


class LeakyError(RuntimeError):
    pass


def leak() -> LeakyError:
    return LeakyError(SECRET)


def assert_logged_with_traceback(caplog, logger_prefix: str, error_type: str = "LeakyError") -> None:
    records = [r for r in caplog.records if r.name.startswith(logger_prefix) and r.exc_info]
    assert records, "应通过 logger.exception 记录完整异常"
    for record in records:
        assert SECRET not in record.getMessage()  # 日志正文只有异常类型，异常内容仅在服务端堆栈里
        assert error_type in record.getMessage()


def assert_error_logged_without_traceback(caplog, logger_prefix: str, error_type: str) -> None:
    """SEC-33 L2 P3-1：diagnose 链路 ERROR 日志只有操作名/类型/状态，不带堆栈（异常链含原始文本），原始信息只进 DEBUG"""
    records = [r for r in caplog.records if r.name.startswith(logger_prefix) and r.levelno >= logging.ERROR]
    assert records
    assert any(error_type in r.getMessage() for r in records)
    for record in records:
        assert record.exc_info is None
        assert SECRET not in logging.Formatter().format(record)


@pytest.fixture(autouse=True)
def debug_on(monkeypatch):
    # debug 打开时全局处理器会回显 message/details，用最宽松的设置证明响应里也没有异常内容
    monkeypatch.setattr(config, "debug", True)


def service_with_fixer(k8s) -> AutoFixService:
    service = AutoFixService(approvals=WorkflowApprovals())
    service._initialized = True
    service._k8s_fixer = MagicMock(k8s_service=k8s)
    service._remediation_engine = service._build_remediation_engine(k8s)
    return service


def test_ready_route_hides_exception(monkeypatch, caplog) -> None:
    service = service_with_fixer(mock_k8s())
    monkeypatch.setattr(service, "initialize", AsyncMock(side_effect=leak()))
    client = make_app(monkeypatch, service)
    with caplog.at_level(logging.ERROR):
        response = client.get("/autofix/ready")
    assert response.status_code == 503
    assert SECRET not in response.text
    assert response.json()["data"]["error_code"] == "SERVICE_UNAVAILABLE"
    assert_logged_with_traceback(caplog, "aiops")


def test_bootstrap_route_hides_exception(monkeypatch, caplog) -> None:
    monkeypatch.setattr(config.autofix, "bootstrap_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["test"])
    service = service_with_fixer(mock_k8s())
    monkeypatch.setattr(service, "initialize", AsyncMock(side_effect=leak()))
    client = make_app(monkeypatch, service)
    with caplog.at_level(logging.ERROR):
        response = client.post("/autofix/bootstrap")
    # 承接 SEC-26 s1：非上游（非 ApiException/超时）的未预期异常 → 500，固定文案 + AUTOFIX_INTERNAL_ERROR
    assert response.status_code == 500
    assert SECRET not in response.text
    assert response.json()["data"]["detail"] == "自动修复内部错误: 引导故障资源失败"
    assert response.json()["data"]["error_code"] == "AUTOFIX_INTERNAL_ERROR"
    assert_logged_with_traceback(caplog, "aiops")


def test_diagnose_route_hides_exception(monkeypatch, caplog) -> None:
    service = service_with_fixer(mock_k8s())
    monkeypatch.setattr(service, "initialize", AsyncMock())
    monkeypatch.setattr(service, "diagnose_kubernetes_issues", AsyncMock(side_effect=leak()))
    client = make_app(monkeypatch, service)
    with caplog.at_level(logging.ERROR):
        response = client.post("/autofix/diagnose", json={"deployment": "api", "namespace": "default"})
    # 承接 SEC-26 s1：非上游（非 ApiException/超时）的未预期异常 → 500，固定文案 + AUTOFIX_INTERNAL_ERROR
    assert response.status_code == 500
    assert SECRET not in response.text
    assert response.json()["data"]["detail"] == "自动修复内部错误: 诊断操作失败"
    assert response.json()["data"]["error_code"] == "AUTOFIX_INTERNAL_ERROR"
    assert_logged_with_traceback(caplog, "aiops")


async def test_service_initialize_hides_exception(monkeypatch, caplog) -> None:
    monkeypatch.setattr(kubernetes_module, "KubernetesService", MagicMock(side_effect=leak()))
    service = AutoFixService(approvals=WorkflowApprovals())
    with caplog.at_level(logging.ERROR), pytest.raises(AutoFixError) as raised:
        await service._do_initialize()
    assert SECRET not in raised.value.message and SECRET not in str(raised.value.details)
    assert raised.value.message.endswith("初始化失败")
    assert_logged_with_traceback(caplog, "aiops")


async def test_service_bootstrap_hides_exception(monkeypatch, caplog) -> None:
    monkeypatch.setattr(config.autofix, "bootstrap_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["test"])
    k8s = mock_k8s()
    k8s.ensure_namespace.side_effect = leak()
    service = service_with_fixer(k8s)
    with caplog.at_level(logging.ERROR), pytest.raises(AutoFixError) as raised:
        await service.bootstrap_test_resources()
    assert SECRET not in raised.value.message
    assert raised.value.message.endswith("引导测试资源失败")
    assert_logged_with_traceback(caplog, "aiops")


async def test_service_pod_logs_failure_hides_exception(caplog) -> None:
    k8s = mock_k8s()
    k8s.get_pods.return_value = [{"name": "api-0"}]
    k8s.get_pod_logs.side_effect = leak()
    k8s.get_events.return_value = []
    service = service_with_fixer(k8s)
    with caplog.at_level(logging.ERROR):
        result = await service.diagnose_kubernetes_issues("api", "default", include_pods=True, include_logs=True)
    assert result["diagnosis"]["logs"] == {"api-0": "获取日志失败"}
    assert SECRET not in str(result)
    # SEC-33 L2 P3-1：diagnose 链路 ERROR 只记类型（取 execute_with_timeout 包装前的原始异常类型），不带堆栈
    assert_error_logged_without_traceback(caplog, "aiops.services.autofix", "LeakyError")


async def test_service_diagnose_failure_hides_exception(caplog) -> None:
    k8s = mock_k8s()
    k8s.get_deployment.side_effect = leak()
    service = service_with_fixer(k8s)
    # SEC-26 P3-e：K8s 调用失败经 execute_with_timeout 包装为上游不可用，diagnose 去掉 details 后以 503 上抛
    with caplog.at_level(logging.ERROR), pytest.raises(ServiceUnavailableError) as raised:
        await service.diagnose_kubernetes_issues("api", "default")
    assert SECRET not in raised.value.message
    assert raised.value.details == {}
    assert raised.value.error_code == "SERVICE_UNAVAILABLE"
    assert_error_logged_without_traceback(caplog, "aiops.services.autofix", "LeakyError")


async def test_service_health_info_hides_exception(monkeypatch, caplog) -> None:
    service = service_with_fixer(mock_k8s())
    monkeypatch.setattr(service, "health_check", AsyncMock(side_effect=leak()))
    with caplog.at_level(logging.ERROR):
        info = await service.get_service_health_info()
    assert info["error"] == "获取健康信息失败"
    assert SECRET not in str(info)
    assert_logged_with_traceback(caplog, "aiops")


async def test_remediation_permission_log_has_only_type_and_action_id(caplog) -> None:
    k8s = AsyncMock()
    k8s.can_i.side_effect = leak()
    engine = AutoFixRemediationEngine(k8s_client=k8s, allowed_namespaces=["default"])
    with caplog.at_level(logging.WARNING, logger="aiops"):
        allowed = await engine._permission_allowed({"action_id": "patch-image-pull-policy", "action_type": "patch_image_pull_policy"}, "default")
    assert allowed is False
    records = [r for r in caplog.records if "权限检查失败" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "patch-image-pull-policy" in message and "LeakyError" in message
    assert SECRET not in message
    assert records[0].exc_info is None  # 不附带堆栈，避免 ApiException body/headers 进入日志


async def test_remediation_get_deployment_log_has_only_type(caplog) -> None:
    k8s = AsyncMock()
    k8s.get_deployment.side_effect = leak()
    engine = AutoFixRemediationEngine(k8s_client=k8s, allowed_namespaces=["default"])
    with caplog.at_level(logging.WARNING, logger="aiops"):
        assert await engine._get_deployment("api", "default") is None
    records = [r for r in caplog.records if "获取Deployment失败" in r.getMessage()]
    assert len(records) == 1 and SECRET not in records[0].getMessage() and records[0].exc_info is None
