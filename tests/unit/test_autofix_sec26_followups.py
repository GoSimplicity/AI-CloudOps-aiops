"""承接 SEC-26 第一层审查的后续项（s1/s4/s5/s6/s7、P3-3/P3-5/P3-6/P3-7、namespace_refusal 公开化，全部离线）"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException
import pytest
import urllib3.exceptions

from app.api.routes import autofix as autofix_routes
from app.common.exceptions import AIOpsException, AutoFixError, AutoFixInternalError, ServiceUnavailableError, ValidationError
from app.config import settings
from app.config.settings import AutoFixConfig, config
from app.core.autofix import RemediationEngine
from app.core.autofix.remediation import AutoFixRemediationEngine, api_error_summary
from tests.unit.test_autofix_sec26_auth_and_tightening import _plan_restart
from tests.unit.test_autofix_sec26_repair_bootstrap import REPAIR_BODY, make_app
from tests.unit.test_autofix_sec26_run_lockdown import assert_no_writes, make_service, mock_k8s

SECRET = "body-secret-token=sk-followup-leak-7c1e"
REPO = Path(__file__).resolve().parents[2]


def api_exception(status: int = 403, reason: str = "Forbidden") -> ApiException:
    exc = ApiException(status=status, reason=reason)
    exc.body = '{"message": "%s"}' % SECRET
    exc.headers = {"Authorization": SECRET}
    return exc


class LeakyError(RuntimeError):
    pass


# ---------------------------------------------------------------- s1：上游错误 502 / 其它 500


@pytest.mark.parametrize(
    "error,status,error_code",
    [
        (api_exception(500, "Internal Server Error"), 502, "AUTOFIX_ERROR"),
        (urllib3.exceptions.ReadTimeoutError(None, "/api", SECRET), 502, "AUTOFIX_ERROR"),
        (urllib3.exceptions.ConnectTimeoutError(SECRET), 502, "AUTOFIX_ERROR"),
        (LeakyError(SECRET), 500, "AUTOFIX_INTERNAL_ERROR"),
        (KeyError(SECRET), 500, "AUTOFIX_INTERNAL_ERROR"),
    ],
)
def test_s1_diagnose_maps_upstream_to_502_and_others_to_500(monkeypatch, error, status, error_code) -> None:
    monkeypatch.setattr(config, "debug", False)
    service = make_service(mock_k8s())
    monkeypatch.setattr(service, "initialize", AsyncMock())
    monkeypatch.setattr(service, "diagnose_kubernetes_issues", AsyncMock(side_effect=error))
    response = make_app(monkeypatch, service).post("/autofix/diagnose", json={"deployment": "api", "namespace": "default"})
    assert response.status_code == status
    assert response.json()["data"]["error_code"] == error_code
    assert SECRET not in response.text


@pytest.mark.parametrize("error,status", [(api_exception(503, "Service Unavailable"), 502), (LeakyError(SECRET), 500)])
def test_s1_repair_maps_upstream_to_502_and_others_to_500(monkeypatch, error, status) -> None:
    monkeypatch.setattr(config, "debug", False)
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    service = make_service(mock_k8s())
    monkeypatch.setattr(service, "fix_kubernetes_deployment", AsyncMock(side_effect=error))
    response = make_app(monkeypatch, service).post("/autofix/repair", json=REPAIR_BODY)
    assert response.status_code == status
    assert SECRET not in response.text


@pytest.mark.parametrize("error,status", [(api_exception(409, "Conflict"), 502), (LeakyError(SECRET), 500)])
def test_s1_bootstrap_maps_upstream_to_502_and_others_to_500(monkeypatch, error, status) -> None:
    monkeypatch.setattr(config, "debug", False)
    monkeypatch.setattr(config.autofix, "bootstrap_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["test"])
    service = make_service(mock_k8s())
    monkeypatch.setattr(service, "initialize", AsyncMock(side_effect=error))
    response = make_app(monkeypatch, service).post("/autofix/bootstrap")
    assert response.status_code == status
    assert SECRET not in response.text


def test_s1_upstream_or_internal_helper_types() -> None:
    assert type(autofix_routes._upstream_or_internal(api_exception(), "x")) is AutoFixError
    assert type(autofix_routes._upstream_or_internal(TimeoutError(), "x")) is AutoFixError
    internal = autofix_routes._upstream_or_internal(ValueError(SECRET), "固定文案")
    assert type(internal) is AutoFixInternalError
    assert internal.error_code == "AUTOFIX_INTERNAL_ERROR" and SECRET not in internal.message


# ---------------------------------------------------------------- s4：先 strip 再 lower，等于 true 才开启


@pytest.mark.parametrize(
    "yaml_value,expected,warns",
    [(" true ", True, False), ("TRUE\n", True, False), (" False", False, False), (1, False, True), ("1", False, True), (" on ", False, True)],
)
def test_s4_yaml_switch_strip_lower_and_int_one_is_off_with_warn(monkeypatch, caplog, yaml_value, expected, warns) -> None:
    monkeypatch.delenv("AUTOFIX_REPAIR_ENABLED", raising=False)
    monkeypatch.setattr(settings, "CONFIG", {"autofix": {"repair_enabled": yaml_value}})
    # 合并 SEC-26 P2-2 后开关从严格布尔 YAML 视图 AUTOFIX_SWITCH_CONFIG 读取
    monkeypatch.setattr(settings, "AUTOFIX_SWITCH_CONFIG", {"autofix": {"repair_enabled": yaml_value}})
    with caplog.at_level(logging.WARNING, logger="aiops.config.autofix"):
        cfg = AutoFixConfig()
    assert cfg.repair_enabled is expected
    warned = [r for r in caplog.records if r.name == "aiops.config.autofix" and "autofix.repair_enabled" in r.getMessage()]
    assert bool(warned) is warns


def test_s4_parser_rule_documented_in_release_notes() -> None:
    notes = (REPO / "docs/release/SEC-26-followups.md").read_text(encoding="utf-8")
    assert "先 strip，再 lower，等于 true 才开启" in notes
    assert "_parse_autofix_bool" in notes


# ---------------------------------------------------------------- s5：confirm 重读白名单；改白名单需重启


async def test_s5_confirm_rereads_whitelist_from_current_config(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    plan_id, action_id = await _plan_restart(service)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["staging"])
    with pytest.raises(AIOpsException) as denied:
        await service.confirm_multi_agent_workflow(plan_id, [action_id])
    assert denied.value.status_code == 403
    assert_no_writes(client)


def test_s5_docs_state_restart_required_and_pending_cleared() -> None:
    guide = (REPO / "docs/autofix_guide.md").read_text(encoding="utf-8")
    assert "改白名单要重启" in guide
    assert "重启会清掉待审批项" in guide


# ---------------------------------------------------------------- s6：固定文案 + error_code，状态码按异常类型映射


@pytest.mark.parametrize(
    "body",
    [
        {"deployment": "api", "event": "CrashLoopBackOff", "namespace": ""},
        {"deployment": "api", "event": "CrashLoopBackOff", "namespace": ["x", SECRET]},
    ],
)
def test_s6_autofix_base_invalid_request_is_400_fixed_text(monkeypatch, body) -> None:
    service = make_service(mock_k8s())
    response = make_app(monkeypatch, service).post("/autofix", json=body)
    assert response.status_code == 400
    payload = response.json()
    assert payload["data"]["error_code"] == "VALIDATION_ERROR"
    assert payload["data"]["detail"] == "字段 request 验证失败: 请求参数无效"
    assert SECRET not in response.text
    assert "pydantic" not in response.text.lower() and "min_length" not in response.text


@pytest.mark.parametrize(
    "error,status,error_code",
    [
        (ValidationError("deployment", "名称无效"), 400, "VALIDATION_ERROR"),
        (ServiceUnavailableError("kubernetes"), 503, "SERVICE_UNAVAILABLE"),
        (AutoFixError("诊断失败"), 502, "AUTOFIX_ERROR"),
    ],
)
def test_s6_diagnose_passes_domain_errors_through_with_mapped_status(monkeypatch, error, status, error_code) -> None:
    service = make_service(mock_k8s())
    monkeypatch.setattr(service, "initialize", AsyncMock())
    monkeypatch.setattr(service, "diagnose_kubernetes_issues", AsyncMock(side_effect=error))
    response = make_app(monkeypatch, service).post("/autofix/diagnose", json={"deployment": "api", "namespace": "default"})
    assert response.status_code == status
    assert response.json()["data"]["error_code"] == error_code


# ---------------------------------------------------------------- s7：remediation 日志只记类型/status/reason


def test_s7_api_error_summary_has_no_body_or_headers() -> None:
    summary = api_error_summary(api_exception(403, "Forbidden"))
    assert summary == {"type": "ApiException", "status": 403, "reason": "Forbidden"}
    assert api_error_summary(LeakyError(SECRET)) == {"type": "LeakyError", "status": None, "reason": None}
    long_reason = api_error_summary(api_exception(500, "x" * 500))
    assert len(long_reason["reason"]) == 64


async def test_s7_get_deployment_failure_logs_status_reason_not_body(caplog) -> None:
    client = mock_k8s()
    client.get_deployment.side_effect = api_exception(403, "Forbidden")
    engine = AutoFixRemediationEngine(client, allowed_namespaces=["default"])
    with caplog.at_level(logging.WARNING, logger="aiops.autofix.remediation"):
        assert await engine._get_deployment("api", "default") is None
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "aiops.autofix.remediation")
    assert "ApiException" in text and "status=403" in text and "reason=Forbidden" in text
    assert SECRET not in caplog.text


async def test_s7_permission_check_failure_logs_status_reason_not_body(caplog) -> None:
    client = mock_k8s()
    client.can_i.side_effect = api_exception(401, "Unauthorized")
    engine = AutoFixRemediationEngine(client, allowed_namespaces=["default"])
    action = {"action_id": "a1", "action_type": "restart_deployment", "target": {"namespace": "default", "deployment": "api"}}
    with caplog.at_level(logging.WARNING, logger="aiops.autofix.remediation"):
        assert await engine._permission_allowed(action, "default") is False
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "aiops.autofix.remediation")
    assert "status=401" in text and "reason=Unauthorized" in text and "a1" in text
    assert SECRET not in caplog.text


# ---------------------------------------------------------------- P3-3：日志只记异常类型，完整内容放 debug


def _messages(caplog, level: int) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


async def test_p3_3_health_check_logs_type_only_details_in_debug(monkeypatch, caplog) -> None:
    service = make_service(mock_k8s())
    service._k8s_fixer = MagicMock()
    service._supervisor = MagicMock()
    monkeypatch.setattr(service, "execute_with_timeout", AsyncMock(side_effect=LeakyError(SECRET)))
    with caplog.at_level(logging.DEBUG, logger=service.logger.name):
        assert await service._do_health_check() is False
    warnings = _messages(caplog, logging.WARNING)
    assert any("LeakyError" in m for m in warnings)
    assert all(SECRET not in m for m in warnings)
    assert any(SECRET in m for m in _messages(caplog, logging.DEBUG))


async def test_p3_3_cleanup_logs_type_only_details_in_debug(caplog) -> None:
    service = make_service(mock_k8s())
    service._k8s_fixer = MagicMock()
    service._k8s_fixer.cleanup = MagicMock(side_effect=LeakyError(SECRET))
    service._supervisor = MagicMock()
    service._supervisor.cleanup = MagicMock(side_effect=LeakyError(SECRET + "-sup"))
    with caplog.at_level(logging.DEBUG, logger=service.logger.name):
        await service.cleanup()
    warnings = _messages(caplog, logging.WARNING) + _messages(caplog, logging.ERROR)
    assert sum("LeakyError" in m for m in warnings) >= 2
    assert all(SECRET not in m for m in warnings)
    assert any(SECRET in m for m in _messages(caplog, logging.DEBUG))


async def test_p3_3_cleanup_outer_failure_logs_type_only(monkeypatch, caplog) -> None:
    service = make_service(mock_k8s())
    monkeypatch.setattr(service._approvals, "clear", MagicMock(side_effect=LeakyError(SECRET)))
    with caplog.at_level(logging.DEBUG, logger=service.logger.name), pytest.raises(LeakyError):
        await service.cleanup()
    errors = _messages(caplog, logging.ERROR)
    assert any("LeakyError" in m for m in errors)
    assert all(SECRET not in m for m in errors)
    assert any(SECRET in m for m in _messages(caplog, logging.DEBUG))


def test_p3_3_route_notification_failure_logs_type_only(monkeypatch, caplog) -> None:
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    service = make_service(mock_k8s())
    monkeypatch.setattr(service, "fix_kubernetes_deployment", AsyncMock(return_value={"success": True, "status": "planned"}))
    monkeypatch.setattr(autofix_routes, "get_notification_service", AsyncMock(side_effect=LeakyError(SECRET)))
    client = make_app(monkeypatch, service)
    with caplog.at_level(logging.DEBUG, logger="aiops.api.autofix"):
        response = client.post("/autofix/repair", json={**REPAIR_BODY, "auto_restart": True})
    assert response.status_code == 200
    warnings = [r.getMessage() for r in caplog.records if r.name == "aiops.api.autofix" and r.levelno == logging.WARNING]
    assert any("LeakyError" in m for m in warnings)
    assert all(SECRET not in m for m in warnings)
    assert any(SECRET in r.getMessage() for r in caplog.records if r.name == "aiops.api.autofix" and r.levelno == logging.DEBUG)


# ---------------------------------------------------------------- P3-5：就绪检查 503 不带 str(e)


def _ready_client(module, getter: str, failing: Any, monkeypatch) -> TestClient:
    from app.api.middleware.error_handler import setup_error_handlers

    monkeypatch.setattr(module, getter, AsyncMock(return_value=failing))
    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(module.router, prefix="/svc")
    return TestClient(app)


@pytest.mark.parametrize("module_name,getter", [("assistant", "get_assistant_service"), ("predict", "get_prediction_service"), ("rca", "get_rca_service")])
@pytest.mark.parametrize("debug", [False, True])
def test_p3_5_ready_503_has_no_exception_text(monkeypatch, caplog, module_name, getter, debug) -> None:
    import importlib

    module = importlib.import_module(f"app.api.routes.{module_name}")
    monkeypatch.setattr(config, "debug", debug)
    failing = MagicMock()
    failing.initialize = AsyncMock(side_effect=LeakyError(SECRET))
    with caplog.at_level(logging.DEBUG):
        response = _ready_client(module, getter, failing, monkeypatch).get("/svc/ready")
    assert response.status_code == 503
    assert response.json()["data"]["error_code"] == "SERVICE_UNAVAILABLE"
    assert SECRET not in response.text  # debug 下 details 也不再含 str(e)
    assert "details" not in response.json()["data"] or "error" not in (response.json()["data"].get("details") or {})
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert all(SECRET not in m for m in errors)


# ---------------------------------------------------------------- P3-6：resource_exists=false → 409


async def test_p3_6_resource_gone_at_confirm_is_409_and_not_executed(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    plan_id, action_id = await _plan_restart(service)
    client.get_deployment.return_value = None
    with pytest.raises(AIOpsException) as denied:
        await service.confirm_multi_agent_workflow(plan_id, [action_id])
    assert denied.value.status_code == 409
    assert denied.value.error_code == "WORKFLOW_APPROVAL_ERROR"
    assert "不存在" in denied.value.message
    assert_no_writes(client)


def test_p3_6_error_contract_table_lists_409_resource_gone() -> None:
    guide = (REPO / "docs/autofix_guide.md").read_text(encoding="utf-8")
    contract = guide.split("#### 错误响应契约", 1)[1].split("\n## ", 1)[0]
    assert "resource_exists=false" in contract and "409" in contract


# ---------------------------------------------------------------- P3-7：局部策略，不改共享单例


def test_p3_7_with_policy_returns_local_copy_and_keeps_shared_untouched() -> None:
    shared = AutoFixRemediationEngine(MagicMock(), allowed_namespaces=["default"], auto_execute=False)
    shared_allowed, shared_restricted = shared.allowed_namespaces, shared.restricted_namespaces
    policy = AutoFixRemediationEngine(None, allowed_namespaces=["default", "team-a"], restricted_namespaces=["locked"], auto_execute=True)
    local = shared.with_policy(policy)
    assert local is not shared and local.k8s_client is shared.k8s_client
    assert local.allowed_namespaces == {"default", "team-a"}
    assert local.auto_execute is True
    assert "locked" in local.restricted_namespaces and shared_restricted <= local.restricted_namespaces
    assert shared.allowed_namespaces is shared_allowed and shared.allowed_namespaces == {"default"}
    assert shared.restricted_namespaces is shared_restricted and "locked" not in shared.restricted_namespaces
    assert shared.auto_execute is False


async def test_p3_7_confirm_builds_policy_locally_without_mutating_singleton(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    shared = service._remediation_engine
    before = (set(shared.allowed_namespaces), shared.auto_execute, set(shared.restricted_namespaces))
    plan_id, action_id = await _plan_restart(service)

    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default", "team-b"])
    monkeypatch.setattr(config.autofix, "auto_execute_enabled", True)
    locals_built: List[AutoFixRemediationEngine] = []
    original = shared.with_policy

    def spy(policy):
        local = original(policy)
        locals_built.append(local)
        return local

    monkeypatch.setattr(shared, "with_policy", spy)
    result = await service.confirm_multi_agent_workflow(plan_id, [action_id])

    assert result is not None
    assert client.restart_deployment.await_count == 1
    assert service._remediation_engine is shared
    assert (set(shared.allowed_namespaces), shared.auto_execute, set(shared.restricted_namespaces)) == before
    assert len(locals_built) == 1
    assert locals_built[0].allowed_namespaces == {"default", "team-b"}
    assert locals_built[0].auto_execute is True
    assert locals_built[0].restricted_namespaces >= before[2]


# ---------------------------------------------------------------- namespace_refusal 公开化 + 所有引擎都预检


def test_namespace_refusal_is_public_and_protocol_declares_it() -> None:
    engine = AutoFixRemediationEngine(MagicMock(), allowed_namespaces=["default"])
    assert isinstance(engine, RemediationEngine)
    assert engine.namespace_refusal("default", []) is None
    assert engine.namespace_refusal("kube-system", []) is not None
    assert engine._namespace_refusal("other", []) == engine.namespace_refusal("other", [])
    assert "namespace_refusal" in RemediationEngine.__dict__


class DelegatingEngine:
    """不是 AutoFixRemediationEngine 子类的引擎：build_plan 委托真实引擎，namespace_refusal 自行决定"""

    def __init__(self, inner: AutoFixRemediationEngine, refusal: Optional[str]) -> None:
        self.inner = inner
        self.refusal = refusal
        self.refusal_calls: List[Any] = []
        self.execute_plan = AsyncMock(side_effect=inner.execute_plan)

    async def build_plan(self, diagnosis: Dict[str, Any]) -> Dict[str, Any]:
        return await self.inner.build_plan(diagnosis)

    def namespace_refusal(self, namespace: str, actions: List[Dict[str, Any]]) -> Optional[str]:
        self.refusal_calls.append((namespace, [a.get("action_id") for a in actions]))
        return self.refusal


async def test_precheck_runs_for_engine_that_is_not_autofix_remediation_engine(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    plan_id, action_id = await _plan_restart(service)
    fake = DelegatingEngine(service._remediation_engine, refusal="custom engine refused")
    assert not isinstance(fake, AutoFixRemediationEngine) and isinstance(fake, RemediationEngine)
    service._remediation_engine = fake

    with pytest.raises(AIOpsException) as denied:
        await service.confirm_multi_agent_workflow(plan_id, [action_id])

    assert denied.value.status_code == 403
    assert fake.refusal_calls and fake.refusal_calls[0][0] == "default"
    assert action_id in fake.refusal_calls[0][1]
    fake.execute_plan.assert_not_awaited()
    assert service._approvals.records[plan_id].state == "invalidated"
    assert_no_writes(client)


async def test_precheck_allows_non_autofix_engine_when_it_accepts(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    service = make_service(mock_k8s())
    plan_id, action_id = await _plan_restart(service)
    fake = DelegatingEngine(service._remediation_engine, refusal=None)
    service._remediation_engine = fake
    await service.confirm_multi_agent_workflow(plan_id, [action_id])
    assert fake.refusal_calls
    fake.execute_plan.assert_awaited_once()


async def test_precheck_fails_closed_when_engine_lacks_namespace_refusal(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    client = mock_k8s()
    service = make_service(client)
    plan_id, action_id = await _plan_restart(service)
    inner = service._remediation_engine

    class LegacyEngine:
        execute_plan = AsyncMock(return_value={"status": "completed", "executed_actions": []})

        async def build_plan(self, diagnosis):
            return await inner.build_plan(diagnosis)

    legacy = LegacyEngine()
    service._remediation_engine = legacy
    with pytest.raises(AIOpsException) as denied:
        await service.confirm_multi_agent_workflow(plan_id, [action_id])
    assert denied.value.status_code == 403
    legacy.execute_plan.assert_not_awaited()
    assert service._approvals.records[plan_id].state == "invalidated"
