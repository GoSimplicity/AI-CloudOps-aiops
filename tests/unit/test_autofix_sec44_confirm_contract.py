"""SEC-44 二次修正（TL 04:00 追加 Q1/Q6/Q7/Q10）：确认接口的错误契约

- Q1：确认时意图审计写入失败、且没有任何动作执行 → 审批原子释放回 pending，可重试且只执行一次
- Q6：确认接口所有错误响应带 data.approval_state ∈ {pending, voided, consumed}
- Q7：确认缺少可信用户主体 → 403 + error_code=confirm_principal_required（不再是 401）
全部离线：假 SDK / 假审计 sink / 合成的假值。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Dict
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import httpx
import pytest

from app.api.decorators import AIOpsHTTPException
from app.api.routes import autofix
from app.common.exceptions import AIOpsException, FeatureDisabledError
from app.common.identity import principal_scope
from app.services import workflow_approvals
from app.services.workflow_approvals import WorkflowApprovals
from tests.unit.test_autofix_approval_isolation import ACTION, create_plan, service  # noqa: F401 - pytest fixture
from tests.unit.test_autofix_sec26_repair_bootstrap import KEY, make_app
from tests.unit.test_autofix_sec44_auto_execute import (  # noqa: F401 - pytest fixtures
    CRASHING,
    CRASHLOOP,
    SECRET_MARKER,
    FakeSDK,
    make_service,
    offline_rbac,
    pending_action,
    run_workflow,
    sink,
    switches,
)

CONFIRM = "/autofix/workflow/confirm"


def confirm_body(data: Dict[str, Any], action_type: str = "restart_deployment") -> Dict[str, Any]:
    return {"plan_id": data["plan"]["plan_id"], "approved_action_ids": [pending_action(data, action_type)["action_id"]]}


def error_data(response) -> Dict[str, Any]:
    payload = response.json()
    assert SECRET_MARKER not in response.text
    return payload["data"]


@pytest.fixture
def confirm_env(monkeypatch, switches, sink):  # noqa: F811
    switches(auto_execute=False)
    sdk = FakeSDK(pods=CRASHING)
    svc = make_service(sdk)
    client = make_app(monkeypatch, svc)
    data = run_workflow(client, CRASHLOOP)
    assert data["approval"]["state"] == "pending"
    return client, sdk, svc, data


# ---------------------------------------------------------------- Q1：审计失败释放审批（CAS）


def test_audit_failure_releases_approval_and_retry_executes_exactly_once(confirm_env, sink) -> None:  # noqa: F811
    client, sdk, svc, data = confirm_env
    body = confirm_body(data)

    sink.fail_phases = ("intent",)
    first = client.post(CONFIRM, json=body)
    assert first.status_code == 503, first.text
    assert error_data(first)["error_code"] == "confirm_audit_unavailable"
    assert error_data(first)["approval_state"] == "pending"
    assert sdk.writes() == []
    record = svc._approvals.records[body["plan_id"]]
    assert (record.state, record.effects_started, record.claim_token) == ("pending", False, "")
    assert record.result is not None

    sink.fail_phases = ()
    second = client.post(CONFIRM, json=body)
    assert second.status_code == 200, second.text
    assert sdk.writes() == ["patch_namespaced_deployment"]

    third = client.post(CONFIRM, json=body)
    assert third.status_code == 409
    assert error_data(third)["approval_state"] == "consumed"
    assert sdk.writes() == ["patch_namespaced_deployment"]


def test_concurrent_replay_during_audit_failure_claims_once(confirm_env, sink) -> None:  # noqa: F811
    """两个确认请求并发：只有一个领取审批；另一个 409。审计失败释放后重试只执行一次"""
    client, sdk, svc, data = confirm_env
    body = confirm_body(data)
    sink.fail_phases = ("intent",)

    async def replay():
        transport = httpx.ASGITransport(app=client.app)
        headers = {"X-API-Key": KEY, "X-CloudOps-User-ID": "1"}
        async with httpx.AsyncClient(transport=transport, base_url="http://offline", headers=headers) as http:
            entered = asyncio.Event()
            gate = asyncio.Event()
            original = svc._remediation_engine.execute_plan

            async def slow_execute(plan, **kwargs):
                entered.set()
                await gate.wait()
                return await original(plan, **kwargs)

            svc._remediation_engine.execute_plan = slow_execute
            first = asyncio.create_task(http.post(CONFIRM, json=body))
            await asyncio.wait_for(entered.wait(), 5)
            second = await http.post(CONFIRM, json=body)
            gate.set()
            return await first, second

    first, second = asyncio.run(replay())
    assert second.status_code == 409 and error_data(second)["approval_state"] == "consumed"
    assert first.status_code == 503 and error_data(first)["approval_state"] == "pending"
    assert sdk.writes() == []

    sink.fail_phases = ()
    retry = client.post(CONFIRM, json=body)
    assert retry.status_code == 200, retry.text
    assert sdk.writes() == ["patch_namespaced_deployment"]


async def test_service_concurrent_confirm_with_audit_refusal_is_released_once(service) -> None:  # noqa: F811
    plan_id = await create_plan(service)
    entered, gate = asyncio.Event(), asyncio.Event()

    async def execute(_plan, **_kwargs):
        entered.set()
        await gate.wait()
        return {"status": "blocked", "executed_actions": [], "not_attempted_actions": [{"action_id": ACTION}], "post_check": None, "refusal": "audit"}

    service._remediation_engine.execute_plan = AsyncMock(side_effect=execute)
    with principal_scope("user:1"):
        first = asyncio.create_task(service.confirm_multi_agent_workflow(plan_id, [ACTION]))
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(AIOpsException) as duplicate:
            await service.confirm_multi_agent_workflow(plan_id, [ACTION])
        assert duplicate.value.status_code == 409
        assert service._approvals.approval_state(plan_id) == "consumed"
        gate.set()
        with pytest.raises(AIOpsException) as released:
            await first
        assert released.value.status_code == 503
        assert service._approvals.approval_state(plan_id) == "pending"

        service._remediation_engine.execute_plan = AsyncMock(return_value={"status": "completed", "executed_actions": [{"action_id": ACTION, "status": "succeeded"}]})
        await service.confirm_multi_agent_workflow(plan_id, [ACTION])
        with pytest.raises(AIOpsException) as replay:
            await service.confirm_multi_agent_workflow(plan_id, [ACTION])
        assert service._approvals.approval_state(plan_id) == "consumed"
    assert replay.value.status_code == 409
    assert service._remediation_engine.execute_plan.await_count == 1


async def test_audit_refusal_after_effects_started_is_not_released(service) -> None:  # noqa: F811
    """执行器报告审计拒绝，但事后检查已存在（不能证明无副作用）→ 不释放，进入 uncertain"""
    plan_id = await create_plan(service)
    service._remediation_engine.execute_plan = AsyncMock(
        return_value={
            "status": "blocked",
            "executed_actions": [],
            "not_attempted_actions": [{"action_id": ACTION}],
            "post_check": {"healthy": False},
            "refusal": "audit",
        }
    )
    with principal_scope("user:1"):
        with pytest.raises(AIOpsException) as raised:
            await service.confirm_multi_agent_workflow(plan_id, [ACTION])
        assert raised.value.status_code == 503
        assert service._approvals.records[plan_id].state == "uncertain"
        assert service._approvals.approval_state(plan_id) == "consumed"
        with pytest.raises(AIOpsException) as again:
            await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert again.value.status_code == 409
    assert service._remediation_engine.execute_plan.await_count == 1


async def test_partial_execution_is_never_released(service) -> None:  # noqa: F811
    plan_id = await create_plan(service)
    service._remediation_engine.execute_plan = AsyncMock(return_value={"status": "failed", "executed_actions": [{"action_id": ACTION, "status": "failed"}]})
    with principal_scope("user:1"):
        with pytest.raises(AIOpsException) as raised:
            await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert raised.value.status_code == 409
    record = service._approvals.records[plan_id]
    assert record.state == "uncertain"
    assert service._approvals.release(record, record.claim_token) is False
    assert record.state == "uncertain"


def _claimed(approvals: WorkflowApprovals):
    with principal_scope("user:1"):
        record = approvals.reserve()
        record.result = {"plan": {"blocked_actions": [{"action_id": ACTION, "risk_assessment": {"namespace_allowed": True, "permission_allowed": True, "resource_exists": True}}]}}
        record.state = "pending"
        claimed = approvals.claim(record.plan_id, [ACTION])
    return claimed


def test_release_is_compare_and_set_on_claim_token() -> None:
    approvals = WorkflowApprovals()
    record = _claimed(approvals)
    token = record.claim_token
    assert token and record.state == "executing"
    result_before = deepcopy(record.result)

    assert approvals.release(record, "stale-token") is False
    assert approvals.release(record, "") is False
    assert record.state == "executing"
    assert approvals.release(record, token) is True
    assert (record.state, record.claim_token, record.result) == ("pending", "", result_before)
    assert approvals.release(record, token) is False  # 已释放：同一 token 不能再次释放

    with principal_scope("user:1"):
        approvals.claim(record.plan_id, [ACTION])
    assert record.claim_token and record.claim_token != token
    assert approvals.release(record, token) is False  # 旧领取者迟到的释放不能把新领取者的执行状态改回 pending
    assert record.state == "executing"


def test_release_refuses_after_effects_or_terminal_state() -> None:
    approvals = WorkflowApprovals()
    record = _claimed(approvals)
    record.effects_started = True
    assert approvals.release(record, record.claim_token) is False
    assert record.state == "executing"
    for state in ("completed", "uncertain", "invalidated", "pending", "building"):
        record.effects_started = False
        record.state = state
        assert approvals.release(record, record.claim_token) is False
        assert record.state == state


# ---------------------------------------------------------------- Q6：approval_state


@pytest.mark.parametrize(
    ("state", "expected"),
    [("pending", "pending"), ("executing", "consumed"), ("completed", "consumed"), ("uncertain", "consumed"), ("invalidated", "voided"), ("building", "voided")],
)
def test_approval_state_mapping(state, expected) -> None:
    approvals = WorkflowApprovals()
    with principal_scope("user:1"):
        record = approvals.reserve()
    record.state = state
    with principal_scope("user:1"):
        assert approvals.approval_state(record.plan_id) == expected
    with principal_scope("user:2"):
        assert approvals.approval_state(record.plan_id) == "voided"  # 不泄露他人审批的状态
    with principal_scope(None):
        assert approvals.approval_state(record.plan_id) == "pending"  # 无身份：未触碰审批
    assert expected in workflow_approvals.APPROVAL_STATES


def test_approval_state_unknown_or_expired_is_voided() -> None:
    now = [100.0]
    approvals = WorkflowApprovals(ttl=10, clock=lambda: now[0])
    with principal_scope("user:1"):
        record = approvals.reserve()
        record.state = "pending"
        assert approvals.approval_state("no-such-plan") == "voided"
        assert approvals.approval_state(None) == "voided"
        assert approvals.approval_state(record.plan_id) == "pending"
        now[0] += 11
        assert approvals.approval_state(record.plan_id) == "voided"


def test_unknown_plan_is_404_voided(confirm_env) -> None:
    client, sdk, _svc, _data = confirm_env
    response = client.post(CONFIRM, json={"plan_id": "0" * 32, "approved_action_ids": ["restart-deployment"]})
    assert response.status_code == 404
    assert error_data(response)["approval_state"] == "voided"


def test_foreign_owner_is_403_voided_without_leaking(confirm_env) -> None:
    client, sdk, svc, data = confirm_env
    other = TestClient(client.app, headers={"X-API-Key": KEY, "X-CloudOps-User-ID": "2"})
    response = other.post(CONFIRM, json=confirm_body(data))
    assert response.status_code == 403
    assert error_data(response)["approval_state"] == "voided"
    assert svc._approvals.records[data["plan"]["plan_id"]].state == "pending"
    assert sdk.writes() == []


def test_invalid_action_id_is_400_and_keeps_pending(confirm_env) -> None:
    client, sdk, _svc, data = confirm_env
    response = client.post(CONFIRM, json={"plan_id": data["plan"]["plan_id"], "approved_action_ids": ["not-in-plan"]})
    assert response.status_code == 400
    assert error_data(response)["approval_state"] == "pending"
    assert client.post(CONFIRM, json=confirm_body(data)).status_code == 200


def test_request_body_validation_error_is_pending(confirm_env) -> None:
    client, _sdk, _svc, _data = confirm_env
    response = client.post(CONFIRM, json={"approved_action_ids": ["x"]})
    assert response.status_code in (400, 422)
    assert error_data(response)["approval_state"] == "pending"


def test_completed_replay_is_409_consumed(confirm_env) -> None:
    client, _sdk, _svc, data = confirm_env
    assert client.post(CONFIRM, json=confirm_body(data)).status_code == 200
    response = client.post(CONFIRM, json=confirm_body(data))
    assert response.status_code == 409
    assert error_data(response)["approval_state"] == "consumed"


def test_mutation_gate_refusal_is_403_voided(confirm_env, monkeypatch) -> None:
    from app.core.autofix import policy

    client, sdk, _svc, data = confirm_env

    def closed_gate(*_args, **_kwargs):
        raise FeatureDisabledError("写操作已关闭", switch="autofix_enabled")

    monkeypatch.setattr(policy, "ensure_mutation_allowed", closed_gate)
    response = client.post(CONFIRM, json=confirm_body(data))
    assert response.status_code == 403
    assert error_data(response)["approval_state"] == "voided"
    assert sdk.writes() == []
    again = client.post(CONFIRM, json=confirm_body(data))
    assert again.status_code == 409 and error_data(again)["approval_state"] == "voided"


def test_principal_refusal_is_403_confirm_principal_required(monkeypatch, switches, sink) -> None:  # noqa: F811
    """Q7：service:internal（无 X-CloudOps-User-ID）拥有的计划被确认 → 403 + confirm_principal_required（不是 401，
    避免前端把它当成 token 过期去刷新/登出）"""
    switches(auto_execute=False)
    sdk = FakeSDK(pods=CRASHING)
    client = make_app(monkeypatch, make_service(sdk))
    internal = TestClient(client.app, headers={"X-API-Key": KEY})
    data = run_workflow(internal, CRASHLOOP)

    response = internal.post(CONFIRM, json=confirm_body(data))

    assert response.status_code == 403, response.text
    body = error_data(response)
    assert body["error_code"] == "confirm_principal_required"
    assert body["approval_state"] == "voided"
    assert sdk.writes() == []


def test_service_closing_is_503_pending(confirm_env) -> None:
    client, _sdk, svc, data = confirm_env
    svc._closing = True
    response = client.post(CONFIRM, json=confirm_body(data))
    assert response.status_code == 503
    assert error_data(response)["approval_state"] == "pending"
    svc._closing = False
    assert client.post(CONFIRM, json=confirm_body(data)).status_code == 200


def test_unexpected_route_error_is_500_with_state_and_no_leak(confirm_env, monkeypatch) -> None:
    client, _sdk, svc, data = confirm_env
    monkeypatch.setattr(svc, "confirm_multi_agent_workflow", AsyncMock(side_effect=RuntimeError(f"boom {SECRET_MARKER}")))
    response = client.post(CONFIRM, json=confirm_body(data))
    assert response.status_code == 500
    assert error_data(response)["approval_state"] == "pending"


def test_route_attaches_state_to_http_exception(confirm_env, monkeypatch) -> None:
    """路由层直接调用：AIOpsHTTPException 上带 approval_state，取自请求结束时的审批记录"""
    from app.models import AutoFixWorkflowConfirmRequest

    _client, _sdk, svc, data = confirm_env
    monkeypatch.setattr(autofix, "get_autofix_service", AsyncMock(return_value=svc))

    async def call():
        with principal_scope("user:2"):
            await autofix.confirm_workflow(request=AutoFixWorkflowConfirmRequest(**confirm_body(data)))

    with pytest.raises(AIOpsHTTPException) as raised:
        asyncio.run(call())
    assert raised.value.status_code == 403 and raised.value.approval_state == "voided"


def test_error_handler_only_emits_known_states() -> None:
    from app.common.exceptions import WorkflowApprovalError

    error = WorkflowApprovalError(409, "x")
    error.approval_state = "pending"
    assert error.approval_state == "pending"
    with pytest.raises(ValueError):
        error.approval_state = "released"
