"""SEC-26 第二层审查 P2-1：/autofix/repair 固定只出计划，auto_execute_enabled 打开也不免确认执行（全部离线，k8s 全部 mock）"""

import asyncio

import pytest

from app.config.settings import config
from app.core.agents.ops_workflow import OpsMultiAgentWorkflow
from tests.unit.test_autofix_sec26_repair_bootstrap import REPAIR_BODY, make_app
from tests.unit.test_autofix_sec26_run_lockdown import WRITE_METHODS, assert_no_writes, live_pods, make_service, mock_k8s

CRASHLOOP = "CrashLoopBackOff restarting failed container"


def write_count(client) -> int:
    return sum(getattr(client, name).await_count for name in WRITE_METHODS)


@pytest.fixture
def both_switches_on(monkeypatch):
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "auto_execute_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])


async def test_repair_is_plan_only_even_when_auto_execute_enabled(both_switches_on) -> None:
    client = mock_k8s(pods=live_pods())  # 单副本 Deployment；SEC-44：实时证据也匹配，仍只出计划
    service = make_service(client)

    planned = await service.fix_kubernetes_deployment("api", "default", event=CRASHLOOP)

    assert write_count(client) == 0
    assert client.restart_deployment.await_count == 0
    assert planned["status"] == "needs_human_confirmation"
    assert planned["next_action"] == "human_confirm"
    assert "Executor" not in planned["agents_used"]
    assert planned["executed_actions"] == [] and planned["execution"] == {}
    assert planned["success"] is False and planned["actions_taken"] == []
    assert planned["review"]["confirmation_required"] is True
    restart = next(action for action in planned["blocked_actions"] if action["action_type"] == "restart_deployment")
    assert restart["action_id"] in planned["approval"]["pending_action_ids"]
    # 共享工作流实例未被改写：仍按配置开启 auto_execute
    assert service._ops_workflow.auto_execute is True

    confirmed = await service.confirm_multi_agent_workflow(planned["plan"]["plan_id"], [restart["action_id"]])
    assert confirmed["status"] == "completed"
    assert client.restart_deployment.await_count == 1


def test_repair_route_plan_only_when_auto_execute_enabled(monkeypatch, both_switches_on) -> None:
    k8s = mock_k8s()
    service = make_service(k8s)
    client = make_app(monkeypatch, service)

    response = client.post("/autofix/repair", json=REPAIR_BODY)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "needs_human_confirmation"
    assert data["approval"]["confirmation_required"] is True
    assert data["actions_taken"] == [] and data["success"] is False
    assert_no_writes(k8s)


async def test_repair_plan_only_does_not_leak_into_concurrent_run(both_switches_on) -> None:
    """plan_only 只随本次调用的状态传递：并发的 /autofix/run 仍按 auto_execute_enabled 执行，repair 仍只出计划"""
    client = mock_k8s(pods=live_pods())
    service = make_service(client)
    run_request = {"problem_description": f"deployment api {CRASHLOOP}", "deployment": "api", "namespace": "default", "event": CRASHLOOP}

    repaired, ran = await asyncio.gather(
        service.fix_kubernetes_deployment("api", "default", event=CRASHLOOP),
        service.execute_multi_agent_workflow(run_request),
    )

    assert repaired["status"] == "needs_human_confirmation" and repaired["executed_actions"] == []
    assert ran["status"] == "completed" and ran["executed_actions"]
    assert write_count(client) == len(ran["executed_actions"])


async def test_workflow_plan_only_overrides_instance_auto_execute_per_call(both_switches_on) -> None:
    from app.core.autofix.remediation import AutoFixRemediationEngine

    client = mock_k8s(pods=live_pods())
    workflow = OpsMultiAgentWorkflow(remediation_engine=AutoFixRemediationEngine(client, allowed_namespaces=["default"], auto_execute=True), auto_execute=True)
    request = {"problem_description": f"deployment api {CRASHLOOP}", "deployment": "api", "namespace": "default", "event": CRASHLOOP}

    planned = await workflow.run(request, plan_only=True)
    assert planned["status"] == "needs_human_confirmation" and planned["executed_actions"] == []
    assert_no_writes(client)
    assert workflow.auto_execute is True

    # 请求体里的 plan_only 不被采信，也不能放宽：只有调用方关键字参数生效
    executed = await workflow.run({**request, "plan_only": False})
    assert executed["status"] == "completed" and executed["executed_actions"]


async def test_repair_raises_conflict_if_plan_only_result_has_executions(both_switches_on, monkeypatch) -> None:
    """防御：即便工作流实现回归、plan_only 结果里出现执行记录，repair 也按结果不确定处理（409），不发布审批"""
    from app.common.exceptions import WorkflowApprovalError

    client = mock_k8s()
    service = make_service(client)

    async def regressed(_request, **_kwargs):
        return {"status": "completed", "executed_actions": [{"action_id": "x", "status": "succeeded"}], "execution": {"status": "completed"}}

    monkeypatch.setattr(service, "_run_multi_agent_workflow", regressed)
    with pytest.raises(WorkflowApprovalError) as raised:
        await service.fix_kubernetes_deployment("api", "default", event=CRASHLOOP)
    assert raised.value.status_code == 409
