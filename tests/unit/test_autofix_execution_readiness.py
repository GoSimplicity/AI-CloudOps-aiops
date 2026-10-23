from unittest.mock import AsyncMock

import pytest

from app.common.identity import principal_scope
from app.core.autofix.remediation import AutoFixRemediationEngine


@pytest.fixture(autouse=True)
def confirming_user():
    """SEC-44 修正：确认路径需要可信用户主体；本模块的确认流程以 user:1 执行"""
    with principal_scope("user:1"):
        yield



@pytest.mark.asyncio
@pytest.mark.parametrize("post_check", [{"ready": False}, None, {}, {"ready": 1}, {"ready": "true"}])
async def test_accepted_patch_without_confirmed_rollout_is_not_completed(post_check, allow_autofix_writes):
    allow_autofix_writes("owned")
    client = AsyncMock()
    client.restart_deployment.return_value = True
    client.wait_for_deployment_rollout.return_value = post_check
    engine = AutoFixRemediationEngine(client, allowed_namespaces=["owned"])
    result = await engine.execute_plan({"deployment": "owned", "namespace": "owned", "allowed_actions": [{"action_id": "restart", "action_type": "restart_deployment"}], "blocked_actions": []}, trigger="confirm")
    assert result["executed_actions"][0]["status"] == "succeeded"
    assert result["status"] == "failed"
    assert result["post_check"] == post_check


@pytest.mark.asyncio
async def test_failed_action_stops_later_writes_and_does_not_leak_exception_body(allow_autofix_writes):
    allow_autofix_writes("owned")
    client = AsyncMock()
    client.restart_deployment.side_effect = RuntimeError("private-exception-body-marker")
    client.wait_for_deployment_rollout.return_value = {"ready": True}
    engine = AutoFixRemediationEngine(client, allowed_namespaces=["owned"])
    actions = [{"action_id": "first", "action_type": "restart_deployment"}, {"action_id": "second", "action_type": "scale_deployment", "parameters": {"replicas": 2}}]
    result = await engine.execute_plan({"deployment": "owned", "namespace": "owned", "allowed_actions": actions, "blocked_actions": []}, trigger="confirm")
    assert result["status"] == "failed"
    client.scale_deployment.assert_not_called()
    assert result["not_attempted_actions"] == actions[1:]
    assert "private-exception-body-marker" not in str(result)


@pytest.mark.asyncio
async def test_post_check_exception_is_failed_and_sanitized(allow_autofix_writes):
    allow_autofix_writes("owned")
    client = AsyncMock()
    client.restart_deployment.return_value = True
    client.wait_for_deployment_rollout.side_effect = RuntimeError("private-post-check-marker")
    engine = AutoFixRemediationEngine(client, allowed_namespaces=["owned"])
    result = await engine.execute_plan({"deployment": "owned", "namespace": "owned", "allowed_actions": [{"action_id": "restart", "action_type": "restart_deployment"}], "blocked_actions": []}, trigger="confirm")
    assert result["status"] == "failed"
    assert "private-post-check-marker" not in str(result)
