"""SEC-26 P3-4：repair/bootstrap 未认证一律 401（开关开/关均如此）；计划生成后白名单收紧，确认返回 403 且不执行（全部离线）"""

from unittest.mock import AsyncMock

import pytest

from app.api.routes import autofix
from app.common.exceptions import AIOpsException
from app.common.identity import principal_scope
from app.config.settings import config
from app.services.autofix_service import AutoFixService
from tests.unit.test_autofix_sec26_repair_bootstrap import (
    KEY,
    REPAIR_BODY,
    assert_bootstrap_untouched,
    bootstrap_service,
    make_app,
)
from tests.unit.test_autofix_sec26_run_lockdown import EVENTS, assert_no_writes, make_service, mock_k8s

UNAUTHENTICATED = [
    {"X-API-Key": "", "X-CloudOps-User-ID": ""},
    {"X-API-Key": "wrong-" + KEY, "X-CloudOps-User-ID": "1"},
    {"X-API-Key": "", "X-CloudOps-User-ID": "1"},
]

@pytest.fixture(autouse=True)
def confirming_user():
    """SEC-44 修正：确认路径需要可信用户主体；本模块的确认流程以 user:1 执行"""
    with principal_scope("user:1"):
        yield



@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("headers", UNAUTHENTICATED)
def test_repair_unauthenticated_is_401_regardless_of_switch(monkeypatch, enabled, headers) -> None:
    monkeypatch.setattr(config.autofix, "repair_enabled", enabled)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"] if enabled else [])
    service = AutoFixService()
    monkeypatch.setattr(service, "initialize", AsyncMock(side_effect=AssertionError("must not initialize")))
    monkeypatch.setattr(service, "fix_kubernetes_deployment", AsyncMock(side_effect=AssertionError("must not run")))
    client = make_app(monkeypatch, service)

    response = client.post("/autofix/repair", json=REPAIR_BODY, headers=headers)

    assert response.status_code == 401
    assert "AUTOFIX_" not in response.text
    autofix.get_autofix_service.assert_not_awaited()
    service.fix_kubernetes_deployment.assert_not_awaited()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("headers", UNAUTHENTICATED)
def test_bootstrap_unauthenticated_is_401_regardless_of_switch(monkeypatch, enabled, headers) -> None:
    monkeypatch.setattr(config.autofix, "bootstrap_enabled", enabled)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["test"] if enabled else [])
    service, k8s_service = bootstrap_service(monkeypatch)
    monkeypatch.setattr(service, "initialize", AsyncMock(side_effect=AssertionError("must not initialize")))
    client = make_app(monkeypatch, service)

    response = client.post("/autofix/bootstrap", headers=headers)

    assert response.status_code == 401
    assert "AUTOFIX_" not in response.text
    assert_bootstrap_untouched(service, k8s_service)


async def _plan_restart(service):
    planned = await service.execute_multi_agent_workflow(
        {"problem_description": "deployment api CrashLoopBackOff", "deployment": "api", "namespace": "default", "event": EVENTS["crashloop"]}
    )
    restart = next(action for action in planned["blocked_actions"] if action["action_type"] == "restart_deployment")
    assert restart["risk_assessment"]["namespace_allowed"] is True
    return planned["plan"]["plan_id"], restart["action_id"]


@pytest.mark.parametrize("tightened", [[], ["staging"], ["Default"], ["*"]])
async def test_whitelist_tightened_after_plan_confirm_is_403_and_not_executed(monkeypatch, tightened) -> None:
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    plan_id, action_id = await _plan_restart(service)
    assert_no_writes(client)

    monkeypatch.setattr(config.autofix, "allowed_namespaces", tightened)
    with pytest.raises(AIOpsException) as denied:
        await service.confirm_multi_agent_workflow(plan_id, [action_id])

    assert denied.value.status_code == 403
    assert denied.value.error_code == "WORKFLOW_APPROVAL_ERROR"
    assert "命名空间" in denied.value.message
    assert_no_writes(client)


async def test_whitelist_unchanged_control_executes(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    plan_id, action_id = await _plan_restart(service)

    confirmed = await service.confirm_multi_agent_workflow(plan_id, [action_id])

    assert confirmed["status"] == "completed"
    assert client.restart_deployment.await_count == 1


async def test_whitelist_tightened_between_revalidation_and_execute_is_403(monkeypatch) -> None:
    """重新校验通过后、执行前白名单被收紧：执行阶段硬校验拒绝，403 且无任何写操作"""
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    plan_id, action_id = await _plan_restart(service)
    engine = service._remediation_engine
    original_build_plan = engine.build_plan

    async def build_then_tighten(diagnosis):
        plan = await original_build_plan(diagnosis)
        engine.allowed_namespaces = set()
        return plan

    monkeypatch.setattr(engine, "build_plan", build_then_tighten)
    with pytest.raises(AIOpsException) as denied:
        await service.confirm_multi_agent_workflow(plan_id, [action_id])

    assert denied.value.status_code == 403
    assert_no_writes(client)


@pytest.mark.parametrize("tighten", [True, False])
def test_whitelist_tightened_confirm_http_contract(monkeypatch, tighten) -> None:
    """同一 HTTP 主体生成计划并确认；收紧时 403 + error_code，未收紧的对照组正常执行（排除主体不匹配等其它原因）"""
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    k8s = mock_k8s()
    service = make_service(k8s)
    client = make_app(monkeypatch, service)
    planned = client.post(
        "/autofix/workflow",
        json={"problem_description": "deployment api CrashLoopBackOff", "deployment": "api", "namespace": "default", "event": EVENTS["crashloop"]},
    )
    assert planned.status_code == 200, planned.text
    data = planned.json()["data"]
    restart = next(action for action in data["blocked_actions"] if action["action_type"] == "restart_deployment")
    if tighten:
        monkeypatch.setattr(config.autofix, "allowed_namespaces", [])

    response = client.post("/autofix/workflow/confirm", json={"plan_id": data["plan"]["plan_id"], "approved_action_ids": [restart["action_id"]]})

    if tighten:
        assert response.status_code == 403
        payload = response.json()
        assert payload["code"] == 403
        assert payload["data"]["error_code"] == "WORKFLOW_APPROVAL_ERROR"
        assert "命名空间" in payload["message"]
        assert_no_writes(k8s)
    else:
        assert response.status_code == 200, response.text
        assert k8s.restart_deployment.await_count == 1
