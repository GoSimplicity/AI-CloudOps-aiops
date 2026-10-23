import asyncio
from datetime import datetime, timezone
import logging
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from kubernetes.client.rest import ApiException
import pytest

from app.config.settings import config
from app.core.agents.k8s_fixer import K8sFixerAgent
from app.services import kubernetes
from app.utils.workload_context import model_workload_context


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "patch", "rollout", "permission", "health"])
async def test_slow_sdk_does_not_block_event_loop_and_has_network_deadline(monkeypatch, operation):
    monkeypatch.setattr(kubernetes.KubernetesService, "_try_init", lambda self: None)
    service = kubernetes.KubernetesService()
    service.initialized = True
    heartbeat = threading.Event()
    started = threading.Event()
    observed = []
    deployment = SimpleNamespace(to_dict=lambda: {"metadata": {"generation": 1}, "spec": {"replicas": 0}, "status": {"observed_generation": 1}})

    def sdk(**kwargs):
        started.set()
        observed.append((heartbeat.wait(0.2), kwargs.get("_request_timeout")))
        return SimpleNamespace(status=SimpleNamespace(allowed=True)) if operation == "permission" else deployment

    service.apps_v1 = SimpleNamespace(read_namespaced_deployment=sdk, patch_namespaced_deployment=sdk)
    service.core_v1 = SimpleNamespace(list_namespace=lambda **kwargs: None)
    monkeypatch.setattr(kubernetes.client, "AuthorizationV1Api", lambda: SimpleNamespace(create_self_subject_access_review=sdk))
    monkeypatch.setattr(kubernetes.client, "VersionApi", lambda: SimpleNamespace(get_code=sdk))

    async def pulse():
        while not started.is_set():
            await asyncio.sleep(0)
        heartbeat.set()

    calls = {
        "read": lambda: service.get_deployment("owned", "owned"),
        "patch": lambda: service.patch_deployment("owned", {}, "owned"),
        "rollout": lambda: service.wait_for_deployment_rollout("owned", "owned", timeout_seconds=1),
        "permission": lambda: service.can_i("patch", "deployments", "owned"),
        "health": service.health_check,
    }
    await asyncio.wait_for(asyncio.gather(calls[operation](), pulse()), timeout=1)
    assert observed and all(nonblocking and deadline is not None for nonblocking, deadline in observed)


def test_failed_initialization_is_not_reported_as_initialized(monkeypatch):
    monkeypatch.setattr(kubernetes.KubernetesService, "_try_init", lambda self: None)
    assert kubernetes.KubernetesService()._ensure_initialized() is False


def test_missing_explicit_config_does_not_fall_back_to_another_file(monkeypatch, tmp_path):
    monkeypatch.setattr(kubernetes.KubernetesService, "_try_init", lambda self: None)
    other = tmp_path / "other-config"
    other.write_text("apiVersion: v1")
    monkeypatch.delenv("K8S_CONFIG_PATH", raising=False)
    monkeypatch.setenv("KUBECONFIG", str(other))
    with pytest.raises(FileNotFoundError):
        kubernetes.KubernetesService()._resolve_kube_config_path(str(tmp_path / "missing-explicit"))


