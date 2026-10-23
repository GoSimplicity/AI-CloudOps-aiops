"""审批隔离与一次性执行只使用离线引擎，不接触模型或集群"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.middleware.auth import setup_auth
from app.api.middleware.error_handler import setup_error_handlers
from app.api.routes import autofix
from app.common.exceptions import AIOpsException
from app.common.identity import principal_scope
from app.config.settings import config
from app.services.autofix_service import AutoFixService

KEY = "offline-autofix-approval-service-key"
ACTION = "restart-deployment"


def pending_result():
    action = {
        "action_id": ACTION, "action_type": "restart_deployment", "executable": True,
        "parameters": {}, "namespace": "default", "deployment": "api",
        "risk_assessment": {"allowed": False, "requires_human_confirmation": True,
                            "namespace_allowed": True, "permission_allowed": True, "resource_exists": True},
    }
    plan = {"plan_id": "autofix-default-api-same-second", "namespace": "default", "deployment": "api",
            "candidate_actions": [action], "blocked_actions": [action], "allowed_actions": []}
    return {"status": "needs_human_confirmation", "plan": plan, "blocked_actions": [action],
            "review": {"approved": False}, "diagnosis": {"namespace": "default", "deployment": "api"},
            "executed_actions": []}


@pytest.fixture
def service():
    service = AutoFixService()
    service._initialized = True
    service._ops_workflow = SimpleNamespace(run=AsyncMock(side_effect=lambda _: deepcopy(pending_result())))
    service._remediation_engine = SimpleNamespace(
        build_plan=AsyncMock(side_effect=lambda _: deepcopy(pending_result()["plan"])),
        execute_plan=AsyncMock(return_value={"status": "completed", "executed_actions": [{"action_id": ACTION, "status": "succeeded"}]}),
    )
    return service


async def create_plan(service, owner="user:1"):
    with principal_scope(owner):
        result = await service.execute_multi_agent_workflow({"problem_description": "offline approval", "deployment": "api"})
    return result["plan"]["plan_id"]


@pytest.mark.parametrize("foreign", ["user:2", "service:internal"])
async def test_known_other_owner_plan_cannot_be_confirmed_or_read(service, foreign):
    plan_id = await create_plan(service)
    with principal_scope(foreign), pytest.raises(AIOpsException) as raised:
        await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert raised.value.status_code == 403
    service._remediation_engine.build_plan.assert_not_awaited()
    service._remediation_engine.execute_plan.assert_not_awaited()


async def test_same_target_and_second_never_overwrites_another_owner_plan(service):
    first = await create_plan(service, "user:1")
    second = await create_plan(service, "user:2")
    assert first != second


async def test_concurrent_confirmation_claims_once_before_any_execution(service):
    plan_id = await create_plan(service)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def execute(_):
        entered.set()
        await release.wait()
        return {"status": "completed", "executed_actions": [{"action_id": ACTION, "status": "succeeded"}]}

    service._remediation_engine.execute_plan.side_effect = execute
    with principal_scope("user:1"):
        first = asyncio.create_task(service.confirm_multi_agent_workflow(plan_id, [ACTION]))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(service.confirm_multi_agent_workflow(plan_id, [ACTION]))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert service._remediation_engine.execute_plan.await_count == 1
    assert sum(isinstance(result, dict) for result in results) == 1
    error = next(result for result in results if isinstance(result, AIOpsException))
    assert error.status_code == 409


async def test_failed_execution_cannot_be_blindly_retried(service):
    plan_id = await create_plan(service)
    service._remediation_engine.execute_plan.side_effect = RuntimeError("unknown side effect")
    with principal_scope("user:1"):
        with pytest.raises(AIOpsException) as first:
            await service.confirm_multi_agent_workflow(plan_id, [ACTION])
        assert first.value.status_code == 409
        assert "重试" in first.value.message and "可能" in first.value.message
        with pytest.raises(AIOpsException) as second:
            await service.confirm_multi_agent_workflow(plan_id, [ACTION])
        assert second.value.status_code == 409
    assert service._remediation_engine.execute_plan.await_count == 1


async def test_cancelled_execution_consumes_the_claim(service):
    plan_id = await create_plan(service)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def execute(_):
        entered.set()
        await release.wait()
        return {"status": "completed", "executed_actions": []}

    service._remediation_engine.execute_plan.side_effect = execute
    with principal_scope("user:1"):
        task = asyncio.create_task(service.confirm_multi_agent_workflow(plan_id, [ACTION]))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        with pytest.raises(AIOpsException) as repeated:
            await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert repeated.value.status_code == 409
    assert service._remediation_engine.execute_plan.await_count == 1


@pytest.mark.parametrize("action_ids", [["foreign-action"], [ACTION, ACTION]])
async def test_invalid_ids_reject_before_replanning_or_claiming(service, action_ids):
    plan_id = await create_plan(service)
    with principal_scope("user:1"), pytest.raises(AIOpsException):
        await service.confirm_multi_agent_workflow(plan_id, action_ids)
    service._remediation_engine.build_plan.assert_not_awaited()
    service._remediation_engine.execute_plan.assert_not_awaited()


async def test_approval_cannot_execute_changed_parameters_under_the_same_action_id(service):
    plan_id = await create_plan(service)
    updated = pending_result()["plan"]
    updated["blocked_actions"][0]["parameters"] = {"different": "unapproved"}
    service._remediation_engine.build_plan.side_effect = None
    service._remediation_engine.build_plan.return_value = updated
    with principal_scope("user:1"), pytest.raises(AIOpsException) as raised:
        await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert raised.value.status_code == 409
    service._remediation_engine.execute_plan.assert_not_awaited()


@pytest.mark.parametrize("method", ["execute", "confirm"])
async def test_direct_calls_without_trusted_principal_have_no_side_effects(service, method):
    plan_id = await create_plan(service)
    service._ops_workflow.run.reset_mock()
    with principal_scope(None), pytest.raises(AIOpsException) as raised:
        if method == "execute":
            await service.execute_multi_agent_workflow({"problem_description": "must reject"})
        else:
            await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert raised.value.status_code == 401
    service._ops_workflow.run.assert_not_awaited()
    service._remediation_engine.build_plan.assert_not_awaited()
    service._remediation_engine.execute_plan.assert_not_awaited()


@pytest.mark.parametrize("target,expected", [("foreign", 403), ("unknown", 404)])
def test_cold_http_confirmation_rejects_before_initialization(service, monkeypatch, target, expected):
    with principal_scope("user:1"):
        result = asyncio.run(service.execute_multi_agent_workflow({"problem_description": "offline"}))
    plan_id = result["plan"]["plan_id"] if target == "foreign" else "unknown-plan"
    service._initialized = False
    initialize = AsyncMock(side_effect=AssertionError("must not initialize for rejected approval"))
    monkeypatch.setattr(service, "initialize", initialize)
    monkeypatch.setattr(autofix, "get_autofix_service", AsyncMock(return_value=service))
    monkeypatch.setattr(config.security, "api_key", KEY)
    app = FastAPI()
    setup_auth(app)
    setup_error_handlers(app)
    app.include_router(autofix.router, prefix="/autofix")
    response = TestClient(app).post("/autofix/workflow/confirm", headers={"X-API-Key": KEY, "X-CloudOps-User-ID": "2"}, json={"plan_id": plan_id, "approved_action_ids": [ACTION]})
    assert response.status_code == expected and response.json()["code"] == expected
    initialize.assert_not_awaited()


@pytest.mark.parametrize("key", ["namespace_allowed", "permission_allowed", "resource_exists"])
async def test_human_cannot_override_stored_hard_restrictions(service, key):
    result = pending_result()
    result["plan"]["blocked_actions"][0]["risk_assessment"][key] = False
    service._ops_workflow.run.side_effect = lambda _: deepcopy(result)
    plan_id = await create_plan(service)
    with principal_scope("user:1"), pytest.raises(AIOpsException) as raised:
        await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert raised.value.status_code == 403
    service._remediation_engine.build_plan.assert_not_awaited()
    service._remediation_engine.execute_plan.assert_not_awaited()


@pytest.mark.parametrize("key", ["namespace_allowed", "permission_allowed", "resource_exists"])
async def test_revalidation_hard_restrictions_stop_execution(service, key):
    plan_id = await create_plan(service)
    result = pending_result()["plan"]
    result["candidate_actions"][0]["risk_assessment"][key] = False
    service._remediation_engine.build_plan.side_effect = None
    service._remediation_engine.build_plan.return_value = result
    with principal_scope("user:1"), pytest.raises(AIOpsException) as raised:
        await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert raised.value.status_code == 409
    service._remediation_engine.execute_plan.assert_not_awaited()


async def test_newly_generated_allowed_actions_are_not_implicitly_executed(service):
    plan_id = await create_plan(service)
    plan = pending_result()["plan"]
    extra = deepcopy(plan["candidate_actions"][0])
    extra["action_id"] = "new-unapproved-action"
    extra["risk_assessment"]["allowed"] = True
    plan["candidate_actions"].append(extra)
    plan["allowed_actions"].append(extra)
    service._remediation_engine.build_plan.side_effect = None
    service._remediation_engine.build_plan.return_value = plan
    with principal_scope("user:1"):
        await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    execution_plan = service._remediation_engine.execute_plan.call_args.args[0]
    assert [action["action_id"] for action in execution_plan["allowed_actions"]] == [ACTION]


async def test_reported_execution_failure_consumes_approval_even_with_remaining_actions(service):
    plan_id = await create_plan(service)
    service._remediation_engine.execute_plan.return_value = {"status": "failed", "executed_actions": [{"action_id": ACTION, "status": "failed"}]}
    with principal_scope("user:1"):
        for _ in range(2):
            with pytest.raises(AIOpsException) as raised:
                await service.confirm_multi_agent_workflow(plan_id, [ACTION])
            assert raised.value.status_code == 409
    assert service._remediation_engine.execute_plan.await_count == 1


async def test_partial_confirmation_only_allows_remaining_original_actions(service):
    original = pending_result()
    second = deepcopy(original["plan"]["blocked_actions"][0])
    second["action_id"] = "second-action"
    original["plan"]["candidate_actions"].append(second)
    original["plan"]["blocked_actions"].append(second)
    service._ops_workflow.run.side_effect = lambda _: deepcopy(original)
    service._remediation_engine.build_plan.side_effect = lambda _: deepcopy(original["plan"])
    service._remediation_engine.execute_plan.side_effect = lambda plan: {
        "status": "completed", "executed_actions": [{"action_id": action["action_id"], "status": "succeeded"} for action in plan["allowed_actions"]],
    }
    plan_id = await create_plan(service)
    with principal_scope("user:1"):
        first = await service.confirm_multi_agent_workflow(plan_id, [ACTION])
        assert first["status"] == "needs_human_confirmation"
        with pytest.raises(AIOpsException):
            await service.confirm_multi_agent_workflow(plan_id, [ACTION])
        result = await service.confirm_multi_agent_workflow(plan_id, ["second-action"])
        assert result["status"] == "completed"
        assert [action["action_id"] for action in result["executed_actions"]] == [ACTION, "second-action"]
        with pytest.raises(AIOpsException) as completed:
            await service.confirm_multi_agent_workflow(plan_id, ["second-action"])
        assert completed.value.status_code == 409
    assert service._remediation_engine.execute_plan.await_count == 2


async def test_internal_plan_and_user_plan_are_isolated_both_ways(service):
    plan_id = await create_plan(service, "service:internal")
    with principal_scope("user:1"), pytest.raises(AIOpsException) as foreign:
        await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert foreign.value.status_code == 403
    with principal_scope("service:internal"):
        result = await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert result["status"] == "completed"


async def test_payload_cannot_select_another_approval_owner(service):
    with principal_scope("user:1"):
        result = await service.execute_multi_agent_workflow({"problem_description": "offline", "user_id": "2", "owner": "user:2"})
    with principal_scope("user:2"), pytest.raises(AIOpsException) as denied:
        await service.confirm_multi_agent_workflow(result["plan"]["plan_id"], [ACTION])
    assert denied.value.status_code == 403


def test_approval_capacity_expiry_and_active_claim_lifecycle():
    from app.services.workflow_approvals import WorkflowApprovals

    clock = [0.0]
    store = WorkflowApprovals(maximum=2, per_owner=1, ttl=10, clock=lambda: clock[0])
    with principal_scope("user:1"):
        first = store.reserve()
        store.publish(first, deepcopy(pending_result()))
        with pytest.raises(AIOpsException) as full:
            store.reserve()
        assert full.value.status_code == 429
        clock[0] = 11
        with pytest.raises(AIOpsException) as expired:
            store.get(first.plan_id)
        assert expired.value.status_code == 404
        active = store.reserve()
        store.publish(active, deepcopy(pending_result()))
        store.claim(active.plan_id, [ACTION])
        active.effects_started = True
        clock[0] = 100
        store.cleanup_expired()
        assert store.get(active.plan_id).state == "executing"
        store.fail(active, "uncertain; cannot retry")
    with principal_scope("user:2"):
        building = store.reserve()
    with principal_scope("user:3"), pytest.raises(AIOpsException) as globally_full:
        store.reserve()
    assert globally_full.value.status_code == 429
    clock[0] = 111
    store.cleanup_expired()
    assert list(store.records) == [building.plan_id]
    store.clear()
    assert store.records == {}


async def test_capacity_is_reserved_before_initialization_or_workflow_side_effects(service, monkeypatch):
    service._approvals.per_owner = 1
    await create_plan(service)
    initialize = AsyncMock(side_effect=AssertionError("capacity must be checked first"))
    monkeypatch.setattr(service, "initialize", initialize)
    service._ops_workflow.run.reset_mock()
    with principal_scope("user:1"), pytest.raises(AIOpsException) as full:
        await service.execute_multi_agent_workflow({"problem_description": "must reject"})
    assert full.value.status_code == 429
    initialize.assert_not_awaited()
    service._ops_workflow.run.assert_not_awaited()


async def test_legacy_unowned_record_is_not_adopted(service):
    service._pending_workflows = {"legacy": pending_result()}
    with principal_scope("user:1"), pytest.raises(AIOpsException) as denied:
        await service.confirm_multi_agent_workflow("legacy", [ACTION])
    assert denied.value.status_code == 404
    service._remediation_engine.build_plan.assert_not_awaited()


async def test_initialization_failure_does_not_leak_capacity(service, monkeypatch):
    initialize = AsyncMock(side_effect=RuntimeError("offline initialization failure"))
    monkeypatch.setattr(service, "initialize", initialize)
    with principal_scope("user:1"), pytest.raises(RuntimeError):
        await service.execute_multi_agent_workflow({"problem_description": "offline"})
    assert service._approvals.records == {}


async def test_concurrent_contexts_never_publish_another_owner_result(service):
    from app.common.identity import require_principal

    arrived = 0
    release = asyncio.Event()
    owners = []

    async def run(_):
        nonlocal arrived
        before = require_principal()
        arrived += 1
        if arrived == 2:
            release.set()
        await release.wait()
        owners.append((before, require_principal()))
        return deepcopy(pending_result())

    service._ops_workflow.run.side_effect = run
    first, second = await asyncio.gather(create_plan(service, "user:1"), create_plan(service, "user:2"))
    assert sorted(owners) == [("user:1", "user:1"), ("user:2", "user:2")]
    for owner, own, foreign in [("user:1", first, second), ("user:2", second, first)]:
        with principal_scope(owner):
            assert service._approvals.get(own).owner == owner
            with pytest.raises(AIOpsException) as denied:
                service._approvals.get(foreign)
            assert denied.value.status_code == 403


async def test_service_cleanup_removes_all_pending_and_terminal_records(service):
    await create_plan(service)
    await service.cleanup()
    assert service._approvals.records == {}


@pytest.mark.parametrize("records", [[], [{"action_id": ACTION, "status": "failed"}], [{"action_id": "unexpected", "status": "succeeded"}]])
async def test_incomplete_or_unexpected_execution_records_never_report_success(service, records):
    plan_id = await create_plan(service)
    service._remediation_engine.execute_plan.return_value = {"status": "completed", "executed_actions": records}
    with principal_scope("user:1"), pytest.raises(AIOpsException) as failed:
        await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert failed.value.status_code == 409
    with principal_scope("user:1"), pytest.raises(AIOpsException) as repeated:
        await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert repeated.value.status_code == 409
    assert service._remediation_engine.execute_plan.await_count == 1


@pytest.mark.parametrize("route", ["/workflow", "/workflow/confirm", "/bootstrap"])
def test_development_without_configured_key_cannot_self_report_identity(service, monkeypatch, route):
    monkeypatch.setattr(config.security, "api_key", "")
    getter = AsyncMock(return_value=service)
    monkeypatch.setattr(autofix, "get_autofix_service", getter)
    app = FastAPI()
    setup_auth(app)
    setup_error_handlers(app)
    app.include_router(autofix.router, prefix="/autofix")
    payload = {"problem_description": "offline", "plan_id": "unknown", "approved_action_ids": [ACTION]}
    response = TestClient(app).post("/autofix" + route, json=payload, headers={"X-CloudOps-User-ID": "1"})
    assert response.status_code == 401
    getter.assert_not_awaited()


@pytest.mark.parametrize("method,args", [("fix_kubernetes_deployment", {"deployment": "api"}), ("bootstrap_test_resources", {}), ("diagnose_kubernetes_issues", {})])
async def test_legacy_cluster_entrypoints_require_principal_before_dependencies(service, method, args):
    with principal_scope(None), pytest.raises(AIOpsException) as denied:
        await getattr(service, method)(**args)
    assert denied.value.status_code == 401


async def test_oversized_plan_snapshot_is_not_retained(service):
    result = pending_result()
    result["diagnosis"]["oversized"] = "x" * (1024 * 1024)
    service._ops_workflow.run.side_effect = lambda _: deepcopy(result)
    with principal_scope("user:1"), pytest.raises(AIOpsException) as rejected:
        await service.execute_multi_agent_workflow({"problem_description": "offline"})
    assert rejected.value.status_code == 409
    assert len(service._approvals.records) == 1
    record = next(iter(service._approvals.records.values()))
    assert record.state == "uncertain" and record.result is None


async def test_factory_getter_does_not_initialize_before_approval_authorization(service, monkeypatch):
    from app.services.factory import ServiceFactory

    getter = AsyncMock(return_value=service)
    monkeypatch.setattr(autofix, "autofix_service", None)
    monkeypatch.setattr(ServiceFactory, "get_service", getter)
    returned = await autofix.get_autofix_service()
    assert returned is service
    getter.assert_awaited_once_with("autofix", AutoFixService, initialize=False)


def approval_http_client(service, monkeypatch):
    monkeypatch.setattr(autofix, "get_autofix_service", AsyncMock(return_value=service))
    monkeypatch.setattr(config.security, "api_key", KEY)
    app = FastAPI()
    setup_auth(app)
    setup_error_handlers(app)
    app.include_router(autofix.router, prefix="/autofix")
    return TestClient(app)


def test_http_confirmation_timeout_is_explicitly_uncertain_and_not_retryable(service, monkeypatch):
    from app.common.constants import ServiceConstants

    plan_id = asyncio.run(create_plan(service))

    async def slow(_):
        await asyncio.sleep(1)
        return {"status": "completed", "executed_actions": []}

    service._remediation_engine.execute_plan.side_effect = slow
    monkeypatch.setattr(ServiceConstants, "AUTOFIX_WORKFLOW_TIMEOUT", 0.01)
    with approval_http_client(service, monkeypatch) as client:
        headers = {"X-API-Key": KEY, "X-CloudOps-User-ID": "1"}
        payload = {"plan_id": plan_id, "approved_action_ids": [ACTION]}
        response = client.post("/autofix/workflow/confirm", headers=headers, json=payload)
        assert response.status_code == 409 and response.json()["code"] == 409
        assert "超时" in response.json()["message"] and "重试" in response.json()["message"]
        repeated = client.post("/autofix/workflow/confirm", headers=headers, json=payload)
        assert repeated.status_code == 409
    assert service._remediation_engine.execute_plan.await_count == 1


def test_http_workflow_timeout_never_returns_retry_safe_generic_server_error(service, monkeypatch):
    from app.common.constants import ServiceConstants

    async def slow(_):
        await asyncio.sleep(1)
        return deepcopy(pending_result())

    service._ops_workflow.run.side_effect = slow
    monkeypatch.setattr(ServiceConstants, "AUTOFIX_WORKFLOW_TIMEOUT", 0.01)
    with approval_http_client(service, monkeypatch) as client:
        response = client.post("/autofix/workflow", headers={"X-API-Key": KEY, "X-CloudOps-User-ID": "1"}, json={"problem_description": "offline"})
        assert response.status_code == 409
        assert "可能" in response.json()["message"] and "重试" in response.json()["message"]
    assert len(service._approvals.records) == 1
    assert next(iter(service._approvals.records.values())).state == "uncertain"


async def test_cleanup_cancels_and_waits_for_confirmation_before_discarding_state(service):
    plan_id = await create_plan(service)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def execution(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service._remediation_engine.execute_plan.side_effect = execution
    with principal_scope("user:1"):
        task = asyncio.create_task(service.confirm_multi_agent_workflow(plan_id, [ACTION]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await service.cleanup()
            assert task.done() and task.cancelled()
            assert cancelled.is_set()
            assert service._approvals.records == {}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_closed_service_rejects_new_workflow_without_initialization(service, monkeypatch):
    await service.cleanup()
    initialize = AsyncMock(side_effect=AssertionError("closed service must not reinitialize"))
    monkeypatch.setattr(service, "initialize", initialize)
    with principal_scope("user:1"), pytest.raises(AIOpsException) as closed:
        await service.execute_multi_agent_workflow({"problem_description": "must reject"})
    assert closed.value.status_code == 503
    initialize.assert_not_awaited()


@pytest.mark.parametrize("global_limit", [False, True])
async def test_inflight_operation_limits_are_bounded_and_release_after_completion(service, global_limit):
    count = 64 if global_limit else 4
    arrived = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run(_):
        nonlocal arrived
        arrived += 1
        if arrived == count:
            entered.set()
        await release.wait()
        return deepcopy(pending_result())

    service._ops_workflow.run.side_effect = run
    tasks = [asyncio.create_task(create_plan(service, f"user:{index + 1}" if global_limit else "user:1")) for index in range(count)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(AIOpsException) as full:
            await create_plan(service, "user:65" if global_limit else "user:1")
        assert full.value.status_code == 429
        assert service._ops_workflow.run.await_count == count
        assert len(service._active_operations) == count
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert service._active_operations == {}


def test_http_principal_ownership_and_duplicate_approval_statuses(service, monkeypatch):
    from app.common.identity import require_principal

    async def run(_):
        result = deepcopy(pending_result())
        result["diagnosis"]["private_marker"] = require_principal() + "-private"
        return result

    service._ops_workflow.run.side_effect = run
    with approval_http_client(service, monkeypatch) as client:
        one = {"X-API-Key": KEY, "X-CloudOps-User-ID": "1"}
        two = {"X-API-Key": KEY, "X-CloudOps-User-ID": "2"}
        internal = {"X-API-Key": KEY}
        created = client.post("/autofix/workflow", headers=one, json={"problem_description": "offline", "owner": "user:2"})
        assert created.status_code == 200
        assert created.json()["data"]["diagnosis"]["private_marker"] == "user:1-private"
        plan_id = created.json()["data"]["plan"]["plan_id"]
        payload = {"plan_id": plan_id, "approved_action_ids": [ACTION]}
        for headers in [two, internal]:
            denied = client.post("/autofix/workflow/confirm", headers=headers, json=payload)
            assert denied.status_code == 403 and denied.json()["code"] == 403
            assert "user:1-private" not in denied.text
        service._remediation_engine.build_plan.assert_not_awaited()
        allowed = client.post("/autofix/workflow/confirm", headers=one, json=payload)
        assert allowed.status_code == 200 and allowed.json()["code"] == 0
        duplicate = client.post("/autofix/workflow/confirm", headers=one, json=payload)
        assert duplicate.status_code == 409 and duplicate.json()["code"] == 409
    assert service._remediation_engine.execute_plan.await_count == 1


def test_http_expired_plan_returns_404_before_initialization(service, monkeypatch):
    clock = [0.0]
    service._approvals.clock = lambda: clock[0]
    service._approvals.ttl = 1
    plan_id = asyncio.run(create_plan(service))
    clock[0] = 2
    initialize = AsyncMock(side_effect=AssertionError("expired plan must not initialize"))
    monkeypatch.setattr(service, "initialize", initialize)
    with approval_http_client(service, monkeypatch) as client:
        response = client.post("/autofix/workflow/confirm", headers={"X-API-Key": KEY, "X-CloudOps-User-ID": "1"}, json={"plan_id": plan_id, "approved_action_ids": [ACTION]})
        assert response.status_code == 404 and response.json()["code"] == 404
    initialize.assert_not_awaited()
    assert service._approvals.records == {}


async def test_closed_service_cannot_be_reinitialized(service):
    await service.cleanup()
    with pytest.raises(AIOpsException) as closed:
        await service.initialize()
    assert closed.value.status_code == 503
    assert service._k8s_fixer is None and service._remediation_engine is None


async def test_original_allowed_actions_wait_for_explicit_selection_in_a_blocked_workflow(service):
    original = pending_result()
    allowed = deepcopy(original["plan"]["candidate_actions"][0])
    allowed["action_id"] = "original-allowed-action"
    allowed["risk_assessment"]["allowed"] = True
    allowed["risk_assessment"]["requires_human_confirmation"] = False
    original["plan"]["candidate_actions"].append(allowed)
    original["plan"]["allowed_actions"].append(allowed)
    service._ops_workflow.run.side_effect = lambda _: deepcopy(original)
    service._remediation_engine.build_plan.side_effect = lambda _: deepcopy(original["plan"])
    service._remediation_engine.execute_plan.side_effect = lambda plan: {"status": "completed", "executed_actions": [{"action_id": action["action_id"], "status": "succeeded"} for action in plan["allowed_actions"]]}
    with principal_scope("user:1"):
        result = await service.execute_multi_agent_workflow({"problem_description": "offline"})
        plan_id = result["plan"]["plan_id"]
        assert {action["action_id"] for action in result["blocked_actions"]} == {ACTION, "original-allowed-action"}
        first = await service.confirm_multi_agent_workflow(plan_id, [ACTION])
        assert first["status"] == "needs_human_confirmation"
        assert [action["action_id"] for action in first["executed_actions"]] == [ACTION]
        second = await service.confirm_multi_agent_workflow(plan_id, ["original-allowed-action"])
        assert second["status"] == "completed"
        assert [action["action_id"] for action in second["executed_actions"]] == [ACTION, "original-allowed-action"]


def test_nonce_collision_fails_closed_without_overwriting_existing_owner(monkeypatch):
    from app.services.workflow_approvals import WorkflowApprovals

    monkeypatch.setattr("app.services.workflow_approvals.uuid.uuid4", lambda: SimpleNamespace(hex="same-nonce"))
    store = WorkflowApprovals()
    with principal_scope("user:1"):
        first = store.reserve()
        store.publish(first, deepcopy(pending_result()))
    with principal_scope("user:2"), pytest.raises(AIOpsException) as denied:
        store.reserve()
    assert denied.value.status_code == 503
    with principal_scope("user:1"):
        assert store.get(first.plan_id).owner == "user:1"


@pytest.mark.parametrize("field", ["namespace", "deployment"])
async def test_revalidation_must_read_the_original_plan_target(service, field):
    plan_id = await create_plan(service)
    changed = pending_result()["plan"]
    changed[field] = "different-target"
    service._remediation_engine.build_plan.side_effect = None
    service._remediation_engine.build_plan.return_value = changed
    with principal_scope("user:1"), pytest.raises(AIOpsException) as rejected:
        await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert rejected.value.status_code == 409
    service._remediation_engine.execute_plan.assert_not_awaited()


async def test_missing_permission_probe_never_allows_automatic_execution():
    from app.core.autofix.remediation import AutoFixRemediationEngine

    fake = SimpleNamespace(get_deployment=AsyncMock(return_value={
        "metadata": {"name": "api", "namespace": "default"},
        "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "api", "image": "test/bad"}]}}},
    }), patch_deployment=AsyncMock(return_value=True))
    engine = AutoFixRemediationEngine(fake)
    result = await engine.plan_and_execute({"deployment": "api", "namespace": "default", "event": "ImagePullBackOff"})
    assert result["status"] == "blocked"
    fake.patch_deployment.assert_not_awaited()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_model_metadata_is_rejected_before_http_response_serialization(service, monkeypatch, value):
    result = pending_result()
    result["review"]["ai_review"] = {"confidence": value}
    service._ops_workflow.run.side_effect = lambda _: deepcopy(result)
    with approval_http_client(service, monkeypatch) as client:
        response = client.post("/autofix/workflow", headers={"X-API-Key": KEY, "X-CloudOps-User-ID": "1"}, json={"problem_description": "offline"})
        assert response.status_code == 409
        assert "可能" in response.json()["message"]
    record = next(iter(service._approvals.records.values()))
    assert record.state == "uncertain" and record.result is None
