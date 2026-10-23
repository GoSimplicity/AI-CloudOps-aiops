"""高权限YAML应用必须完成真实SSA请求，所有集群客户端均使用离线替身"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

from kubernetes.client.rest import ApiException
import pytest
import yaml

from app.mcp.server.tools.k8s_resource_tool import K8sResourceTool


def manifest(name="one", **kwargs):
    result = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name}, "data": {"value": "new"}}
    result.update(kwargs)
    return result


def bundle(*resources):
    return yaml.safe_dump_all(resources, allow_unicode=True)


@pytest.fixture
def tool():
    value = K8sResourceTool()
    yield value
    value._executor.shutdown(wait=True)


def clients(*, namespaced=True):
    calls = []
    api = Mock()
    api.namespaced = namespaced
    api.verbs = ["get", "patch", "create"]

    def apply(**kwargs):
        calls.append(deepcopy(kwargs))
        body = deepcopy(kwargs["body"])
        body["metadata"].update(uid="offline-uid", resourceVersion="42")
        return SimpleNamespace(to_dict=lambda: body)

    api.server_side_apply.side_effect = apply
    dynamic = Mock()
    dynamic.resources.get.return_value = api
    return {"dynamic": dynamic}, api, calls


async def test_real_ssa_dry_run_all_then_apply_and_acknowledge(tool):
    deps, api, calls = clients()
    result = await tool._apply_yaml(deps, {"yaml_content": bundle(manifest("one"), manifest("two")), "namespace": "owned"})
    assert result["success"] is True
    assert len(calls) == 4
    assert [call.get("dry_run") for call in calls] == ["All", "All", None, None]
    assert [item["name"] for item in result["applied_resources"]] == ["one", "two"]
    assert all(item["resource_version"] == "42" for item in result["applied_resources"])
    for call in calls:
        assert call["field_manager"] == "ai-cloudops-mcp"
        assert call["force_conflicts"] is False
        assert call["namespace"] == "owned"
        assert call["body"]["metadata"]["namespace"] == "owned"
        assert call["_request_timeout"] == (5, 15)
        assert ("fieldValidation", "Strict") in call["query_params"]
    assert "data" not in result["applied_resources"][0]


INVALID = [
    "", "---\n", "[]", "false", "a: [unclosed", "apiVersion: v1\nkind: ConfigMap\nkind: Secret\nmetadata: {name: one}",
    "apiVersion: v1\nkind: ConfigMap\nmetadata: &x {name: one}\ndata: {test: *x}",
    bundle(manifest(metadata={})), bundle(manifest(metadata={"name": "../unsafe"})),
    bundle(manifest(metadata={"name": "one", "generateName": "other-"})),
    bundle(manifest(metadata={"name": "one", "managedFields": []})),
    bundle(manifest(metadata={"name": "one", "namespace": "other"})),
    bundle(manifest(data={"number": float("nan")})),
    bundle(manifest(apiVersion="")), bundle(manifest(kind="List")),
    bundle(manifest("same"), manifest("same")),
    bundle(*[manifest(f"item-{i}") for i in range(21)]),
    bundle(manifest(data={"blob": "x" * (1024 * 1024)})),
]


@pytest.mark.parametrize("content", INVALID)
async def test_entire_input_rejected_before_any_client_initialization(tool, content):
    tool._initialize_clients = Mock(side_effect=AssertionError("must not initialize"))
    result = await tool._execute_internal({"operation": "apply_yaml", "yaml_content": content, "namespace": "owned"})
    assert result["success"] is False
    assert result.get("applied_resources", []) == []
    tool._initialize_clients.assert_not_called()


async def test_later_unknown_or_cluster_kind_prevents_any_write(tool):
    deps, api, calls = clients()
    deps["dynamic"].resources.get.side_effect = [api, RuntimeError("offline unknown kind")]
    result = await tool._apply_yaml(deps, {"yaml_content": bundle(manifest("one"), manifest("two", kind="Unknown")), "namespace": "owned"})
    assert result["success"] is False
    assert calls == []
    deps, api, calls = clients(namespaced=False)
    result = await tool._apply_yaml(deps, {"yaml_content": bundle(manifest(kind="Namespace"))})
    assert result["success"] is False
    assert calls == []


async def test_later_dry_run_failure_prevents_all_persistent_writes(tool):
    deps, api, calls = clients()
    actual = api.server_side_apply.side_effect

    def fail(**kwargs):
        if kwargs["body"]["metadata"]["name"] == "two":
            raise ApiException(status=403, reason="offline-denied", body="private-token")
        return actual(**kwargs)

    api.server_side_apply.side_effect = fail
    result = await tool._apply_yaml(deps, {"yaml_content": bundle(manifest("one"), manifest("two")), "namespace": "owned"})
    assert result["success"] is False
    assert result["phase"] == "dry_run"
    assert len(calls) == 1 and calls[0]["dry_run"] == "All"
    assert result["applied_resources"] == []
    assert result["outcome_unknown"] is False
    assert "private-token" not in str(result)


@pytest.mark.parametrize("failure,unknown", [(ApiException(status=409, reason="conflict", body="secret-value"), False), (TimeoutError("secret-value"), True)])
async def test_partial_apply_and_conflict_never_report_all_success_or_retry(tool, failure, unknown):
    deps, api, calls = clients()
    actual = api.server_side_apply.side_effect

    def fail(**kwargs):
        if not kwargs.get("dry_run") and kwargs["body"]["metadata"]["name"] == "two":
            raise failure
        return actual(**kwargs)

    api.server_side_apply.side_effect = fail
    result = await tool._apply_yaml(deps, {"yaml_content": bundle(manifest("one"), manifest("two"), manifest("three")), "namespace": "owned"})
    assert result["success"] is False
    assert result["phase"] == "apply"
    assert [item["name"] for item in result["applied_resources"]] == ["one"]
    assert [item["name"] for item in result["not_attempted"]] == ["three"]
    assert result["errors"][0]["name"] == "two"
    assert result["outcome_unknown"] is unknown
    assert result["retry_safe"] is False
    assert "secret-value" not in str(result)
    assert api.server_side_apply.call_count == 5


async def test_unacknowledged_write_is_not_fabricated_as_applied(tool):
    deps, api, calls = clients()
    api.server_side_apply.return_value = None
    api.server_side_apply.side_effect = None
    result = await tool._apply_yaml(deps, {"yaml_content": bundle(manifest()), "namespace": "owned"})
    assert result["success"] is False
    assert result["applied_resources"] == []
    assert result["outcome_unknown"] is True


async def test_slow_sync_ssa_never_blocks_other_coroutines(tool):
    import threading
    deps, api, calls = clients()
    actual = api.server_side_apply.side_effect
    entered = threading.Event()
    release = threading.Event()

    def slow(**kwargs):
        entered.set()
        assert release.wait(2)
        return actual(**kwargs)

    api.server_side_apply.side_effect = slow
    task = asyncio.create_task(tool._apply_yaml(deps, {"yaml_content": bundle(manifest()), "namespace": "owned"}))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        release.set()
        assert (await task)["success"] is True
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_mcp_http_preserves_partial_failure_in_error_result(monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.mcp import main

    partial_result = {"success": False, "applied_resources": [{"name": "one"}], "errors": [{"name": "two", "api_status": 409}], "retry_safe": False}
    monkeypatch.setattr(main, "mcp_server", SimpleNamespace(tools={"offline": object()}, execute_tool=AsyncMock(return_value=partial_result)))
    app = FastAPI()
    app.post("/tools/execute")(main.execute_tool)
    response = TestClient(app).post("/tools/execute", json={"tool": "offline"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]
    assert body["result"] == partial_result


def test_discovery_and_transport_disable_unbounded_requests_and_implicit_retries(monkeypatch):
    from kubernetes.dynamic import DynamicClient

    from app.mcp.server.tools.yaml_apply import BoundedDynamicClient

    captured = []
    monkeypatch.setattr(DynamicClient, "request", lambda self, *args, **kwargs: captured.append(kwargs))
    value = object.__new__(BoundedDynamicClient)
    value.request("get", "/version")
    assert captured == [{"_request_timeout": (5, 15)}]
    from kubernetes import client as k8s_client

    configuration = k8s_client.Configuration()
    configuration.host = "https://bound-source.invalid"
    bound = k8s_client.ApiClient(configuration=configuration)
    api = K8sResourceTool._apply_api_client(bound)
    try:
        assert api.configuration.retries == 0
        # SEC-33 L2 P1-1：复制绑定来源的独立 Configuration，不读全局默认，也不改动绑定客户端
        assert api.configuration.host == "https://bound-source.invalid"
        assert api.configuration is not bound.configuration and bound.configuration.retries is None
    finally:
        api.close()
        bound.close()


async def test_timeout_does_not_claim_a_running_sdk_thread_was_stopped(tool, monkeypatch):
    import threading

    from app.common.constants import ServiceConstants

    release = threading.Event()
    entered = threading.Event()
    deps, api, calls = clients()
    actual = api.server_side_apply.side_effect

    def blocked(**kwargs):
        entered.set()
        assert release.wait(2)
        return actual(**kwargs)

    api.server_side_apply.side_effect = blocked
    tool._initialize_clients = Mock(return_value=deps)
    monkeypatch.setattr(ServiceConstants, "AUTOFIX_WORKFLOW_TIMEOUT", .02)
    try:
        result = await tool.execute({"operation": "apply_yaml", "yaml_content": bundle(manifest()), "namespace": "owned"})
        assert entered.is_set()
        assert result["success"] is False
        assert result["outcome_unknown"] is True and result["retry_safe"] is False
        assert "核对实际状态" in result["message"]
        assert "已中止" not in result["message"]
    finally:
        release.set()


async def test_different_api_versions_cannot_apply_same_discovered_resource_twice(tool):
    deps, api, calls = clients()
    result = await tool._apply_yaml(deps, {"yaml_content": bundle(manifest(apiVersion="example.com/v1"), manifest(apiVersion="example.com/v2")), "namespace": "owned"})
    assert result["success"] is False
    assert calls == []
    assert "重复" in result["errors"][0]["reason"]


async def test_invalid_explicit_config_never_falls_back_to_another_cluster(tool):
    tool._initialize_clients = Mock(side_effect=AssertionError("must not initialize"))
    result = await tool._execute_internal({"operation": "apply_yaml", "yaml_content": bundle(manifest()), "config_path": "/nonexistent/offline-kubeconfig"})
    assert result["success"] is False
    assert "拒绝回退" in result["message"]
    tool._initialize_clients.assert_not_called()


def test_cached_resource_client_never_ignores_a_different_explicit_target(tool, tmp_path, monkeypatch):
    from app.mcp.server.tools.k8s_base_tool import K8sBaseTool

    first, second = tmp_path / "first", tmp_path / "second"
    first.touch()
    second.touch()
    calls = []

    def new_api_client(self, config_path):
        calls.append(config_path)
        return Mock(name="api-client")

    # SEC-33 L2 P1-1：绑定逻辑下沉到 K8sBaseTool，只替换最底层的 kubeconfig 加载，真实走初始化与缓存
    monkeypatch.setattr(K8sBaseTool, "_new_api_client", new_api_client)
    monkeypatch.setattr(K8sResourceTool, "_create_api_clients", lambda self, api_client: {"offline": api_client})
    tool._initialize_clients(str(first))
    tool._initialize_clients(str(first))
    with pytest.raises(ValueError, match="切换集群"):
        tool._initialize_clients(str(second))
    with pytest.raises(ValueError, match="切换集群"):
        tool._initialize_clients(None)
    assert calls == [str(first)]