@pytest.mark.asyncio
async def test_fixer_never_searches_or_switches_cluster_after_failed_health_check(monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/owned/config")
    monkeypatch.setattr(kubernetes.config.k8s, "config_path", "/owned/config")
    client = SimpleNamespace(health_check=AsyncMock(return_value=False), is_healthy=lambda: False, _try_init=lambda: pytest.fail("不能探测其他集群"))
    monkeypatch.setattr("os.path.exists", lambda path: True)
    assert await K8sFixerAgent(k8s_client=client)._check_and_fix_k8s_connection() is False
    client.health_check.assert_awaited_once()
    assert kubernetes.config.k8s.config_path == "/owned/config"
    assert os.environ["KUBECONFIG"] == "/owned/config"


@pytest.mark.asyncio
async def test_bootstrap_mixed_already_exists_and_denied_is_failure_without_raw_body(monkeypatch):
    monkeypatch.setattr(kubernetes.KubernetesService, "_try_init", lambda self: None)
    service = kubernetes.KubernetesService()
    service.initialized = True
    service.apps_v1 = SimpleNamespace(api_client=object())
    errors = [ApiException(status=409, reason="AlreadyExists"), ApiException(status=403, reason="Forbidden")]
    for error in errors:
        error.body = "private-server-body-marker"

    def create(*args, **kwargs):
        raise kubernetes.k8s_utils.FailToCreateError(errors)

    monkeypatch.setattr(kubernetes.k8s_utils, "create_from_yaml", create)
    result = await service.apply_yaml_file("owned.yaml", "owned")
    assert result["success"] is False
    assert "private-server-body-marker" not in str(result)


@pytest.mark.asyncio
async def test_patch_logs_do_not_contain_patch_or_server_exception_body(monkeypatch, caplog):
    monkeypatch.setattr(kubernetes.KubernetesService, "_try_init", lambda self: None)
    service = kubernetes.KubernetesService()
    service.initialized = True

    def patch(**kwargs):
        error = ApiException(status=403, reason="Forbidden")
        error.body = "private-api-response-marker"
        raise error

    service.apps_v1 = SimpleNamespace(patch_namespaced_deployment=patch)
    with caplog.at_level(logging.INFO, logger="aiops.kubernetes"):
        assert await service.patch_deployment("owned", {"spec": {"private": "private-patch-marker"}}, "owned") is False
    assert "private-api-response-marker" not in caplog.text
    assert "private-patch-marker" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_type", ["deployment", "pod"])
async def test_model_context_masks_inline_environment_and_last_applied_copy(resource_type):
    spec = {"containers": [{"name": "owned", "image": "busybox:1.37.0", "env": [{"name": "TOKEN", "value": "private-env-marker"}], "resources": {"requests": {"cpu": "100m"}}}]}
    original = {"metadata": {"annotations": {"kubectl.kubernetes.io/last-applied-configuration": "private-copy-marker", "owner": "local"}}, "spec": {"template": {"spec": spec}} if resource_type == "deployment" else spec}
    result = model_workload_context(original)
    assert "private-env-marker" not in str(result)
    assert "private-copy-marker" not in str(result)
    assert "busybox:1.37.0" in str(result) and "100m" in str(result)
    assert "private-env-marker" in str(original)


@pytest.mark.asyncio
async def test_fixer_sends_only_model_copy_and_keeps_internal_workload_for_repairs(monkeypatch):
    # SEC-26：K8sFixerAgent 写入口受 repair_enabled + allowed_namespaces 门禁，此处显式放行 owned
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["owned"])
    deployment = {
        "metadata": {"creation_timestamp": datetime(2026, 10, 23, tzinfo=timezone.utc), "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "private-copy-marker"}},
        "spec": {"template": {"spec": {"containers": [{"name": "owned", "env": [{"name": "TOKEN", "value": "private-env-marker"}]}]}}},
    }
    k8s = SimpleNamespace(health_check=AsyncMock(return_value=True), get_deployment=AsyncMock(return_value=deployment), get_events=AsyncMock(return_value=[]), get_pods=AsyncMock(return_value=[]))
    llm = SimpleNamespace(model="offline", analyze_k8s_problem=AsyncMock(return_value={"analysis": "offline"}))
    agent = K8sFixerAgent(k8s_client=k8s, llm_client=llm)
    agent.retry_delay = 0
    agent._identify_and_fix_common_issues = AsyncMock(return_value={"fixed": False})
    agent._execute_fix = AsyncMock(return_value="offline")
    agent._verify_fix = AsyncMock(return_value="成功")
    await agent.analyze_and_fix_deployment("owned", "owned", "offline problem")
    llm.analyze_k8s_problem.assert_awaited_once()
    assert len(llm.analyze_k8s_problem.await_args.args) == 3
    sent = str(llm.analyze_k8s_problem.await_args)
    assert "2026-10-23T00:00:00+00:00" in sent
    assert "private-env-marker" not in sent and "private-copy-marker" not in sent
    internal = agent._identify_and_fix_common_issues.await_args.args[0]
    assert internal is deployment and "private-env-marker" in str(internal)


@pytest.mark.asyncio
@pytest.mark.parametrize("desired,updated,ready,total,expected", [(1, 1, 1, 2, False), (1, 2, 2, 2, False), (0, 0, 0, 1, False), (1, 1, 1, 1, True)])
async def test_rollout_waits_for_old_surge_replicas_to_disappear(monkeypatch, desired, updated, ready, total, expected):
    monkeypatch.setattr(kubernetes.KubernetesService, "_try_init", lambda self: None)
    service = kubernetes.KubernetesService()
    service.initialized = True
    status = {"observed_generation": 2, "updated_replicas": updated, "ready_replicas": ready, "available_replicas": ready, "replicas": total}
    deployment = {"metadata": {"generation": 2}, "spec": {"replicas": desired}, "status": status}
    service.apps_v1 = SimpleNamespace(read_namespaced_deployment=lambda **kwargs: SimpleNamespace(to_dict=lambda: deployment))
    result = await service.wait_for_deployment_rollout("owned", "owned", timeout_seconds=0)
    assert result["ready"] is expected
