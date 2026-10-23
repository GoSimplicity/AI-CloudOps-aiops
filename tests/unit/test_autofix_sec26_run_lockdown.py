"""SEC-26：autofix.run 默认只出计划、显式确认后执行、命名空间白名单 fail-closed（全部离线）"""

from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from app.common.exceptions import AIOpsException
from app.config.settings import AutoFixConfig, _parse_namespace_list, config
from app.core.autofix.remediation import AutoFixRemediationEngine
from app.services.autofix_service import AutoFixService

WRITE_METHODS = ("patch_deployment", "scale_deployment", "restart_deployment")


def fake_deployment(namespace: str = "default", replicas: int = 1) -> Dict[str, Any]:
    return {
        "metadata": {"name": "api", "namespace": namespace},
        "spec": {
            "replicas": replicas,
            "selector": {"match_labels": {"app": "api"}},
            "template": {"spec": {"containers": [{"name": "api", "image": "repo/api:bad", "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}}}]}},
        },
    }


def live_pods(reason: str = "CrashLoopBackOff") -> list:
    """SEC-44：实时 Pod 状态（假值），容器 waiting 原因为 reason；用于满足自动执行的实时证据"""
    return [{"status": {"container_statuses": [{"name": "api", "restart_count": 5, "state": {"waiting": {"reason": reason}}}]}}]


def mock_k8s(namespace: str = "default", pods: list | None = None) -> AsyncMock:
    """pods=None 时 get_pods 返回非列表（AsyncMock 默认值）：没有实时证据，自动执行一律转人工确认"""
    client = AsyncMock()
    client.get_deployment.return_value = fake_deployment(namespace)
    if pods is not None:
        client.get_pods.return_value = pods
    client.can_i.return_value = True
    client.patch_deployment.return_value = True
    client.scale_deployment.return_value = True
    client.restart_deployment.return_value = True
    client.wait_for_deployment_rollout.return_value = {"ready": True, "replicas": 1}
    return client


def assert_no_writes(client: AsyncMock) -> None:
    for name in WRITE_METHODS:
        assert getattr(client, name).await_count == 0, name


def make_service(client: AsyncMock) -> AutoFixService:
    service = AutoFixService()
    service._initialized = True
    service._remediation_engine = service._build_remediation_engine(client)
    return service


EVENTS = {
    "crashloop": "CrashLoopBackOff restarting failed container",
    "resource": "FailedScheduling insufficient memory",
    "image_pull": "ImagePullBackOff failed to pull image",
}


def test_autofix_config_defaults_are_fail_closed(monkeypatch) -> None:
    for key in ("AUTOFIX_AUTO_EXECUTE_ENABLED", "AUTOFIX_ALLOWED_NAMESPACES"):
        monkeypatch.delenv(key, raising=False)
    defaults = AutoFixConfig()
    assert defaults.auto_execute_enabled is False
    assert defaults.allowed_namespaces == []


def test_allowed_namespaces_accepts_comma_string_and_list(monkeypatch) -> None:
    assert _parse_namespace_list(" a, b ,,a ") == ["a", "b"]
    assert _parse_namespace_list(["x", " y ", ""]) == ["x", "y"]
    assert _parse_namespace_list(None) == []
    monkeypatch.setenv("AUTOFIX_ALLOWED_NAMESPACES", "staging,dev")
    assert AutoFixConfig().allowed_namespaces == ["staging", "dev"]


@pytest.mark.parametrize("allowed", [[], ["default"]])
@pytest.mark.parametrize("event", sorted(EVENTS))
async def test_default_run_never_touches_kubernetes(monkeypatch, allowed, event) -> None:
    monkeypatch.setattr(config.autofix, "auto_execute_enabled", False)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", allowed)
    client = mock_k8s()
    service = make_service(client)

    result = await service.execute_multi_agent_workflow(
        {"problem_description": f"deployment api {EVENTS[event]}", "deployment": "api", "namespace": "default", "event": EVENTS[event]}
    )

    assert_no_writes(client)
    assert result["status"] == "needs_human_confirmation"
    assert "Executor" not in result["agents_used"]
    assert result["executed_actions"] == []
    assert result["plan"]["allowed_actions"] == []
    executable = [action for action in result["candidate_actions"] if action.get("executable")]
    assert executable and all(action["risk_assessment"]["allowed"] is False for action in executable)
    assert result["approval"]["state"] == "pending"
    assert result["approval"]["confirm_endpoint"] == "/api/v1/autofix/workflow/confirm"
    assert set(result["approval"]["pending_action_ids"]) == {action["action_id"] for action in executable}


async def test_engine_plan_and_execute_defaults_to_plan_only() -> None:
    client = mock_k8s()
    engine = AutoFixRemediationEngine(client, allowed_namespaces=["default"])
    result = await engine.plan_and_execute({"deployment": "api", "namespace": "default", "event": EVENTS["crashloop"]})
    assert result["status"] == "blocked"
    assert result["executed_actions"] == []
    assert_no_writes(client)
    restart = next(action for action in result["plan"]["candidate_actions"] if action["action_type"] == "restart_deployment")
    assert restart["risk_assessment"]["risk_level"] == "low"
    assert restart["risk_assessment"]["requires_human_confirmation"] is True
    assert "explicit confirmation required" in restart["risk_assessment"]["reasons"]


async def test_action_executes_only_after_explicit_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)

    planned = await service.execute_multi_agent_workflow(
        {"problem_description": "deployment api CrashLoopBackOff", "deployment": "api", "namespace": "default", "event": EVENTS["crashloop"]}
    )
    assert_no_writes(client)
    restart = next(action for action in planned["blocked_actions"] if action["action_type"] == "restart_deployment")

    confirmed = await service.confirm_multi_agent_workflow(planned["plan"]["plan_id"], [restart["action_id"]])

    assert client.restart_deployment.await_count == 1
    assert client.patch_deployment.await_count == 0
    assert client.scale_deployment.await_count == 0
    assert confirmed["status"] == "completed"
    assert [action["action_id"] for action in confirmed["executed_actions"]] == [restart["action_id"]]


async def test_partial_confirmation_executes_only_selected_action(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    planned = await service.execute_multi_agent_workflow(
        {"problem_description": "api insufficient memory", "deployment": "api", "namespace": "default", "event": EVENTS["resource"]}
    )
    ids = {action["action_type"]: action["action_id"] for action in planned["blocked_actions"]}
    assert {"scale_deployment", "adjust_resource_requests"} <= set(ids)

    confirmed = await service.confirm_multi_agent_workflow(planned["plan"]["plan_id"], [ids["scale_deployment"]])

    assert client.scale_deployment.await_count == 1
    assert client.patch_deployment.await_count == 0
    assert confirmed["status"] == "needs_human_confirmation"
    assert ids["adjust_resource_requests"] in confirmed["approval"]["pending_action_ids"]


@pytest.mark.parametrize("allowed", [[], ["staging"]])
async def test_namespace_outside_whitelist_cannot_be_confirmed(monkeypatch, allowed) -> None:
    monkeypatch.setattr(config.autofix, "allowed_namespaces", allowed)
    client = mock_k8s("prod")
    service = make_service(client)
    planned = await service.execute_multi_agent_workflow(
        {"problem_description": "prod api CrashLoopBackOff", "deployment": "api", "namespace": "prod", "event": EVENTS["crashloop"]}
    )
    restart = next(action for action in planned["blocked_actions"] if action["action_type"] == "restart_deployment")
    assert restart["risk_assessment"]["namespace_allowed"] is False
    assert restart["risk_assessment"]["risk_level"] == "high"

    with pytest.raises(AIOpsException) as denied:
        await service.confirm_multi_agent_workflow(planned["plan"]["plan_id"], [restart["action_id"]])

    assert denied.value.status_code == 403
    assert_no_writes(client)


@pytest.mark.parametrize("allowed,namespace", [([], "default"), (["default"], "prod"), (["kube-system"], "kube-system")])
async def test_engine_execute_plan_rejects_namespace_outside_whitelist(allowed, namespace) -> None:
    client = mock_k8s(namespace)
    engine = AutoFixRemediationEngine(client, allowed_namespaces=allowed, auto_execute=True)
    plan = {
        "deployment": "api",
        "namespace": namespace,
        "allowed_actions": [{"action_id": "restart", "action_type": "restart_deployment", "target": {"namespace": namespace}}],
        "blocked_actions": [],
    }
    result = await engine.execute_plan(plan)
    assert result["status"] == "blocked"
    assert result["executed_actions"] == []
    assert "allowed_namespaces" in result["error"]
    assert_no_writes(client)


async def test_engine_execute_plan_rejects_action_targeting_other_namespace() -> None:
    client = mock_k8s()
    engine = AutoFixRemediationEngine(client, allowed_namespaces=["default"], auto_execute=True)
    plan = {
        "deployment": "api",
        "namespace": "default",
        "allowed_actions": [{"action_id": "restart", "action_type": "restart_deployment", "target": {"namespace": "prod"}}],
        "blocked_actions": [],
    }
    result = await engine.execute_plan(plan)
    assert result["status"] == "blocked"
    assert_no_writes(client)


async def test_legacy_switch_restores_auto_execution_inside_whitelist_only(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "auto_execute_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s(pods=live_pods())
    service = make_service(client)
    result = await service.execute_multi_agent_workflow(
        {"problem_description": "deployment api CrashLoopBackOff", "deployment": "api", "namespace": "default", "event": EVENTS["crashloop"]}
    )
    assert result["status"] == "completed"
    assert client.restart_deployment.await_count == 1

    # SEC-44：同样的请求，实时 Pod 健康（没有证据）→ 转人工确认，不写集群
    healthy = mock_k8s(pods=[{"status": {"container_statuses": [{"name": "api", "restart_count": 0, "state": {"running": {}}}]}}])
    service = make_service(healthy)
    pending = await service.execute_multi_agent_workflow(
        {"problem_description": "deployment api CrashLoopBackOff", "deployment": "api", "namespace": "default", "event": EVENTS["crashloop"]}
    )
    assert pending["status"] == "needs_human_confirmation"
    assert_no_writes(healthy)

    other = mock_k8s("prod", pods=live_pods())
    service = make_service(other)
    blocked = await service.execute_multi_agent_workflow(
        {"problem_description": "prod api CrashLoopBackOff", "deployment": "api", "namespace": "prod", "event": EVENTS["crashloop"]}
    )
    assert blocked["status"] == "needs_human_confirmation"
    assert_no_writes(other)
