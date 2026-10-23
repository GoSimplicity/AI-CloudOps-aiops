"""SEC-44：autofix 自动执行加固（G1 实时证据 / G2 写入口门禁 / G4 结构化审计 / G6 低风险白名单）

真实链路：HTTP 路由 → AutoFixService → OpsMultiAgentWorkflow → AutoFixRemediationEngine → KubernetesService；
只 mock 最底层 Kubernetes SDK（apps_v1 / core_v1 / AuthorizationV1Api），全部离线、只用假值。
"""

from __future__ import annotations

from copy import deepcopy
import time
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

from kubernetes.client.rest import ApiException
import pytest

from app.common.exceptions import AIOpsException
from app.config.settings import config
from app.core.autofix import audit as audit_module, remediation
from app.core.autofix.remediation import (
    ACTION_FAULT_TYPES,
    ACTION_RISK_GRADES,
    AUTO_EXECUTE_ACTION_TYPES,
    AutoFixRemediationEngine,
    auto_execution_refusal,
)
from app.services import kubernetes as k8s_module
from app.services.autofix_service import AutoFixService
from app.services.kubernetes import KubernetesService
from tests.unit.test_autofix_sec26_repair_bootstrap import make_app

NS = "default"
CRASHLOOP = "CrashLoopBackOff restarting failed container"
OOM = "OOMKilled insufficient memory"
IMAGE_PULL = "ImagePullBackOff failed to pull image"
WRITE_PREFIXES = ("patch_", "create_", "replace_", "delete_")
SECRET_MARKER = "sec44-fake-exception-body-7c1e"
POD_HASH = "5f7c"
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
AUDIT_KEYS = {"event", "schema_version", "audit_id", "phase", "time", "principal", "target", "action", "auto_executed", "result", "error_type", "reason"}


# ---------------------------------------------------------------- 底层 SDK 替身（只用假值）


