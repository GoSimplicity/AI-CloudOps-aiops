"""SEC-26 第二层审查 P3-1：确认后按剩余待确认动作重算 review.confirmation_required（全部离线，引擎/集群全部 mock）"""

from copy import deepcopy

from app.common.identity import principal_scope
from app.core.agents.ops_workflow import OpsMultiAgentWorkflow
from app.core.autofix.remediation import AutoFixRemediationEngine
from tests.unit.test_autofix_approval_isolation import (
    ACTION,
    create_plan,
    pending_result,
    service,  # noqa: F401 - pytest fixture
)
from tests.unit.test_autofix_sec26_run_lockdown import mock_k8s

CRASHLOOP = "CrashLoopBackOff restarting failed container"
REQUEST = {"problem_description": f"deployment api {CRASHLOOP}", "deployment": "api", "namespace": "default", "event": CRASHLOOP}


def two_action_service(service):  # noqa: F811
    original = pending_result()
    second = deepcopy(original["plan"]["blocked_actions"][0])
    second["action_id"] = "second-action"
    original["plan"]["candidate_actions"].append(second)
    original["plan"]["blocked_actions"].append(second)
    original["review"]["confirmation_required"] = True
    service._ops_workflow.run.side_effect = lambda _, **__: deepcopy(original)
    service._remediation_engine.build_plan.side_effect = lambda _, **__: deepcopy(original["plan"])
    service._remediation_engine.execute_plan.side_effect = lambda plan: {
        "status": "completed",
        "executed_actions": [{"action_id": action["action_id"], "status": "succeeded"} for action in plan["allowed_actions"]],
    }
    return service


async def test_partial_confirmation_keeps_confirmation_required_then_clears(service) -> None:  # noqa: F811
    service = two_action_service(service)
    plan_id = await create_plan(service)
    with principal_scope("user:1"):
        first = await service.confirm_multi_agent_workflow(plan_id, [ACTION])
        assert first["status"] == "needs_human_confirmation"
        assert first["review"]["confirmation_required"] is True
        assert first["approval"]["confirmation_required"] is True
        assert first["approval"]["pending_action_ids"] == ["second-action"]

        done = await service.confirm_multi_agent_workflow(plan_id, ["second-action"])
    assert done["status"] == "completed"
    assert done["review"]["confirmation_required"] is False
    assert done["review"]["approved"] is True and done["review"]["blocked_actions"] == []
    assert done["approval"]["state"] == "completed"
    assert done["approval"]["confirmation_required"] is False
    assert "pending_action_ids" not in done["approval"]


async def test_confirming_all_actions_at_once_clears_confirmation_required(service) -> None:  # noqa: F811
    service = two_action_service(service)
    plan_id = await create_plan(service)
    with principal_scope("user:1"):
        done = await service.confirm_multi_agent_workflow(plan_id, [ACTION, "second-action"])
    assert done["status"] == "completed"
    assert done["review"]["confirmation_required"] is False
    assert done["approval"]["confirmation_required"] is False


async def test_reviewer_sets_confirmation_required_false_when_nothing_pending() -> None:
    """reviewer 各分支都给出 confirmation_required：自动执行分支为 False，需确认分支为 True"""
    client = mock_k8s()
    engine = AutoFixRemediationEngine(client, allowed_namespaces=["default"], auto_execute=True)

    executed = await OpsMultiAgentWorkflow(remediation_engine=engine, auto_execute=True).run(dict(REQUEST))
    assert executed["status"] == "completed"
    assert executed["review"]["confirmation_required"] is False

    planned = await OpsMultiAgentWorkflow(remediation_engine=engine, auto_execute=True).run(dict(REQUEST), plan_only=True)
    assert planned["review"]["confirmation_required"] is True

    nothing = await OpsMultiAgentWorkflow(remediation_engine=engine, auto_execute=True).run({"problem_description": "deployment api 运行正常", "deployment": "api", "namespace": "default"})
    assert nothing["review"]["blocked_actions"] == [] and nothing["review"]["allowed_actions"] == []
    assert nothing["review"]["confirmation_required"] is False
