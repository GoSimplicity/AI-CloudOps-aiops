"""Safety boundaries for durable AutoFix operation identifiers."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.common.exceptions import AIOpsException
from app.common.identity import principal_scope
from app.services.autofix_service import AutoFixService
from app.services.workflow_approval_redis import RedisWorkflowApprovals
from app.services.workflow_approvals import WorkflowApprovals
from tests.unit.test_autofix_approval_isolation import ACTION, pending_result


async def test_missing_operation_id_forces_plan_only_even_if_auto_execute_is_enabled(monkeypatch):
    from app.config.settings import config

    monkeypatch.setattr(config.autofix, "auto_execute_enabled", True)
    svc = AutoFixService(approvals=WorkflowApprovals())
    svc._initialized = True
    svc._ops_workflow = SimpleNamespace(run=AsyncMock(return_value=deepcopy(pending_result())))
    with principal_scope("user:1"):
        result = await svc.execute_multi_agent_workflow({"problem_description": "offline"})
    assert svc._ops_workflow.run.await_args.kwargs["plan_only"] is True
    assert result["automatic_execution"]["enabled"] is False
    assert result["status"] == "needs_human_confirmation"


async def test_invalid_operation_id_does_not_run_workflow():
    svc = AutoFixService(approvals=WorkflowApprovals())
    svc._initialized = True
    svc._ops_workflow = SimpleNamespace(run=AsyncMock())
    with principal_scope("user:1"), pytest.raises(AIOpsException):
        await svc.execute_multi_agent_workflow({"problem_description": "offline", "operation_id": "invalid"})
    svc._ops_workflow.run.assert_not_awaited()


class UnconfirmedPipeline:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def config_get(self, *_args):
        return self

    def eval(self, *_args):
        return self

    def execute_command(self, *_args):
        return self

    async def execute(self):
        return [{"maxmemory-policy": "noeviction"}, ["created", "4102444800000"], [0, 0]]


class UnconfirmedRedis:
    def pipeline(self, **_kwargs):
        return UnconfirmedPipeline()


async def test_unconfirmed_aof_prevents_workflow_execution():
    svc = AutoFixService(approvals=RedisWorkflowApprovals(UnconfirmedRedis()))
    svc._initialized = True
    svc._ops_workflow = SimpleNamespace(run=AsyncMock())
    with principal_scope("user:1"), pytest.raises(AIOpsException) as raised:
        await svc.execute_multi_agent_workflow({"problem_description": "offline", "operation_id": "11111111-1111-4111-8111-111111111111"})
    assert raised.value.status_code == 503
    svc._ops_workflow.run.assert_not_awaited()


async def test_missing_operation_id_rejects_unexpected_execution():
    result = deepcopy(pending_result())
    result["executed_actions"] = [{"action_id": ACTION, "status": "succeeded"}]
    svc = AutoFixService(approvals=WorkflowApprovals())
    svc._initialized = True
    svc._ops_workflow = SimpleNamespace(run=AsyncMock(return_value=result))
    with principal_scope("user:1"), pytest.raises(AIOpsException) as raised:
        await svc.execute_multi_agent_workflow({"problem_description": "offline"})
    assert raised.value.status_code == 409


async def test_oversized_action_list_is_rejected_before_store_access():
    store = RedisWorkflowApprovals(UnconfirmedRedis())
    with principal_scope("user:1"), pytest.raises(AIOpsException):
        await store.claim("0" * 32, [f"action-{index}" for index in range(101)])


def test_approval_repr_does_not_expose_persisted_plan():
    from app.services.workflow_approvals import WorkflowApproval

    record = WorkflowApproval("a" * 32, "user:1", "pending", 0, "", result={"private": "secret-canary"})
    record.payload_snapshot = "snapshot-canary"
    assert "secret-canary" not in repr(record)
    assert "snapshot-canary" not in repr(record)
