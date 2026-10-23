"""SEC-26 第一层审查附加条件 P3-1/P3-2（全部离线）

P3-1：确认执行被命名空间白名单拦下时返回 403，审批记录进入 invalidated（不是 uncertain），
      不调用 K8s 写接口，重放不返回“可能已产生副作用”的 409；正常进入执行后的异常仍为 uncertain。
P3-2：经 api_response 抛出的 5xx 领域异常，非 debug 模式下响应不携带 data.details。
"""

from typing import Any, Dict

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.decorators import api_response
from app.api.middleware.error_handler import setup_error_handlers
from app.common.exceptions import AIOpsException, ServiceUnavailableError
from app.common.identity import principal_scope
from app.config.settings import config
from tests.unit.test_autofix_sec26_auth_and_tightening import _plan_restart
from tests.unit.test_autofix_sec26_run_lockdown import assert_no_writes, make_service, mock_k8s

SIDE_EFFECT_HINT = "可能已产生副作用"

@pytest.fixture(autouse=True)
def confirming_user():
    """SEC-44 修正：确认路径需要可信用户主体；本模块的确认流程以 user:1 执行"""
    with principal_scope("user:1"):
        yield



async def _confirm_denied(service, plan_id: str, action_id: str) -> AIOpsException:
    with pytest.raises(AIOpsException) as denied:
        await service.confirm_multi_agent_workflow(plan_id, [action_id])
    return denied.value


async def _assert_replay_not_side_effect_409(service, plan_id: str, action_id: str) -> None:
    replay = await _confirm_denied(service, plan_id, action_id)
    assert replay.status_code == 409
    assert SIDE_EFFECT_HINT not in replay.message
    assert "尚未执行" in replay.message


async def test_p3_1_namespace_precheck_denies_before_effects_started(monkeypatch) -> None:
    """重新校验通过后白名单被收紧：执行前预检拒绝，execute_plan 不被调用，记录 invalidated"""
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    plan_id, action_id = await _plan_restart(service)
    engine = service._remediation_engine
    original_build_plan = engine.build_plan
    execute_calls = []
    original_execute_plan = engine.execute_plan

    async def build_then_tighten(diagnosis):
        plan = await original_build_plan(diagnosis)
        engine.allowed_namespaces = set()
        return plan

    async def spy_execute_plan(plan, *args, **kwargs):
        execute_calls.append(plan)
        return await original_execute_plan(plan, *args, **kwargs)

    monkeypatch.setattr(engine, "build_plan", build_then_tighten)
    monkeypatch.setattr(engine, "execute_plan", spy_execute_plan)

    denied = await _confirm_denied(service, plan_id, action_id)

    assert denied.status_code == 403
    assert "命名空间" in denied.message
    record = service._approvals.records[plan_id]
    assert record.state == "invalidated"
    assert record.effects_started is False
    assert SIDE_EFFECT_HINT not in record.reason
    assert execute_calls == []
    assert_no_writes(client)
    await _assert_replay_not_side_effect_409(service, plan_id, action_id)
    assert_no_writes(client)


async def test_p3_1_executor_whitelist_block_rolls_back_to_invalidated(monkeypatch) -> None:
    """预检之后、执行器内部白名单拦截（未执行任何动作）：回退 effects_started，记录 invalidated"""
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    plan_id, action_id = await _plan_restart(service)
    engine = service._remediation_engine
    original_execute_plan = engine.execute_plan
    seen_effects_started = []

    async def tighten_then_execute(plan, *args, **kwargs):
        seen_effects_started.append(service._approvals.records[plan_id].effects_started)
        engine.allowed_namespaces = set()
        return await original_execute_plan(plan, *args, **kwargs)

    monkeypatch.setattr(engine, "execute_plan", tighten_then_execute)

    denied = await _confirm_denied(service, plan_id, action_id)

    assert seen_effects_started == [True]  # 确实已越过预检进入执行器
    assert denied.status_code == 403
    record = service._approvals.records[plan_id]
    assert record.state == "invalidated"
    assert record.effects_started is False
    assert_no_writes(client)
    await _assert_replay_not_side_effect_409(service, plan_id, action_id)


