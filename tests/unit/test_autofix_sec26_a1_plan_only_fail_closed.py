"""SEC-26 A1：OpsMultiAgentWorkflow.run 的 plan_only 入口 fail-closed 归一化——只有显式 bool False 才可能免确认执行（全部离线，k8s 全部 mock）"""

import pytest

from app.core.agents.ops_workflow import OpsMultiAgentWorkflow
from app.core.autofix.remediation import AutoFixRemediationEngine
from tests.unit.test_autofix_sec26_run_lockdown import assert_no_writes, live_pods, mock_k8s

CRASHLOOP = "CrashLoopBackOff restarting failed container"
REQUEST = {"problem_description": f"deployment api {CRASHLOOP}", "deployment": "api", "namespace": "default", "event": CRASHLOOP}

NOT_FALSE = [True, "true", "True", "false", "False", "", "0", 1, 0, None, [], {}, 0.0]


def auto_workflow(client) -> OpsMultiAgentWorkflow:
    engine = AutoFixRemediationEngine(client, allowed_namespaces=["default"], auto_execute=True)
    return OpsMultiAgentWorkflow(remediation_engine=engine, auto_execute=True)


@pytest.mark.parametrize("value", NOT_FALSE, ids=repr)
async def test_non_false_plan_only_is_plan_only_even_when_auto_execute(value) -> None:
    client = mock_k8s()
    result = await auto_workflow(client).run(dict(REQUEST), plan_only=value)
    assert result["status"] == "needs_human_confirmation"
    assert result["executed_actions"] == []
    assert_no_writes(client)


async def test_explicit_false_and_default_still_follow_auto_execute(allow_autofix_writes) -> None:
    # 对照：显式 False（以及默认值 False）在实例 auto_execute=True 时仍按原行为执行；
    # SEC-44 起还要求实时 Pod 证据与诊断匹配，且写执行点按当前配置放行
    allow_autofix_writes("default", auto_execute=True)
    for kwargs in ({"plan_only": False}, {}):
        client = mock_k8s(pods=live_pods())
        result = await auto_workflow(client).run(dict(REQUEST), **kwargs)
        assert result["status"] == "completed" and result["executed_actions"]
    for kwargs in ({"plan_only": False}, {}):
        client = mock_k8s()  # 没有实时证据：同样的请求只出计划
        result = await auto_workflow(client).run(dict(REQUEST), **kwargs)
        assert result["status"] == "needs_human_confirmation" and result["executed_actions"] == []
        assert_no_writes(client)


def eligible_plan() -> dict:
    """满足 SEC-44 自动执行条件的计划（低风险白名单动作 + 匹配的实时证据）"""
    evidence = {"source": "live", "fault_type": "crash_loop_backoff", "checked": True, "matched": True}
    action = {"action_id": "restart-deployment", "action_type": "restart_deployment", "executable": True, "risk_assessment": {"allowed": True, "risk_level": "low", "live_evidence": evidence}}
    return {"allowed_actions": [action], "candidate_actions": [action]}


@pytest.mark.parametrize("value", NOT_FALSE, ids=repr)
def test_may_auto_execute_requires_state_plan_only_is_false(value) -> None:
    workflow = auto_workflow(mock_k8s())
    plan = eligible_plan()
    assert workflow._may_auto_execute({"plan_only": value, "plan": plan}) is False
    assert workflow._may_auto_execute({"plan": plan}) is False  # 状态缺少 plan_only 同样 fail-closed
    assert workflow._may_auto_execute({"plan_only": False, "plan": plan}) is True
    assert OpsMultiAgentWorkflow(remediation_engine=None, auto_execute=False)._may_auto_execute({"plan_only": False, "plan": plan}) is False
    # SEC-44：plan_only=False 也不够，计划里没有实时证据（或空计划）时同样 fail-closed
    no_evidence = eligible_plan()
    no_evidence["allowed_actions"][0]["risk_assessment"]["live_evidence"] = {"source": "none", "matched": False}
    assert workflow._may_auto_execute({"plan_only": False, "plan": no_evidence}) is False
    assert workflow._may_auto_execute({"plan_only": False, "plan": {}}) is False