def live_deployment(replicas: int = 1) -> Dict[str, Any]:
    return {
        "metadata": {"name": "api", "namespace": NS, "generation": 1, "annotations": {REVISION_ANNOTATION: "3"}},
        "spec": {
            "replicas": replicas,
            "selector": {"match_labels": {"app": "api"}},
            "template": {"spec": {"containers": [{"name": "api", "image": "repo/api:fake", "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}}}]}},
        },
        "status": {"replicas": replicas, "updated_replicas": replicas, "ready_replicas": replicas, "available_replicas": replicas, "observed_generation": 1},
    }


def pod(waiting: str | None = None, restarts: int = 0, ready: bool = True, terminated: str | None = None) -> Dict[str, Any]:
    state = {"waiting": {"reason": waiting}} if waiting else {"running": {"started_at": "fake"}}
    status: Dict[str, Any] = {
        "phase": "Running",
        "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        "container_statuses": [{"name": "api", "restart_count": restarts, "ready": ready, "state": state}],
    }
    if terminated:
        status["container_statuses"][0]["last_state"] = {"terminated": {"reason": terminated}}
    return {"metadata": pod_metadata(), "status": status}


def pod_metadata(rs_name: str = "api-" + POD_HASH, pod_hash: str = POD_HASH, controller: bool = True, kind: str = "ReplicaSet") -> Dict[str, Any]:
    """SEC-44 修正 P2-2：Pod 由目标 Deployment 当前 ReplicaSet（api-<pod-template-hash>）控制"""
    return {
        "name": f"{rs_name}-x1",
        "namespace": NS,
        "labels": {"app": "api", "pod-template-hash": pod_hash},
        "owner_references": [{"api_version": "apps/v1", "kind": kind, "name": rs_name, "controller": controller}],
    }


def replica_set(name: str = "api-" + POD_HASH, owner: str | None = "api", revision: str = "3", owner_kind: str = "Deployment") -> Dict[str, Any]:
    owners = [{"api_version": "apps/v1", "kind": owner_kind, "name": owner, "controller": True}] if owner else []
    return {"metadata": {"name": name, "namespace": NS, "annotations": {REVISION_ANNOTATION: revision}, "owner_references": owners}}


HEALTHY = [pod()]
CRASHING = [pod(waiting="CrashLoopBackOff", restarts=5, ready=False)]
OOM_KILLED = [pod(restarts=1, ready=False, terminated="OOMKilled")]
PULL_FAILING = [pod(waiting="ImagePullBackOff", ready=False)]


def sdk_objects(items: List[Dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(items=[SimpleNamespace(to_dict=(lambda item=item: deepcopy(item))) for item in items])


class FakeSDK:
    """KubernetesService 下面的 SDK：读返回假对象，写只计数"""

    def __init__(self, pods: List[Dict[str, Any]] | None = None, deployment: Dict[str, Any] | None = None) -> None:
        self.apps_v1 = MagicMock(name="AppsV1Api")
        self.core_v1 = MagicMock(name="CoreV1Api")
        dep = deployment if deployment is not None else live_deployment()
        self.apps_v1.read_namespaced_deployment.side_effect = lambda **_: SimpleNamespace(to_dict=lambda: deepcopy(dep))
        self.pod_reads = 0
        self.pods_effect = None
        self.pods = pods if pods is not None else HEALTHY

        def list_pods(**_kwargs):
            self.pod_reads += 1
            if self.pods_effect is not None:
                return self.pods_effect()
            return sdk_objects(self.pods)

        self.core_v1.list_namespaced_pod.side_effect = list_pods
        self.replica_sets: Dict[str, Dict[str, Any]] = {"api-" + POD_HASH: replica_set()}
        self.rs_reads = 0

        def read_rs(name, namespace, **_kwargs):
            self.rs_reads += 1
            if name not in self.replica_sets:
                raise ApiException(status=404, reason="Not Found")
            return SimpleNamespace(to_dict=lambda: deepcopy(self.replica_sets[name]))

        self.apps_v1.read_namespaced_replica_set.side_effect = read_rs

    def writes(self) -> List[str]:
        calls = []
        for api in (self.apps_v1, self.core_v1):
            for name, _args, _kwargs in api.mock_calls:
                if name.split(".")[0].startswith(WRITE_PREFIXES):
                    calls.append(name)
        return calls


def make_k8s_service(sdk: FakeSDK) -> KubernetesService:
    service = object.__new__(KubernetesService)
    service.apps_v1 = sdk.apps_v1
    service.core_v1 = sdk.core_v1
    service.initialized = True
    service.last_init_attempt = time.time()
    service._init_retry_interval = 60
    return service


class ListSink:
    def __init__(self, fail_phases: tuple = ()) -> None:
        self.records: List[Dict[str, Any]] = []
        self.fail_phases = fail_phases

    def write(self, record: Dict[str, Any]) -> None:
        if record["phase"] in self.fail_phases:
            raise OSError(f"audit backend down {SECRET_MARKER}")
        self.records.append(deepcopy(record))


@pytest.fixture(autouse=True)
def offline_rbac(monkeypatch):
    """can_i 底层 SelfSubjectAccessReview：假值，一律允许"""
    api = MagicMock(name="AuthorizationV1Api")
    api.create_self_subject_access_review.return_value = SimpleNamespace(status=SimpleNamespace(allowed=True))
    monkeypatch.setattr(k8s_module.client, "AuthorizationV1Api", lambda: api)


@pytest.fixture
def sink():
    collected = ListSink()
    previous = audit_module.set_audit_sink(collected)
    try:
        yield collected
    finally:
        audit_module.set_audit_sink(previous)


@pytest.fixture
def switches(monkeypatch):
    def _set(auto_execute: bool, namespaces=(NS,)) -> None:
        monkeypatch.setattr(config.autofix, "auto_execute_enabled", auto_execute)
        monkeypatch.setattr(config.autofix, "repair_enabled", False)  # 默认值：确认流程不依赖 repair_enabled
        monkeypatch.setattr(config.autofix, "allowed_namespaces", list(namespaces))

    return _set


@pytest.fixture
def human():
    """确认路径需要可信用户主体（SEC-44 修正）；引擎级 confirm 测试以 user:1 身份执行"""
    from app.common.identity import principal_scope

    with principal_scope("user:1"):
        yield


def make_service(sdk: FakeSDK) -> AutoFixService:
    service = AutoFixService()
    service._initialized = True
    service._remediation_engine = service._build_remediation_engine(make_k8s_service(sdk))
    return service


def run_workflow(client, event: str, **extra) -> Dict[str, Any]:
    body = {"problem_description": f"deployment api {event}", "deployment": "api", "namespace": NS, "event": event, **extra}
    response = client.post("/autofix/workflow", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def pending_action(data: Dict[str, Any], action_type: str) -> Dict[str, Any]:
    return next(action for action in data["blocked_actions"] if action["action_type"] == action_type)


# ---------------------------------------------------------------- G6：动作分级与白名单


def test_action_grades_and_whitelist_are_explicit() -> None:
    assert ACTION_RISK_GRADES == {
        "restart_deployment": "low",
        "patch_image_pull_policy": "low",
        "scale_deployment": "medium",
        "adjust_resource_requests": "medium",
    }
    assert AUTO_EXECUTE_ACTION_TYPES == frozenset({"restart_deployment"})  # SEC-44 修正：image 仍为 low，但一律确认
    assert all(ACTION_RISK_GRADES[action] == "low" for action in AUTO_EXECUTE_ACTION_TYPES)
    assert set(ACTION_FAULT_TYPES) == set(ACTION_RISK_GRADES)


@pytest.mark.parametrize("action_type", ["patch_image_pull_policy", "scale_deployment", "adjust_resource_requests", "unknown_write"])
def test_non_whitelisted_action_is_refused_even_with_low_risk_and_matching_evidence(action_type) -> None:
    evidence = {"source": "live", "fault_type": ACTION_FAULT_TYPES.get(action_type, "unknown"), "matched": True}
    action = {"action_type": action_type, "risk_assessment": {"risk_level": "low", "live_evidence": evidence}}
    assert auto_execution_refusal(action) == "action not in auto-execute whitelist"


def test_whitelisted_action_requires_live_matching_evidence() -> None:
    good = {"source": "live", "fault_type": "crash_loop_backoff", "matched": True}
    base = {"action_type": "restart_deployment", "risk_assessment": {"risk_level": "low", "live_evidence": good}}
    assert auto_execution_refusal(base) is None
    for evidence in (
        {},
        {**good, "source": "diagnosis"},
        {**good, "matched": False},
        {**good, "matched": "true"},
        {**good, "fault_type": "image_pull_failure"},
    ):
        action = deepcopy(base)
        action["risk_assessment"]["live_evidence"] = evidence
        assert auto_execution_refusal(action) == "live evidence missing or mismatched"
    medium = deepcopy(base)
    medium["risk_assessment"]["risk_level"] = "medium"
    assert auto_execution_refusal(medium) == "risk level not low"


# ---------------------------------------------------------------- G1：真实链路，证据不足一律 pending


@pytest.mark.parametrize(
    "extra",
    [
        {},
        # 伪造 diagnosis：请求里声称 Pod 处于 CrashLoopBackOff、重启很多次
        {
            "diagnosis": {
                "deployment": "api",
                "namespace": NS,
                "event": CRASHLOOP,
                "pods": [{"status": {"container_statuses": [{"restart_count": 99, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}],
                "events": [{"reason": "BackOff", "message": CRASHLOOP}],
            }
        },
    ],
    ids=["text-only", "forged-diagnosis"],
)
def test_diagnosis_with_healthy_live_pods_stays_pending(monkeypatch, switches, sink, extra) -> None:
    switches(auto_execute=True)
    sdk = FakeSDK(pods=HEALTHY)
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP, **extra)

    assert data["status"] == "needs_human_confirmation"
    assert data["executed_actions"] == []
    assert "Executor" not in data["agents_used"]
    restart = pending_action(data, "restart_deployment")
    assert restart["risk_assessment"]["live_evidence"]["matched"] is False
    assert "live evidence missing or mismatched" in restart["risk_assessment"]["reasons"]
    assert data["approval"]["state"] == "pending"
    assert sdk.pod_reads == 1
    assert sdk.writes() == []
    assert sink.records == []


def test_target_missing_in_live_cluster_stays_pending(monkeypatch, switches, sink) -> None:
    """目标只来自请求文字（集群里读不到 Deployment）→ 不读 Pod、不自动执行"""
    switches(auto_execute=True)
    sdk = FakeSDK(pods=CRASHING)
    sdk.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404, reason="Not Found")
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == "needs_human_confirmation"
    assert data["executed_actions"] == []
    assert sdk.pod_reads == 0
    assert sdk.writes() == [] and sink.records == []


@pytest.mark.parametrize("failure", ["api-error", "runtime-error", "timeout", "no-pods", "no-selector"])
def test_live_evidence_read_failure_or_timeout_stays_pending(monkeypatch, switches, sink, failure) -> None:
    switches(auto_execute=True)
    deployment = live_deployment()
    if failure == "no-selector":
        deployment["spec"].pop("selector")
    sdk = FakeSDK(pods=CRASHING, deployment=deployment)
    if failure == "api-error":
        sdk.pods_effect = lambda: (_ for _ in ()).throw(ApiException(status=500, reason=SECRET_MARKER))
    elif failure == "runtime-error":
        sdk.pods_effect = lambda: (_ for _ in ()).throw(RuntimeError(SECRET_MARKER))
    elif failure == "timeout":
        monkeypatch.setattr(remediation, "EVIDENCE_TIMEOUT_SECONDS", 0.05)

        def slow():
            time.sleep(0.4)
            return sdk_objects(CRASHING)

        sdk.pods_effect = slow
    elif failure == "no-pods":
        sdk.pods = []
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == "needs_human_confirmation"
    assert data["executed_actions"] == []
    restart = pending_action(data, "restart_deployment")
    evidence = restart["risk_assessment"]["live_evidence"]
    assert evidence["matched"] is False and evidence["reason"]
    assert SECRET_MARKER not in str(data)
    assert sdk.writes() == [] and sink.records == []


def test_matching_live_evidence_auto_executes_restart_with_two_audit_records(monkeypatch, switches, sink) -> None:
    switches(auto_execute=True)
    sdk = FakeSDK(pods=CRASHING)
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == "completed", data
    assert [action["action_type"] for action in data["executed_actions"]] == ["restart_deployment"]
    assert sdk.writes() == ["patch_namespaced_deployment"]
    evidence = data["executed_actions"][0]["risk_assessment"]["live_evidence"]
    assert evidence["source"] == "live" and evidence["matched"] is True
    assert "crash_loop_backoff" in evidence["signals"]

    assert [record["phase"] for record in sink.records] == ["intent", "result"]
    intent, result = sink.records
    assert intent["audit_id"] == result["audit_id"]
    for record in sink.records:
        assert set(record) == AUDIT_KEYS
        assert record["event"] == "autofix.mutation"
        assert record["principal"] == "user:1"
        assert record["target"] == {"namespace": NS, "kind": "Deployment", "name": "api"}
        assert record["action"] == {"action_id": "restart-deployment", "action_type": "restart_deployment"}
        assert record["auto_executed"] is True
        assert record["time"].endswith("+00:00")
    assert intent["result"] == "pending"
    assert result["result"] == "succeeded" and result["error_type"] is None


def test_image_pull_evidence_is_recorded_but_still_requires_confirmation(monkeypatch, switches, sink) -> None:
    """SEC-44 修正：patch_image_pull_policy 风险等级仍为 low，但不在自动执行白名单；开启 auto 时照样读证据，只记录不放行"""
    switches(auto_execute=True)
    sdk = FakeSDK(pods=PULL_FAILING)
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, IMAGE_PULL)

    assert data["status"] == "needs_human_confirmation"
    assert data["executed_actions"] == []
    patch = pending_action(data, "patch_image_pull_policy")
    risk = patch["risk_assessment"]
    assert risk["risk_level"] == "low"
    assert risk["live_evidence"]["source"] == "live" and risk["live_evidence"]["matched"] is True
    assert "image_pull_backoff" in risk["live_evidence"]["signals"]
    assert "action not in auto-execute whitelist" in risk["reasons"]
    assert sdk.pod_reads == 1
    assert sdk.writes() == [] and sink.records == []

    confirmed = client.post("/autofix/workflow/confirm", json={"plan_id": data["plan"]["plan_id"], "approved_action_ids": [patch["action_id"]]})
    assert confirmed.status_code == 200, confirmed.text
    assert sdk.writes() == ["patch_namespaced_deployment"]
    assert [(r["phase"], r["auto_executed"]) for r in sink.records] == [("intent", False), ("result", False)]
    # 审计不含 patch 内容
    assert "IfNotPresent" not in str(sink.records) and "imagePullPolicy" not in str(sink.records)


@pytest.mark.parametrize(
    ("pods", "expected"),
    [
        ([pod(restarts=1, ready=False)], "needs_human_confirmation"),
        ([pod(restarts=2, ready=False)], "needs_human_confirmation"),
        # P2-1：已恢复 Ready 的 Pod 即使 restart_count>=3 也不算 CrashLoop 证据（TL 实测会被自动重启）
        ([pod(restarts=3, ready=True)], "needs_human_confirmation"),
        ([pod(restarts=50, ready=True)], "needs_human_confirmation"),
        ([pod(restarts=3, ready=False)], "completed"),
        ([pod(waiting="CrashLoopBackOff", restarts=0, ready=False)], "completed"),
    ],
    ids=["not-ready-restart-1", "not-ready-restart-2", "ready-restart-3", "ready-restart-50", "not-ready-restart-3", "waiting-crashloop"],
)
def test_crashloop_evidence_only_waiting_reason_or_three_restarts(monkeypatch, switches, sink, pods, expected) -> None:
    """SEC-44 修正：CrashLoop 证据只认 waiting=CrashLoopBackOff，或 restart_count>=3 且容器当前未 Ready"""
    switches(auto_execute=True)
    sdk = FakeSDK(pods=pods)
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == expected, data
    if expected == "completed":
        assert sdk.writes() == ["patch_namespaced_deployment"]
    else:
        assert sdk.writes() == [] and sink.records == []
        assert "not_ready_after_restart" not in pending_action(data, "restart_deployment")["risk_assessment"]["live_evidence"]["signals"]


def test_mismatched_live_evidence_does_not_authorize_other_fault(monkeypatch, switches, sink) -> None:
    """实时信号存在但属于另一种故障（镜像拉取失败 ≠ CrashLoop）→ 仍需确认"""
    switches(auto_execute=True)
    sdk = FakeSDK(pods=PULL_FAILING)
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == "needs_human_confirmation"
    assert sdk.writes() == [] and sink.records == []


# ---------------------------------------------------------------- G6：scale / requests 永远走确认


def test_scale_with_matching_evidence_stays_pending_then_confirm_executes(monkeypatch, switches, sink) -> None:
    switches(auto_execute=True)
    sdk = FakeSDK(pods=OOM_KILLED)
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, OOM)

    assert data["status"] == "needs_human_confirmation"
    assert data["executed_actions"] == []
    scale = pending_action(data, "scale_deployment")
    assert scale["risk_assessment"]["live_evidence"]["matched"] is True  # 证据匹配也不放行
    assert scale["risk_assessment"]["risk_level"] == "medium"
    assert "action not in auto-execute whitelist" in scale["risk_assessment"]["reasons"]
    requests_action = pending_action(data, "adjust_resource_requests")
    assert "action not in auto-execute whitelist" in requests_action["risk_assessment"]["reasons"]
    assert sdk.writes() == [] and sink.records == []

    confirmed = client.post("/autofix/workflow/confirm", json={"plan_id": data["plan"]["plan_id"], "approved_action_ids": [scale["action_id"]]})
    assert confirmed.status_code == 200, confirmed.text
    assert sdk.writes() == ["patch_namespaced_deployment"]
    assert [record["phase"] for record in sink.records] == ["intent", "result"]
    assert all(record["auto_executed"] is False for record in sink.records)
    assert all(record["action"]["action_type"] == "scale_deployment" for record in sink.records)
    assert '"replicas"' not in str(sink.records) and "replicas" not in str(sink.records)


def test_default_config_confirm_flow_still_executes(monkeypatch, switches, sink) -> None:
    """默认开关（auto_execute=False、repair_enabled=False）下确认流程照常：门禁不要求 repair_enabled"""
    switches(auto_execute=False)
    sdk = FakeSDK(pods=HEALTHY)
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)
    assert data["status"] == "needs_human_confirmation"
    assert sdk.pod_reads == 0  # 默认配置不增加集群读取
    restart = pending_action(data, "restart_deployment")

    confirmed = client.post("/autofix/workflow/confirm", json={"plan_id": data["plan"]["plan_id"], "approved_action_ids": [restart["action_id"]]})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["status"] == "completed"
    assert sdk.writes() == ["patch_namespaced_deployment"]
    assert [(r["phase"], r["auto_executed"], r["result"]) for r in sink.records] == [("intent", False, "pending"), ("result", False, "succeeded")]


# ---------------------------------------------------------------- G2：写入口门禁


def test_auto_path_gate_refuses_when_switch_revoked_before_write(monkeypatch, switches, sink) -> None:
    """计划阶段放行后、写入前开关被关闭（配置热更新）：写入口按当前配置拒绝，0 写入并退回人工确认"""
    switches(auto_execute=True)
    sdk = FakeSDK(pods=CRASHING)

    def revoke_then_read():
        monkeypatch.setattr(config.autofix, "auto_execute_enabled", False)
        return sdk_objects(CRASHING)

    sdk.pods_effect = revoke_then_read
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == "needs_human_confirmation"
    assert data["executed_actions"] == []
    assert data["execution"]["refusal"] == "mutation_gate"
    assert data["approval"]["state"] == "pending"
    assert sdk.writes() == []
    assert [(r["phase"], r["result"], r["reason"]) for r in sink.records] == [("refused", "refused", "switch disabled")]


def test_auto_path_gate_refuses_when_namespace_revoked_before_write(monkeypatch, switches, sink) -> None:
    switches(auto_execute=True)
    sdk = FakeSDK(pods=CRASHING)

    def revoke_then_read():
        monkeypatch.setattr(config.autofix, "allowed_namespaces", [])
        return sdk_objects(CRASHING)

    sdk.pods_effect = revoke_then_read
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == "needs_human_confirmation"
    assert sdk.writes() == []
    assert [r["reason"] for r in sink.records] == ["namespace not allowed"]


@pytest.mark.parametrize("trigger", ["auto", "confirm"])
async def test_engine_write_entry_gate_blocks_both_triggers(switches, sink, human, trigger) -> None:
    """引擎自身白名单放行、但当前配置不允许：auto 与 confirm 两条路径在写入口都被拒绝"""
    switches(auto_execute=True, namespaces=[])
    sdk = FakeSDK(pods=CRASHING)
    engine = AutoFixRemediationEngine(make_k8s_service(sdk), allowed_namespaces=[NS], auto_execute=True)
    plan = await engine.build_plan({"deployment": "api", "namespace": NS, "event": CRASHLOOP})
    assert [a["action_type"] for a in plan["allowed_actions"]] == ["restart_deployment"]

    result = await engine.execute_plan(plan, trigger=trigger)

    assert result["status"] == "blocked"
    assert result["refusal"] == "mutation_gate"
    assert result["executed_actions"] == []
    assert sdk.writes() == []
    assert [r["phase"] for r in sink.records] == ["refused"]
    assert sink.records[0]["auto_executed"] is (trigger == "auto")


async def test_confirm_trigger_requires_namespace_but_not_auto_switch(switches, sink, human) -> None:
    switches(auto_execute=False)
    sdk = FakeSDK()
    engine = AutoFixRemediationEngine(make_k8s_service(sdk), allowed_namespaces=[NS])
    action = {"action_id": "restart-deployment", "action_type": "restart_deployment", "target": {"namespace": NS, "name": "api"}}
    plan = {"deployment": "api", "namespace": NS, "allowed_actions": [action], "blocked_actions": []}

    auto = await engine.execute_plan(plan, trigger="auto")
    assert auto["status"] == "blocked" and auto["refusal"] == "auto_eligibility"
    assert sdk.writes() == []

    confirmed = await engine.execute_plan(plan, trigger="confirm")
    assert confirmed["status"] == "completed"
    assert sdk.writes() == ["patch_namespaced_deployment"]


async def test_forged_allowed_plan_cannot_auto_execute(switches, sink) -> None:
    """execute_plan(trigger=auto) 独立校验：伪造成 allowed 的计划（没有实时证据 / 非白名单）不写集群"""
    switches(auto_execute=True)
    sdk = FakeSDK()
    engine = AutoFixRemediationEngine(make_k8s_service(sdk), allowed_namespaces=[NS], auto_execute=True)
    forged = [
        {"action_id": "r", "action_type": "restart_deployment", "risk_assessment": {"allowed": True, "risk_level": "low"}},
        {
            "action_id": "s",
            "action_type": "scale_deployment",
            "parameters": {"replicas": 2},
            "risk_assessment": {"allowed": True, "risk_level": "low", "live_evidence": {"source": "live", "fault_type": "resource_insufficient", "matched": True}},
        },
    ]
    for action in forged:
        result = await engine.execute_plan({"deployment": "api", "namespace": NS, "allowed_actions": [action], "blocked_actions": []})
        assert result["status"] == "blocked" and result["refusal"] == "auto_eligibility"
    assert sdk.writes() == [] and sink.records == []


def test_engine_trigger_must_be_explicit_value() -> None:
    engine = AutoFixRemediationEngine(None, allowed_namespaces=[NS])
    with pytest.raises(ValueError):
        import asyncio

        asyncio.run(engine.execute_plan({"allowed_actions": []}, trigger="manual"))


# ---------------------------------------------------------------- G4：审计失败不执行、内容不含敏感值


def test_intent_audit_failure_blocks_auto_execution(monkeypatch, switches) -> None:
    switches(auto_execute=True)
    failing = ListSink(fail_phases=("intent",))
    previous = audit_module.set_audit_sink(failing)
    try:
        sdk = FakeSDK(pods=CRASHING)
        client = make_app(monkeypatch, make_service(sdk))
        data = run_workflow(client, CRASHLOOP)
    finally:
        audit_module.set_audit_sink(previous)

    assert data["status"] == "needs_human_confirmation"
    assert data["execution"]["refusal"] == "audit"
    assert data["executed_actions"] == []
    assert sdk.writes() == []
    assert SECRET_MARKER not in str(data)


def test_intent_audit_failure_blocks_confirmed_execution(monkeypatch, switches) -> None:
    switches(auto_execute=False)
    sdk = FakeSDK()
    client = make_app(monkeypatch, make_service(sdk))
    data = run_workflow(client, CRASHLOOP)
    restart = pending_action(data, "restart_deployment")

    previous = audit_module.set_audit_sink(ListSink(fail_phases=("intent",)))
    try:
        response = client.post("/autofix/workflow/confirm", json={"plan_id": data["plan"]["plan_id"], "approved_action_ids": [restart["action_id"]]})
    finally:
        audit_module.set_audit_sink(previous)

    assert response.status_code == 503
    assert "审计" in response.json()["message"]
    assert SECRET_MARKER not in response.text
    assert sdk.writes() == []


async def test_result_audit_failure_is_flagged_after_write(switches, human, caplog) -> None:
    """写入已提交后结果审计失败：无法回退，只在执行记录上标记 audit_result_recorded=False"""
    switches(auto_execute=False)
    failing = ListSink(fail_phases=("result",))
    previous = audit_module.set_audit_sink(failing)
    try:
        sdk = FakeSDK()
        engine = AutoFixRemediationEngine(make_k8s_service(sdk), allowed_namespaces=[NS])
        action = {"action_id": "restart-deployment", "action_type": "restart_deployment"}
        result = await engine.execute_plan({"deployment": "api", "namespace": NS, "allowed_actions": [action], "blocked_actions": []}, trigger="confirm")
    finally:
        audit_module.set_audit_sink(previous)
    assert sdk.writes() == ["patch_namespaced_deployment"]
    assert result["executed_actions"][0]["audit_result_recorded"] is False
    assert result["audit_result_recorded"] is False
    assert [r["phase"] for r in failing.records] == ["intent"]


async def test_audit_has_no_patch_values_or_exception_text(switches, sink, human) -> None:
    switches(auto_execute=False)
    client = AsyncMock()
    client.patch_deployment.side_effect = RuntimeError(f"apiserver said {SECRET_MARKER}")
    client.wait_for_deployment_rollout.return_value = {"ready": True}
    engine = AutoFixRemediationEngine(client, allowed_namespaces=[NS])
    action = {
        "action_id": "adjust-resource-requests",
        "action_type": "adjust_resource_requests",
        "parameters": {"container": "api", "cpu": "777m", "memory": "999Mi"},
    }

    result = await engine.execute_plan({"deployment": "api", "namespace": NS, "allowed_actions": [action], "blocked_actions": []}, trigger="confirm")

    assert result["status"] == "failed"
    assert [r["phase"] for r in sink.records] == ["intent", "result"]
    final = sink.records[-1]
    assert final["result"] == "failed" and final["error_type"] == "RuntimeError"
    dumped = str(sink.records)
    for forbidden in (SECRET_MARKER, "apiserver said", "777m", "999Mi", "parameters", "'cpu'", "'memory'"):
        assert forbidden not in dumped
    assert SECRET_MARKER not in str(result)


def test_principal_defaults_to_unknown_without_context() -> None:
    from app.common.identity import principal_context

    token = principal_context.set(None)
    try:
        record = audit_module.build_audit_record("intent", audit_id="x", namespace=NS, name="api", action={"action_type": "restart_deployment", "parameters": {"replicas": 9}}, auto_executed=True)
    finally:
        principal_context.reset(token)
    assert record["principal"] == "unknown"
    assert set(record) == AUDIT_KEYS
    assert "parameters" not in str(record)


def test_logger_sink_writes_one_json_line() -> None:
    """P2-3：默认 sink 只写纯 JSON 行（无控制台前缀），写入注入的流"""
    import io
    import json

    record = audit_module.build_audit_record("intent", audit_id="x", namespace=NS, name="api", action={"action_id": "a", "action_type": "restart_deployment"}, auto_executed=False)
    stream = io.StringIO()
    audit_module.LoggerAuditSink(stream=stream).write(record)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1 and json.loads(lines[0]) == record
    assert stream.getvalue() == lines[0] + "\n"
    with pytest.raises(TypeError):
        audit_module.set_audit_sink(object())


async def test_service_maps_gate_refusal_to_403(monkeypatch) -> None:
    """confirm 时执行引擎在写入口拒绝（门禁）→ 403，审批作废、不可重试"""
    from app.common.identity import principal_scope
    from tests.unit.test_autofix_approval_isolation import ACTION, create_plan, pending_result  # noqa: F401

    service = AutoFixService()
    service._initialized = True
    original = pending_result()
    service._ops_workflow = MagicMock()
    service._ops_workflow.run = AsyncMock(side_effect=lambda *_a, **_k: deepcopy(original))
    engine = MagicMock()
    engine.build_plan = AsyncMock(side_effect=lambda *_a, **_k: deepcopy(original["plan"]))
    engine.namespace_refusal = MagicMock(return_value=None)
    engine.with_policy = MagicMock(return_value=engine)
    engine.execute_plan = AsyncMock(return_value={"status": "blocked", "executed_actions": [], "refusal": "mutation_gate", "error": "x"})
    service._remediation_engine = engine
    plan_id = await create_plan(service)
    with principal_scope("user:1"):
        with pytest.raises(AIOpsException) as raised:
            await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert raised.value.status_code == 403
    assert "门禁" in raised.value.message
    assert engine.execute_plan.await_args.kwargs == {"trigger": "confirm"}


# ---------------------------------------------------------------- SEC-44 修正：结果审计失败上报、确认路径主体


def result_audit_error_logs(caplog) -> List[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "aiops.autofix.remediation" and r.levelname == "ERROR" and "结果审计写入失败" in r.getMessage()
    ]


def test_result_audit_failure_on_confirm_is_reported(monkeypatch, switches, caplog) -> None:
    switches(auto_execute=False)
    sdk = FakeSDK()
    client = make_app(monkeypatch, make_service(sdk))
    data = run_workflow(client, CRASHLOOP)
    restart = pending_action(data, "restart_deployment")

    failing = ListSink(fail_phases=("result",))
    previous = audit_module.set_audit_sink(failing)
    try:
        with caplog.at_level("ERROR", logger="aiops.autofix.remediation"):
            response = client.post("/autofix/workflow/confirm", json={"plan_id": data["plan"]["plan_id"], "approved_action_ids": [restart["action_id"]]})
    finally:
        audit_module.set_audit_sink(previous)

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert sdk.writes() == ["patch_namespaced_deployment"]
    assert body["audit_result_recorded"] is False
    assert body["execution"]["audit_result_recorded"] is False
    assert body["executed_actions"][0]["audit_result_recorded"] is False
    logs = result_audit_error_logs(caplog)
    assert len(logs) == 1
    assert "OSError" in logs[0]
    assert SECRET_MARKER not in logs[0] and failing.records[0]["audit_id"] not in logs[0]
    assert SECRET_MARKER not in response.text


def test_result_audit_failure_on_auto_run_is_reported(monkeypatch, switches, caplog) -> None:
    switches(auto_execute=True)
    sdk = FakeSDK(pods=CRASHING)
    failing = ListSink(fail_phases=("result",))
    previous = audit_module.set_audit_sink(failing)
    try:
        client = make_app(monkeypatch, make_service(sdk))
        with caplog.at_level("ERROR", logger="aiops.autofix.remediation"):
            data = run_workflow(client, CRASHLOOP)
    finally:
        audit_module.set_audit_sink(previous)

    assert sdk.writes() == ["patch_namespaced_deployment"]
    assert data["audit_result_recorded"] is False
    assert data["execution"]["audit_result_recorded"] is False
    assert len(result_audit_error_logs(caplog)) == 1


def test_successful_audit_reports_recorded_true(monkeypatch, switches, sink) -> None:
    switches(auto_execute=True)
    sdk = FakeSDK(pods=CRASHING)
    client = make_app(monkeypatch, make_service(sdk))
    data = run_workflow(client, CRASHLOOP)
    assert data["status"] == "completed"
    assert data["audit_result_recorded"] is True
    assert data["execution"]["audit_result_recorded"] is True


def test_confirm_without_user_identity_is_refused_with_401(monkeypatch, switches, sink) -> None:
    """缺少 X-CloudOps-User-ID（中间件记为 service:internal）的确认：401，记一条 refused 审计，0 写入"""
    from fastapi.testclient import TestClient

    from tests.unit.test_autofix_sec26_repair_bootstrap import KEY

    switches(auto_execute=False)
    sdk = FakeSDK()
    client = make_app(monkeypatch, make_service(sdk))
    internal = TestClient(client.app, headers={"X-API-Key": KEY})
    data = run_workflow(internal, CRASHLOOP)
    restart = pending_action(data, "restart_deployment")

    response = internal.post("/autofix/workflow/confirm", json={"plan_id": data["plan"]["plan_id"], "approved_action_ids": [restart["action_id"]]})

    assert response.status_code == 401, response.text
    assert sdk.writes() == []
    assert [(r["phase"], r["result"], r["reason"], r["principal"], r["auto_executed"]) for r in sink.records] == [
        ("refused", "refused", "principal missing", "service:internal", False)
    ]


def test_auto_run_without_user_identity_records_internal_principal(monkeypatch, switches, sink) -> None:
    from fastapi.testclient import TestClient

    from tests.unit.test_autofix_sec26_repair_bootstrap import KEY

    switches(auto_execute=True)
    sdk = FakeSDK(pods=CRASHING)
    client = make_app(monkeypatch, make_service(sdk))
    internal = TestClient(client.app, headers={"X-API-Key": KEY})

    data = run_workflow(internal, CRASHLOOP)

    assert data["status"] == "completed"
    assert sdk.writes() == ["patch_namespaced_deployment"]
    assert [r["principal"] for r in sink.records] == ["service:internal", "service:internal"]


@pytest.mark.parametrize("principal", [None, "service:internal", "", "user:"])
async def test_engine_confirm_requires_user_principal(switches, sink, principal) -> None:
    from app.common.identity import principal_scope

    switches(auto_execute=False)
    sdk = FakeSDK()
    engine = AutoFixRemediationEngine(make_k8s_service(sdk), allowed_namespaces=[NS])
    action = {"action_id": "restart-deployment", "action_type": "restart_deployment"}
    with principal_scope(principal):
        result = await engine.execute_plan({"deployment": "api", "namespace": NS, "allowed_actions": [action], "blocked_actions": []}, trigger="confirm")

    assert result["status"] == "blocked" and result["refusal"] == "principal"
    assert sdk.writes() == []
    assert [(r["phase"], r["reason"]) for r in sink.records] == [("refused", "principal missing")]
    assert sink.records[0]["principal"] == (principal or "unknown")


async def test_service_maps_principal_refusal_to_401() -> None:
    from app.common.identity import principal_scope
    from tests.unit.test_autofix_approval_isolation import ACTION, create_plan, pending_result

    service = AutoFixService()
    service._initialized = True
    original = pending_result()
    service._ops_workflow = MagicMock()
    service._ops_workflow.run = AsyncMock(side_effect=lambda *_a, **_k: deepcopy(original))
    engine = MagicMock()
    engine.build_plan = AsyncMock(side_effect=lambda *_a, **_k: deepcopy(original["plan"]))
    engine.namespace_refusal = MagicMock(return_value=None)
    engine.with_policy = MagicMock(return_value=engine)
    engine.execute_plan = AsyncMock(return_value={"status": "blocked", "executed_actions": [], "refusal": "principal", "error": "x"})
    service._remediation_engine = engine
    plan_id = await create_plan(service)
    with principal_scope("user:1"):
        with pytest.raises(AIOpsException) as raised:
            await service.confirm_multi_agent_workflow(plan_id, [ACTION])
    assert raised.value.status_code == 401


# ---------------------------------------------------------------- SEC-44 修正（03:33 TL Review P2/P3）


@pytest.mark.parametrize(
    ("restarts", "ready", "expected"),
    [(2, False, False), (3, False, True), (3, True, False), (1, False, False), (0, False, False)],
    ids=["2-not-ready", "3-not-ready", "3-ready", "1-not-ready", "0-not-ready"],
)
def test_high_restart_signal_threshold_is_three_and_requires_not_ready(restarts, ready, expected) -> None:
    """P2-1 / M38：阈值锁定为 3（改成 1 / 2 / 4 都会被抓到），且容器当前 Ready=True 时不认"""
    signals = AutoFixRemediationEngine._pod_signals([pod(restarts=restarts, ready=ready)])
    assert ("high_restart_count" in signals) is expected
    assert remediation.HIGH_RESTART_COUNT == 3


def test_ready_pod_with_ready_missing_counts_as_not_ready() -> None:
    """ready 字段缺失按未 Ready 处理（fail-closed 只影响「能否算证据」，缺字段时信号仍需 restart>=3）"""
    item = pod(restarts=3, ready=False)
    item["status"]["container_statuses"][0].pop("ready")
    assert "high_restart_count" in AutoFixRemediationEngine._pod_signals([item])


def test_selector_with_match_expressions_fails_closed(monkeypatch, switches, sink) -> None:
    """P2-2：Deployment selector 含 matchExpressions 时不读 Pod、不自动执行（丢掉表达式会放大选择范围）"""
    switches(auto_execute=True)
    deployment = live_deployment()
    deployment["spec"]["selector"]["match_expressions"] = [{"key": "tier", "operator": "In", "values": ["web"]}]
    sdk = FakeSDK(pods=CRASHING, deployment=deployment)
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == "needs_human_confirmation"
    evidence = pending_action(data, "restart_deployment")["risk_assessment"]["live_evidence"]
    assert evidence["matched"] is False and "matchExpressions" in evidence["reason"]
    assert sdk.pod_reads == 0
    assert sdk.writes() == [] and sink.records == []


@pytest.mark.parametrize("key", ["match_expressions", "matchExpressions"])
def test_label_selector_returns_empty_when_match_expressions_present(key) -> None:
    selector = {"match_labels": {"app": "api"}, key: [{"key": "tier", "operator": "Exists"}]}
    assert AutoFixRemediationEngine._label_selector({"spec": {"selector": selector}}) == ""
    assert AutoFixRemediationEngine._label_selector({"spec": {"selector": {"match_labels": {"app": "api"}, key: []}}}) == "app=api"


def foreign_pod(**metadata_kwargs) -> Dict[str, Any]:
    item = pod(waiting="CrashLoopBackOff", restarts=5, ready=False)
    item["metadata"] = pod_metadata(**metadata_kwargs)
    return item


@pytest.mark.parametrize(
    "item",
    [
        # TL 实测：同 label 的另一个 Deployment（RS api-worker-6d4f）里 crashloop 的 Pod
        foreign_pod(rs_name="api-worker-6d4f", pod_hash="6d4f"),
        foreign_pod(controller=False),
        foreign_pod(kind="StatefulSet", rs_name="api-" + POD_HASH),
        foreign_pod(pod_hash="9999"),
    ],
    ids=["other-deployment-rs", "not-controller", "not-replicaset", "hash-mismatch"],
)
def test_pods_not_owned_by_target_deployment_are_ignored(monkeypatch, switches, sink, item) -> None:
    """P2-2：只采纳 controller ownerReference 指向目标 Deployment ReplicaSet（api-<pod-template-hash>）的 Pod"""
    switches(auto_execute=True)
    sdk = FakeSDK(pods=[item, pod()])
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == "needs_human_confirmation", data
    evidence = pending_action(data, "restart_deployment")["risk_assessment"]["live_evidence"]
    assert evidence["matched"] is False
    assert evidence["pods_checked"] == 1
    assert sdk.writes() == [] and sink.records == []


def test_pod_without_owner_references_is_ignored(monkeypatch, switches, sink) -> None:
    switches(auto_execute=True)
    item = pod(waiting="CrashLoopBackOff", restarts=5, ready=False)
    item["metadata"].pop("owner_references")
    sdk = FakeSDK(pods=[item])
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == "needs_human_confirmation"
    assert "no live pods owned" in pending_action(data, "restart_deployment")["risk_assessment"]["live_evidence"]["reason"]
    assert sdk.writes() == [] and sink.records == []


@pytest.mark.parametrize(
    "rs",
    [
        replica_set(owner=None),  # 名字仿成 api-<hash> 的裸 RS
        replica_set(owner="api-worker"),
        replica_set(owner_kind="StatefulSet"),
        replica_set(revision="2"),  # 旧 ReplicaSet（非当前 revision）
        None,  # 读取失败
    ],
    ids=["bare-rs", "other-owner", "owner-not-deployment", "old-revision", "rs-read-failed"],
)
def test_replica_set_must_be_current_and_controlled_by_target(monkeypatch, switches, sink, rs) -> None:
    """P2-2：再读一次 ReplicaSet，核对其 controller owner 是目标 Deployment 且 revision 与 Deployment 当前一致"""
    switches(auto_execute=True)
    sdk = FakeSDK(pods=CRASHING)
    if rs is None:
        sdk.replica_sets = {}
    else:
        sdk.replica_sets = {"api-" + POD_HASH: rs}
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == "needs_human_confirmation", data
    assert pending_action(data, "restart_deployment")["risk_assessment"]["live_evidence"]["matched"] is False
    assert sdk.rs_reads >= 1
    assert sdk.writes() == [] and sink.records == []


def test_deployment_without_revision_annotation_fails_closed(monkeypatch, switches, sink) -> None:
    switches(auto_execute=True)
    deployment = live_deployment()
    deployment["metadata"].pop("annotations")
    sdk = FakeSDK(pods=CRASHING, deployment=deployment)
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == "needs_human_confirmation"
    assert sdk.writes() == [] and sink.records == []


def test_owned_current_pods_still_auto_execute(monkeypatch, switches, sink) -> None:
    """对照：Pod 属于目标 Deployment 当前 RS → 证据成立，自动执行；RS 只读一次（同 RS 多 Pod 去重）"""
    switches(auto_execute=True)
    sdk = FakeSDK(pods=CRASHING + [pod(waiting="CrashLoopBackOff", restarts=4, ready=False)])
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == "completed", data
    assert sdk.writes() == ["patch_namespaced_deployment"]
    assert sdk.apps_v1.read_namespaced_replica_set.call_count == 2  # 计划阶段 1 次 + 执行前复核 1 次


def test_live_evidence_rechecked_before_auto_write(monkeypatch, switches, sink) -> None:
    """P3-1 TOCTOU：计划阶段证据成立，执行前重新读取发现 Pod 已恢复 → 退回 pending，0 写入"""
    switches(auto_execute=True)
    sdk = FakeSDK(pods=CRASHING)
    reads = {"n": 0}

    def recover_after_planning():
        reads["n"] += 1
        return sdk_objects(CRASHING if reads["n"] == 1 else HEALTHY)

    sdk.pods_effect = recover_after_planning
    client = make_app(monkeypatch, make_service(sdk))

    data = run_workflow(client, CRASHLOOP)

    assert data["status"] == "needs_human_confirmation", data
    assert data["execution"]["refusal"] == "live_evidence"
    assert data["approval"]["state"] == "pending"
    assert reads["n"] == 2
    assert sdk.writes() == [] and sink.records == []


async def test_engine_auto_execute_rechecks_evidence_but_confirm_does_not(switches, sink, human) -> None:
    switches(auto_execute=True)
    sdk = FakeSDK(pods=CRASHING)
    engine = AutoFixRemediationEngine(make_k8s_service(sdk), allowed_namespaces=[NS], auto_execute=True)
    plan = await engine.build_plan({"deployment": "api", "namespace": NS, "event": CRASHLOOP})
    assert [a["action_type"] for a in plan["allowed_actions"]] == ["restart_deployment"]
    sdk.pods = HEALTHY

    auto = await engine.execute_plan(plan, trigger="auto")
    assert auto["status"] == "blocked" and auto["refusal"] == "live_evidence"
    assert sdk.writes() == [] and sink.records == []

    # 人工确认路径不要求实时证据（人已看过计划）
    confirmed = await engine.execute_plan(plan, trigger="confirm")
    assert confirmed["status"] == "completed"
    assert sdk.writes() == ["patch_namespaced_deployment"]


def test_execute_plan_docstring_requires_server_side_evidence() -> None:
    """P3-4：将来若允许外部传入计划，live_evidence 必须在服务端重新推导"""
    doc = AutoFixRemediationEngine.execute_plan.__doc__ or ""
    assert "外部传入计划" in doc and "服务端重新推导" in doc


# ---------------------------------------------------------------- P2-3 默认审计 sink 不受 LOG_LEVEL 影响、写失败必须上抛


@pytest.fixture
def quiet_logging():
    """模拟 LOG_LEVEL=WARNING：root 与 aiops 及其子 logger 都调到 WARNING"""
    import logging

    names = ["", "aiops", "aiops.audit", "aiops.audit.autofix"]
    saved = {name: logging.getLogger(name).level for name in names}
    for name in names:
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        yield
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


def test_default_sink_writes_json_line_to_stdout_under_warning_level(quiet_logging, capsys) -> None:
    import json

    record = audit_module.build_audit_record("intent", audit_id="y", namespace=NS, name="api", action={"action_id": "a", "action_type": "restart_deployment"}, auto_executed=True)
    audit_module.get_audit_sink().write(record)
    out = capsys.readouterr().out.splitlines()
    assert out == [json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)]
    assert " | INFO | " not in out[0]


def test_default_sink_ignores_logging_disable_and_logger_level(quiet_logging) -> None:
    import io
    import json
    import logging

    stream = io.StringIO()
    logging.disable(logging.CRITICAL)
    try:
        audit_module.LoggerAuditSink(stream=stream).write({"phase": "intent", "audit_id": "z"})
    finally:
        logging.disable(logging.NOTSET)
    assert json.loads(stream.getvalue()) == {"phase": "intent", "audit_id": "z"}


def test_audit_logger_is_isolated_from_console_handlers() -> None:
    import logging

    logger = logging.getLogger("aiops.audit.autofix")
    assert logger.propagate is False
    assert logger.level == logging.INFO


class BrokenStream:
    def __init__(self, fail_on: str = "write") -> None:
        self.fail_on = fail_on

    def write(self, _data: str) -> int:
        if self.fail_on == "write":
            raise OSError(f"disk full {SECRET_MARKER}")
        return 0

    def flush(self) -> None:
        if self.fail_on == "flush":
            raise OSError(f"broken pipe {SECRET_MARKER}")


@pytest.mark.parametrize("fail_on", ["write", "flush"])
def test_default_sink_write_failure_raises(fail_on) -> None:
    with pytest.raises(OSError):
        audit_module.LoggerAuditSink(stream=BrokenStream(fail_on)).write({"phase": "intent"})


async def test_default_sink_stream_failure_blocks_auto_execution(switches) -> None:
    """P2-3：默认 sink 的流写失败 → 意图审计失败 → 不执行任何写操作"""
    switches(auto_execute=True)
    previous = audit_module.set_audit_sink(audit_module.LoggerAuditSink(stream=BrokenStream()))
    try:
        sdk = FakeSDK(pods=CRASHING)
        engine = AutoFixRemediationEngine(make_k8s_service(sdk), allowed_namespaces=[NS], auto_execute=True)
        plan = await engine.build_plan({"deployment": "api", "namespace": NS, "event": CRASHLOOP})
        result = await engine.execute_plan(plan, trigger="auto")
    finally:
        audit_module.set_audit_sink(previous)
    assert result["status"] == "blocked" and result["refusal"] == "audit"
    assert sdk.writes() == []
    assert SECRET_MARKER not in str(result)


def test_default_sink_stream_failure_on_confirm_is_503(monkeypatch, switches) -> None:
    """P2-3：确认路径默认 sink 写失败 → 503，0 写入"""
    switches(auto_execute=False)
    sdk = FakeSDK(pods=CRASHING)
    client = make_app(monkeypatch, make_service(sdk))
    data = run_workflow(client, CRASHLOOP)
    action = pending_action(data, "restart_deployment")
    previous = audit_module.set_audit_sink(audit_module.LoggerAuditSink(stream=BrokenStream()))
    try:
        response = client.post("/autofix/workflow/confirm", json={"plan_id": data["plan"]["plan_id"], "approved_action_ids": [action["action_id"]]})
    finally:
        audit_module.set_audit_sink(previous)
    assert response.status_code == 503
    assert sdk.writes() == []
    assert SECRET_MARKER not in response.text


def test_setup_logging_with_warning_level_keeps_audit_output(monkeypatch, capsys) -> None:
    """P2-3：按 LOG_LEVEL=WARNING 走一遍真实 setup_logging，默认审计 sink 仍写出纯 JSON 行"""
    import json
    import logging

    from app.config import logging as logging_config
    from app.config.settings import config as app_config

    root = logging.getLogger()
    names = ["aiops", "aiops.services", "aiops.core", "aiops.api", "aiops.routes", "aiops.error_handler"]
    saved_handlers, saved_root_level = root.handlers[:], root.level
    saved_levels = {name: (logging.getLogger(name).level, logging.getLogger(name).propagate) for name in names}
    monkeypatch.setattr(app_config, "log_level", "WARNING")
    try:
        logging_config.setup_logging()
        logging.getLogger("aiops.core.autofix").info("console info should be filtered")
        audit_module.get_audit_sink().write({"phase": "intent", "audit_id": "w"})
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_root_level)
        for name, (level, propagate) in saved_levels.items():
            logging.getLogger(name).setLevel(level)
            logging.getLogger(name).propagate = propagate
    out = capsys.readouterr().out.splitlines()
    assert out == [json.dumps({"audit_id": "w", "phase": "intent"}, sort_keys=True)]


# ---------------------------------------------------------------- P2-4 / P3-2 / P3-3 锁定测试（M17 / M35 / M04 / M26 / M28 / M30）


async def test_mutation_gate_unexpected_error_refuses_with_zero_writes(monkeypatch, switches, sink, human) -> None:
    """P2-4 / M17：ensure_mutation_allowed 抛非预期异常（RuntimeError）→ 按拒绝处理：0 写入 + 一条 refused 审计"""
    from app.core.autofix import policy

    switches(auto_execute=False)

    def broken_gate(*_args, **_kwargs):
        raise RuntimeError(f"gate backend exploded {SECRET_MARKER}")

    monkeypatch.setattr(policy, "ensure_mutation_allowed", broken_gate)
    sdk = FakeSDK(pods=CRASHING)
    engine = AutoFixRemediationEngine(make_k8s_service(sdk), allowed_namespaces=[NS], auto_execute=False)
    action = {"action_id": "restart-api", "action_type": "restart_deployment", "risk_assessment": {"allowed": True}}

    record = await engine._execute_action(action, "api", NS, auto_executed=False)

    assert record["status"] == "refused" and record["refusal"] == "mutation_gate"
    assert sdk.writes() == []
    assert [(r["phase"], r["reason"]) for r in sink.records] == [("refused", "gate error")]
    assert SECRET_MARKER not in str(record) and SECRET_MARKER not in str(sink.records)


async def test_get_pods_default_call_returns_empty_list_on_api_exception() -> None:
    """P2-4 / M35：get_pods 默认调用（raise_errors=False）遇 ApiException 仍返回 []，其它调用方行为不变"""
    sdk = FakeSDK()
    sdk.core_v1.list_namespaced_pod.side_effect = ApiException(status=500, reason="Internal Server Error")
    service = make_k8s_service(sdk)

    assert await service.get_pods(NS, "app=api") == []
    with pytest.raises(ApiException):
        await service.get_pods(NS, "app=api", raise_errors=True)


async def test_get_pods_default_call_returns_empty_list_on_other_exception() -> None:
    sdk = FakeSDK()
    sdk.core_v1.list_namespaced_pod.side_effect = ValueError("bad payload")
    assert await make_k8s_service(sdk).get_pods(NS, "app=api") == []


async def test_execute_plan_auto_checks_engine_auto_execute_flag(switches, sink) -> None:
    """P3-2 / M04：引擎 auto_execute=False 时，即使计划里动作 allowed 且实时证据匹配，trigger=auto 也不执行"""
    switches(auto_execute=True)
    sdk = FakeSDK(pods=CRASHING)
    planner = AutoFixRemediationEngine(make_k8s_service(sdk), allowed_namespaces=[NS], auto_execute=True)
    plan = await planner.build_plan({"deployment": "api", "namespace": NS, "event": CRASHLOOP})
    assert [a["action_type"] for a in plan["allowed_actions"]] == ["restart_deployment"]

    executor = AutoFixRemediationEngine(make_k8s_service(sdk), allowed_namespaces=[NS], auto_execute=False)
    result = await executor.execute_plan(plan, trigger="auto")

    assert result["status"] == "blocked" and result["refusal"] == "auto_eligibility"
    assert sdk.writes() == [] and sink.records == []


async def test_execute_action_defaults_to_auto_executed(switches, sink) -> None:
    """P3-2 / M26：_execute_action 不传 auto_executed 时按自动执行（最严）处理：需要 auto_execute 开关"""
    switches(auto_execute=False)
    sdk = FakeSDK(pods=CRASHING)
    engine = AutoFixRemediationEngine(make_k8s_service(sdk), allowed_namespaces=[NS], auto_execute=True)
    action = {"action_id": "restart-api", "action_type": "restart_deployment", "risk_assessment": {"allowed": True}}

    record = await engine._execute_action(action, "api", NS)

    assert record["status"] == "refused" and record["refusal"] == "mutation_gate"
    assert sdk.writes() == []
    assert [(r["phase"], r["auto_executed"], r["reason"]) for r in sink.records] == [("refused", True, "switch disabled")]


async def test_plan_and_execute_uses_auto_trigger(switches, sink, human) -> None:
    """P3-2 / M28：plan_and_execute 属于免确认执行，必须按 trigger=auto（受 auto_execute 开关约束），
    即使调用方有可信用户主体也不能当成人工确认"""
    switches(auto_execute=False)
    sdk = FakeSDK(pods=CRASHING)
    engine = AutoFixRemediationEngine(make_k8s_service(sdk), allowed_namespaces=[NS], auto_execute=True)

    result = await engine.plan_and_execute({"deployment": "api", "namespace": NS, "event": CRASHLOOP})

    assert sdk.writes() == []
    assert result["executed_actions"] == []
    assert [r["auto_executed"] for r in sink.records] == [True]


async def test_confirm_principal_comes_only_from_principal_context(switches, sink) -> None:
    """P3-3 / M30：确认执行的主体只取 principal_context；计划 / 动作里自带的 principal 字段一律不采信。
    service:internal（持钥但无 X-CloudOps-User-ID）与未知主体同样拒绝"""
    from app.common.identity import INTERNAL_PRINCIPAL, principal_context, principal_scope

    switches(auto_execute=False)
    sdk = FakeSDK(pods=CRASHING)
    engine = AutoFixRemediationEngine(make_k8s_service(sdk), allowed_namespaces=[NS], auto_execute=False)
    plan = await engine.build_plan({"deployment": "api", "namespace": NS, "event": CRASHLOOP})
    action = deepcopy(next(a for a in plan["blocked_actions"] if a["action_type"] == "restart_deployment"))
    action["principal"] = "user:1"
    action["risk_assessment"]["allowed"] = True
    forged = {**plan, "principal": "user:1", "allowed_actions": [action], "blocked_actions": []}

    assert principal_context.get() == INTERNAL_PRINCIPAL
    result = await engine.execute_plan(forged, trigger="confirm")
    assert result["status"] == "blocked" and result["refusal"] == "principal"

    for principal in (INTERNAL_PRINCIPAL, None, "user:", "service:gateway"):
        with principal_scope(principal):
            record = await engine._execute_action(action, "api", NS, auto_executed=False)
        assert record["refusal"] == "principal", principal
    assert sdk.writes() == []
    assert {r["principal"] for r in sink.records} >= {INTERNAL_PRINCIPAL, "unknown"}
