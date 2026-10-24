"""SEC-26：/autofix/repair 与 /autofix/bootstrap 默认关闭、repair 走确认与白名单、移除硬编码镜像（全部离线）"""

from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.middleware.auth import setup_auth
from app.api.middleware.error_handler import setup_error_handlers
from app.api.routes import autofix
from app.common.exceptions import AIOpsException, FeatureDisabledError
from app.common.identity import principal_scope
from app.config.settings import AutoFixConfig, config
from app.services.autofix_service import AutoFixService
from tests.unit.test_autofix_sec26_run_lockdown import assert_no_writes, make_service, mock_k8s

KEY = "offline-autofix-sec26-service-key"
ROOT = Path(__file__).resolve().parents[2]
# 拼接构造，避免本测试文件自身包含被禁止的镜像字面量
FORBIDDEN_IMAGE = "nginx" + ":" + "1.21.6"
REPAIR_BODY = {"deployment": "api", "namespace": "default", "event": "CrashLoopBackOff restarting failed container"}

@pytest.fixture(autouse=True)
def confirming_user():
    """SEC-44 修正：确认路径需要可信用户主体；本模块的确认流程以 user:1 执行"""
    with principal_scope("user:1"):
        yield



def make_app(monkeypatch, service) -> TestClient:
    monkeypatch.setattr(autofix, "get_autofix_service", AsyncMock(return_value=service))
    monkeypatch.setattr(config.security, "api_key", KEY)
    app = FastAPI()
    setup_auth(app)
    setup_error_handlers(app)
    app.include_router(autofix.router, prefix="/autofix")
    return TestClient(app, headers={"X-API-Key": KEY, "X-CloudOps-User-ID": "1"})


