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
from app.config.settings import AutoFixConfig, config
from app.services.autofix_service import AutoFixService
from tests.unit.test_autofix_sec26_run_lockdown import assert_no_writes, make_service, mock_k8s

KEY = "offline-autofix-sec26-service-key"
ROOT = Path(__file__).resolve().parents[2]
# 拼接构造，避免本测试文件自身包含被禁止的镜像字面量
FORBIDDEN_IMAGE = "nginx" + ":" + "1.21.6"
REPAIR_BODY = {"deployment": "api", "namespace": "default", "event": "CrashLoopBackOff restarting failed container"}


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
