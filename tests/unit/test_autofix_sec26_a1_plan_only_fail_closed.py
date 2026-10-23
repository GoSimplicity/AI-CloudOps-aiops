"""SEC-26 A1：OpsMultiAgentWorkflow.run 的 plan_only 入口 fail-closed 归一化——只有显式 bool False 才可能免确认执行（全部离线，k8s 全部 mock）"""

import pytest

from app.core.agents.ops_workflow import OpsMultiAgentWorkflow
from app.core.autofix.remediation import AutoFixRemediationEngine
from tests.unit.test_autofix_sec26_run_lockdown import assert_no_writes, mock_k8s

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


async def test_explicit_false_and_default_still_follow_auto_execute() -> None:
    # 对照：显式 False（以及默认值 False）在实例 auto_execute=True 时仍按原行为执行
    for kwargs in ({"plan_only": False}, {}):
        client = mock_k8s()
        result = await auto_workflow(client).run(dict(REQUEST), **kwargs)
        assert result["status"] == "completed" and result["executed_actions"]


@pytest.mark.parametrize("value", NOT_FALSE, ids=repr)
def test_may_auto_execute_requires_state_plan_only_is_false(value) -> None:
    workflow = auto_workflow(mock_k8s())
    assert workflow._may_auto_execute({"plan_only": value}) is False
    assert workflow._may_auto_execute({}) is False  # 状态缺少 plan_only 同样 fail-closed
    assert workflow._may_auto_execute({"plan_only": False}) is True
    assert OpsMultiAgentWorkflow(remediation_engine=None, auto_execute=False)._may_auto_execute({"plan_only": False}) is False