async def test_p3_1_blocked_without_proof_of_no_effects_stays_uncertain(monkeypatch) -> None:
    """对照：执行器返回 blocked 但无法确认选中动作均未尝试时，不回退，保持 uncertain"""
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    plan_id, action_id = await _plan_restart(service)

    async def opaque_blocked(plan, *args, **kwargs) -> Dict[str, Any]:
        return {"status": "blocked", "executed_actions": [], "post_check": None}

    monkeypatch.setattr(service._remediation_engine, "execute_plan", opaque_blocked)

    denied = await _confirm_denied(service, plan_id, action_id)

    assert denied.status_code == 403
    record = service._approvals.records[plan_id]
    assert record.state == "uncertain"
    assert record.effects_started is True


async def test_p3_1_exception_after_execution_started_stays_uncertain(monkeypatch) -> None:
    """对照：正常进入执行并已调用 K8s 写接口后抛异常，仍为 uncertain，不能回退"""
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    plan_id, action_id = await _plan_restart(service)
    engine = service._remediation_engine
    original_execute_plan = engine.execute_plan

    async def execute_then_crash(plan, *args, **kwargs):
        await original_execute_plan(plan, *args, **kwargs)
        raise RuntimeError("post-execution failure")

    monkeypatch.setattr(engine, "execute_plan", execute_then_crash)

    denied = await _confirm_denied(service, plan_id, action_id)

    assert client.restart_deployment.await_count == 1
    assert denied.status_code == 409
    assert SIDE_EFFECT_HINT in denied.message
    record = service._approvals.records[plan_id]
    assert record.state == "uncertain"
    assert record.effects_started is True

    replay = await _confirm_denied(service, plan_id, action_id)
    assert replay.status_code == 409
    assert SIDE_EFFECT_HINT in replay.message
    assert client.restart_deployment.await_count == 1


async def test_p3_1_failed_write_stays_uncertain(monkeypatch) -> None:
    """对照：K8s 写接口失败（执行器返回 failed）仍为 uncertain"""
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    client.restart_deployment.side_effect = RuntimeError("apiserver error")
    service = make_service(client)
    plan_id, action_id = await _plan_restart(service)

    denied = await _confirm_denied(service, plan_id, action_id)

    assert client.restart_deployment.await_count == 1
    assert denied.status_code == 409
    assert service._approvals.records[plan_id].state == "uncertain"


PRIVATE_DETAIL = "upstream-private-detail-7f3a"


def _service_unavailable_client() -> TestClient:
    app = FastAPI()
    setup_error_handlers(app)

    @app.get("/probe/unavailable")
    @api_response("probe")
    async def unavailable():
        raise ServiceUnavailableError("autofix", details={"error": PRIVATE_DETAIL})

    return TestClient(app)


def test_p3_2_5xx_details_hidden_when_not_debug(monkeypatch) -> None:
    monkeypatch.setattr(config, "debug", False)

    response = _service_unavailable_client().get("/probe/unavailable")

    assert response.status_code == 503
    payload = response.json()
    assert payload["data"]["error_code"] == "SERVICE_UNAVAILABLE"
    assert "details" not in payload["data"]
    assert PRIVATE_DETAIL not in response.text


def test_p3_2_5xx_details_returned_in_debug_mode(monkeypatch) -> None:
    """对照：按现有设计 debug 模式保留 data.details 便于排障"""
    monkeypatch.setattr(config, "debug", True)

    response = _service_unavailable_client().get("/probe/unavailable")

    assert response.status_code == 503
    payload = response.json()
    assert payload["data"]["error_code"] == "SERVICE_UNAVAILABLE"
    assert payload["data"]["details"] == {"error": PRIVATE_DETAIL}