def test_repair_and_bootstrap_config_defaults_are_off(monkeypatch) -> None:
    for key in ("AUTOFIX_REPAIR_ENABLED", "AUTOFIX_BOOTSTRAP_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    defaults = AutoFixConfig()
    assert defaults.repair_enabled is False
    assert defaults.bootstrap_enabled is False
    assert config.autofix.repair_enabled is False
    assert config.autofix.bootstrap_enabled is False
    monkeypatch.setenv("AUTOFIX_REPAIR_ENABLED", "true")
    monkeypatch.setenv("AUTOFIX_BOOTSTRAP_ENABLED", "TRUE")
    enabled = AutoFixConfig()
    assert enabled.repair_enabled is True and enabled.bootstrap_enabled is True


@pytest.mark.parametrize("path,body,switch", [("/autofix/repair", REPAIR_BODY, "repair_enabled"), ("/autofix/bootstrap", None, "bootstrap_enabled")])
def test_disabled_endpoints_return_403_without_initializing(monkeypatch, path, body, switch) -> None:
    monkeypatch.setattr(config.autofix, "repair_enabled", False)
    monkeypatch.setattr(config.autofix, "bootstrap_enabled", False)
    service = AutoFixService()
    initialize = AsyncMock(side_effect=AssertionError("disabled endpoint must not initialize kubernetes"))
    monkeypatch.setattr(service, "initialize", initialize)
    client = make_app(monkeypatch, service)

    response = client.post(path, json=body) if body is not None else client.post(path)

    assert response.status_code == 403
    payload = response.json()
    assert payload["code"] == 403
    assert f"autofix.{switch}" in payload["message"]
    initialize.assert_not_awaited()
    autofix.get_autofix_service.assert_not_awaited()


async def test_service_level_switches_are_defense_in_depth(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "repair_enabled", False)
    monkeypatch.setattr(config.autofix, "bootstrap_enabled", False)
    client = mock_k8s()
    service = make_service(client)
    with pytest.raises(FeatureDisabledError) as repair:
        await service.fix_kubernetes_deployment("api", "default", event="CrashLoopBackOff")
    with pytest.raises(FeatureDisabledError) as bootstrap:
        await service.bootstrap_test_resources()
    assert repair.value.status_code == 403 and bootstrap.value.status_code == 403
    assert_no_writes(client)


async def test_enabled_repair_only_plans_and_requires_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)

    planned = await service.fix_kubernetes_deployment("api", "default", event="ImagePullBackOff failed to pull image")

    assert_no_writes(client)
    assert planned["status"] == "needs_human_confirmation"
    assert planned["success"] is False and planned["actions_taken"] == []
    assert planned["approval"]["confirm_endpoint"] == "/api/v1/autofix/workflow/confirm"
    images = [
        container.get("image")
        for action in planned["candidate_actions"]
        for container in ((action.get("parameters") or {}).get("patch", {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers") or [])
    ]
    assert all(image is None for image in images), "repair 不得替换镜像"

    action_ids = planned["approval"]["pending_action_ids"]
    confirmed = await service.confirm_multi_agent_workflow(planned["plan"]["plan_id"], action_ids)
    assert confirmed["status"] == "completed"
    assert client.patch_deployment.await_count + client.restart_deployment.await_count + client.scale_deployment.await_count >= 1
    for call in client.patch_deployment.await_args_list:
        assert FORBIDDEN_IMAGE not in repr(call)


async def test_enabled_repair_rejects_force(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    with pytest.raises(AIOpsException) as raised:
        await service.fix_kubernetes_deployment("api", "default", force_fix=True, event="CrashLoopBackOff")
    assert "force" in raised.value.message
    assert_no_writes(client)


@pytest.mark.parametrize("allowed", [[], ["staging"]])
async def test_enabled_repair_respects_namespace_whitelist(monkeypatch, allowed) -> None:
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", allowed)
    client = mock_k8s("prod")
    service = make_service(client)
    planned = await service.fix_kubernetes_deployment("api", "prod", event="CrashLoopBackOff restarting failed container")
    assert_no_writes(client)
    restart = next(action for action in planned["blocked_actions"] if action["action_type"] == "restart_deployment")
    assert restart["risk_assessment"]["namespace_allowed"] is False

    with pytest.raises(AIOpsException) as denied:
        await service.confirm_multi_agent_workflow(planned["plan"]["plan_id"], [restart["action_id"]])
    assert denied.value.status_code == 403
    assert_no_writes(client)


def test_enabled_repair_route_returns_pending_contract(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    k8s = mock_k8s()
    service = make_service(k8s)
    client = make_app(monkeypatch, service)

    response = client.post("/autofix/repair", json=REPAIR_BODY)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "needs_human_confirmation"
    assert data["success"] is False and data["actions_taken"] == []
    assert data["plan_id"] and data["approval"]["confirmation_required"] is True
    assert data["approval"]["pending_action_ids"]
    assert_no_writes(k8s)

    forced = client.post("/autofix/repair", json={**REPAIR_BODY, "force": True})
    assert forced.status_code == 400
    assert_no_writes(k8s)


def test_hardcoded_fallback_image_removed_from_code() -> None:
    offenders = []
    for folder in ("app", "scripts", "tests"):
        for path in (ROOT / folder).rglob("*.py"):
            if FORBIDDEN_IMAGE in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


async def test_autofix_config_endpoint_exposes_switches(monkeypatch) -> None:
    service = make_service(mock_k8s())
    info = await service.get_autofix_config()
    policy = info["execution_policy"]
    assert policy == {
        "auto_execute_enabled": False,
        "allowed_namespaces": list(config.autofix.allowed_namespaces),
        "repair_enabled": False,
        "bootstrap_enabled": False,
        "confirm_endpoint": "/api/v1/autofix/workflow/confirm",
    }
    assert "update_image" not in info["fix_strategies"]


# ---------------------------------------------------------------------------
# SEC-26 追加：bootstrap 开启后 test 命名空间也必须在 allowed_namespaces 内
# ---------------------------------------------------------------------------


def bootstrap_service(monkeypatch):
    """已“初始化”的服务：k8s_service 为 AsyncMock，initialize/KubernetesService 一旦被调用即失败"""
    from unittest.mock import MagicMock

    from app.services import kubernetes as kubernetes_module

    monkeypatch.setattr(kubernetes_module, "KubernetesService", MagicMock(side_effect=AssertionError("must not build k8s client")))
    service = AutoFixService()
    service._initialized = True
    k8s_service = AsyncMock()
    k8s_service.apply_yaml_file.return_value = {"success": True}
    service._k8s_fixer = MagicMock(k8s_service=k8s_service)
    return service, k8s_service


def assert_bootstrap_untouched(service, k8s_service) -> None:
    k8s_service.ensure_namespace.assert_not_awaited()
    k8s_service.apply_yaml_file.assert_not_awaited()


@pytest.mark.parametrize("allowed", [[], ["staging"], ["staging", "default"]])
def test_enabled_bootstrap_requires_test_in_allowed_namespaces(monkeypatch, allowed) -> None:
    monkeypatch.setattr(config.autofix, "bootstrap_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", allowed)
    service, k8s_service = bootstrap_service(monkeypatch)
    initialize = AsyncMock(side_effect=AssertionError("namespace-denied bootstrap must not initialize kubernetes"))
    monkeypatch.setattr(service, "initialize", initialize)
    client = make_app(monkeypatch, service)

    response = client.post("/autofix/bootstrap")

    assert response.status_code == 403, response.text
    payload = response.json()
    assert payload["code"] == 403
    assert payload["data"]["error_code"] == "AUTOFIX_NAMESPACE_NOT_ALLOWED"
    assert payload["data"]["details"] == {"namespace": "test", "switch": "autofix.allowed_namespaces"}
    assert "命名空间 test" in payload["message"] and "AUTOFIX_ALLOWED_NAMESPACES" in payload["message"]
    if not allowed:
        assert "为空" in payload["message"]
    initialize.assert_not_awaited()
    autofix.get_autofix_service.assert_not_awaited()
    assert_bootstrap_untouched(service, k8s_service)


def test_enabled_bootstrap_with_test_whitelisted_applies_manifests(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "bootstrap_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["staging", "test"])
    service, k8s_service = bootstrap_service(monkeypatch)
    client = make_app(monkeypatch, service)

    response = client.post("/autofix/bootstrap")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "completed" and data["namespace"] == "test"
    k8s_service.ensure_namespace.assert_awaited_once_with("test")
    assert [call.kwargs["namespace"] for call in k8s_service.apply_yaml_file.await_args_list] == ["test", "test"]


def test_bootstrap_refuses_test_when_configured_as_restricted(monkeypatch) -> None:
    from app.core.autofix.remediation import AutoFixRemediationEngine

    monkeypatch.setattr(config.autofix, "bootstrap_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["test"])
    monkeypatch.setattr(AutoFixRemediationEngine, "DEFAULT_RESTRICTED_NAMESPACES", {"kube-system", "test"})
    service, k8s_service = bootstrap_service(monkeypatch)
    client = make_app(monkeypatch, service)

    response = client.post("/autofix/bootstrap")

    assert response.status_code == 403, response.text
    payload = response.json()
    assert payload["data"]["error_code"] == "AUTOFIX_NAMESPACE_NOT_ALLOWED"
    assert "受限命名空间" in payload["message"]
    autofix.get_autofix_service.assert_not_awaited()
    assert_bootstrap_untouched(service, k8s_service)
    # 与 run/confirm 使用同一判定
    assert service._build_remediation_engine(None)._namespace_allowed("test") is False


@pytest.mark.parametrize("allowed", [[], ["test"]])
def test_disabled_bootstrap_still_returns_feature_disabled(monkeypatch, allowed) -> None:
    monkeypatch.setattr(config.autofix, "bootstrap_enabled", False)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", allowed)
    service, k8s_service = bootstrap_service(monkeypatch)
    client = make_app(monkeypatch, service)

    response = client.post("/autofix/bootstrap")

    assert response.status_code == 403
    payload = response.json()
    assert payload["data"]["error_code"] == "AUTOFIX_FEATURE_DISABLED"
    assert payload["data"]["details"] == {"switch": "autofix.bootstrap_enabled"}
    assert "autofix.bootstrap_enabled" in payload["message"]
    autofix.get_autofix_service.assert_not_awaited()
    assert_bootstrap_untouched(service, k8s_service)


def test_bootstrap_without_principal_is_401_before_switch_checks(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "bootstrap_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", [])
    service, k8s_service = bootstrap_service(monkeypatch)
    client = make_app(monkeypatch, service)

    response = client.post("/autofix/bootstrap", headers={"X-API-Key": "", "X-CloudOps-User-ID": ""})

    assert response.status_code == 401
    assert_bootstrap_untouched(service, k8s_service)


async def test_service_level_bootstrap_namespace_check_is_defense_in_depth(monkeypatch) -> None:
    from app.common.exceptions import NamespaceNotAllowedError

    monkeypatch.setattr(config.autofix, "bootstrap_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    service, k8s_service = bootstrap_service(monkeypatch)
    with pytest.raises(NamespaceNotAllowedError) as denied:
        await service.bootstrap_test_resources()
    assert denied.value.status_code == 403
    assert denied.value.error_code == "AUTOFIX_NAMESPACE_NOT_ALLOWED"
    assert_bootstrap_untouched(service, k8s_service)


def test_repair_generic_failure_hides_detail_but_logs_full_exception(monkeypatch, caplog) -> None:
    import logging

    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    service = make_service(mock_k8s())
    secret_like = "internal-boom-detail-xyz"
    monkeypatch.setattr(service, "fix_kubernetes_deployment", AsyncMock(side_effect=RuntimeError(secret_like)))
    client = make_app(monkeypatch, service)

    with caplog.at_level(logging.ERROR, logger="aiops.api.autofix"):
        response = client.post("/autofix/repair", json=REPAIR_BODY)

    assert response.status_code >= 500
    assert secret_like not in response.text
    records = [r for r in caplog.records if r.name == "aiops.api.autofix" and "自动修复失败" in r.getMessage()]
    assert len(records) == 1
    record = records[0]
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError
    assert "RuntimeError" in record.getMessage() and "api" in record.getMessage()
    # 日志消息本身只含部署名和异常类型；不得带出 API Key / kubeconfig 等配置
    assert secret_like not in record.getMessage()
    assert KEY not in caplog.text
    assert "kubeconfig" not in caplog.text.lower()
