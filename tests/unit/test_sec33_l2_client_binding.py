"""SEC-33 第二层 P1-1：K8s 工具客户端与配置来源绑定，缓存客户端不能绕过写门禁的 config_path 限制。

全部离线：kubeconfig 加载被替换为把合成 host 写入 Configuration 的假实现，所有 K8s API 方法在类上替换为
只记录「方法名 + 实际使用的 host」的记录器（不替换 _initialize_clients，真实走工具的初始化与缓存逻辑）。
"""

import os
from unittest.mock import Mock

from kubernetes import client, config as kube_config
import pytest

from app.config.settings import config
from app.mcp.server.mcp_server import MCPServer
from app.mcp.server.tools import create_tools, k8s_resource_tool

DEFAULT_HOST = "https://default-cluster.invalid"
ALT_HOST = "https://alt-cluster.invalid"
API_CLASSES = (client.CoreV1Api, client.AppsV1Api, client.NetworkingV1Api, client.AutoscalingV1Api, client.AutoscalingV2Api, client.VersionApi)
API_PREFIXES = ("list_", "read_", "delete_", "patch_", "create_", "replace_", "get_code")


@pytest.fixture
def gates(monkeypatch):
    def set_gates(write=False, secret=False, repair=False, allowed=()):
        monkeypatch.setattr(config.mcp, "write_tools_enabled", write)
        monkeypatch.setattr(config.mcp, "secret_read_enabled", secret)
        monkeypatch.setattr(config.autofix, "repair_enabled", repair)
        monkeypatch.setattr(config.autofix, "allowed_namespaces", list(allowed))

    set_gates()
    return set_gates


@pytest.fixture
def kube(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".kube").mkdir(parents=True)
    (home / ".kube" / "config").write_text("synthetic-default-kubeconfig\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    alt = tmp_path / "alt-kubeconfig"
    alt.write_text("synthetic-alt-kubeconfig\n")
    alt_link = tmp_path / "alt-link"
    alt_link.symlink_to(alt)
    monkeypatch.setattr(client.Configuration, "_default", None)
    loads = []

    def fake_load_kube_config(config_file=None, context=None, client_configuration=None, persist_config=True, temp_file_path=None):
        host = ALT_HOST if config_file and os.path.realpath(config_file) == os.path.realpath(alt) else DEFAULT_HOST
        loads.append((config_file, client_configuration is None))
        if client_configuration is None:
            cfg = client.Configuration()
            cfg.host = host
            client.Configuration.set_default(cfg)
        else:
            client_configuration.host = host

    monkeypatch.setattr(kube_config, "load_kube_config", fake_load_kube_config)
    monkeypatch.setattr(kube_config, "load_incluster_config", Mock(side_effect=AssertionError("in-cluster config must not be used")))
    calls = []

    def recorder(name):
        def method(self, *args, **kwargs):
            calls.append((name, self.api_client.configuration.host))
            raise RuntimeError("synthetic offline k8s api")

        return method

    for cls in API_CLASSES:
        for attr in dir(cls):
            if attr.startswith(API_PREFIXES) and not attr.endswith("_with_http_info"):
                monkeypatch.setattr(cls, attr, recorder(attr))

    dynamic_hosts = []

    def fake_dynamic(api_client):
        dynamic_hosts.append(api_client.configuration.host)
        return Mock(name="dynamic")

    monkeypatch.setattr(k8s_resource_tool, "BoundedDynamicClient", fake_dynamic)

    class Env:
        pass

    env = Env()
    env.alt, env.alt_link, env.calls, env.loads, env.dynamic_hosts = str(alt), str(alt_link), calls, loads, dynamic_hosts
    return env


@pytest.fixture
async def server():
    srv = MCPServer()
    for tool in create_tools():
        await srv.register_tool(tool)
    try:
        yield srv
    finally:
        await srv.shutdown()


def _hosts(calls, prefix=""):
    return [host for name, host in calls if name.startswith(prefix)]


async def test_list_pods_with_alt_config_then_delete_pod_without_config_never_hits_alt(kube, gates, server):
    gates(write=True, repair=True, allowed=["staging"])
    await server.execute_tool("k8s_pod_management", {"operation": "list_pods", "namespace": "staging", "config_path": kube.alt})
    assert ("list_namespaced_pod", ALT_HOST) in kube.calls
    result = await server.execute_tool("k8s_pod_management", {"operation": "delete_pod", "namespace": "staging", "pod_name": "web-0"})
    assert _hosts(kube.calls, "delete_") == []
    assert result["success"] is False
    assert "切换集群" in result["message"]


async def test_list_configmaps_with_alt_config_then_get_secret_never_reads_alt_secret(kube, gates, server):
    gates(secret=True, allowed=["staging"])
    await server.execute_tool("k8s_config_management", {"operation": "list_configmaps", "namespace": "staging", "config_path": kube.alt})
    assert ("list_namespaced_config_map", ALT_HOST) in kube.calls
    result = await server.execute_tool("k8s_config_management", {"operation": "get_secret", "namespace": "staging", "resource_name": "db"})
    assert [c for c in kube.calls if "secret" in c[0]] == []
    assert result["success"] is False
    assert "切换集群" in result["message"]


READS = [
    ("k8s_pod_management", {"operation": "list_pods", "namespace": "staging"}),
    ("k8s_deployment_management", {"operation": "list_deployments", "namespace": "staging"}),
    ("k8s_application_scaling", {"operation": "list_hpa", "namespace": "staging"}),
    ("k8s_service_management", {"operation": "list_services", "namespace": "staging"}),
    ("k8s_ingress_management", {"operation": "list_ingresses", "namespace": "staging"}),
    ("k8s_namespace_management", {"operation": "list_namespaces"}),
    ("k8s_config_management", {"operation": "list_configmaps", "namespace": "staging"}),
    ("k8s_logs_viewer", {"operation": "get_pod_logs", "namespace": "staging", "pod_name": "web-0"}),
    ("k8s_resource_operations", {"operation": "describe_resource", "resource_type": "pod", "resource_name": "web-0", "namespace": "staging"}),
    ("k8s_cluster_check", {}),
]


@pytest.mark.parametrize("tool,params", READS, ids=[r[0] for r in READS])
async def test_every_k8s_tool_binds_clients_to_first_config_source(kube, gates, server, tool, params):
    await server.execute_tool(tool, {**params, "config_path": kube.alt})
    first = list(kube.calls)
    assert first and set(_hosts(first)) == {ALT_HOST}
    # 独立 Configuration：不修改 kubernetes 全局默认配置
    assert client.Configuration.get_default_copy().host != ALT_HOST
    assert all(not global_load for _, global_load in kube.loads)
    result = await server.execute_tool(tool, dict(params))
    assert kube.calls == first, "未带 config_path 的请求不能复用绑定到备用配置的客户端"
    assert result["success"] is False
    assert "切换集群" in result["message"]


@pytest.mark.parametrize("tool,params", READS, ids=[r[0] for r in READS])
async def test_default_bound_tool_refuses_later_alt_config(kube, gates, server, tool, params):
    await server.execute_tool(tool, dict(params))
    assert kube.calls and set(_hosts(kube.calls)) == {DEFAULT_HOST}
    before = list(kube.calls)
    result = await server.execute_tool(tool, {**params, "config_path": kube.alt})
    assert kube.calls == before
    assert result["success"] is False and "切换集群" in result["message"]


async def test_same_source_via_symlink_reuses_clients(kube, gates, server):
    params = {"operation": "list_pods", "namespace": "staging"}
    await server.execute_tool("k8s_pod_management", {**params, "config_path": kube.alt})
    await server.execute_tool("k8s_pod_management", {**params, "config_path": kube.alt_link})
    assert _hosts(kube.calls) == [ALT_HOST, ALT_HOST]
    assert len(kube.loads) == 1


@pytest.mark.parametrize("tool,params", READS[:3], ids=[r[0] for r in READS[:3]])
async def test_missing_explicit_config_never_falls_back_to_default(kube, gates, server, tool, params, tmp_path):
    result = await server.execute_tool(tool, {**params, "config_path": str(tmp_path / "missing-kubeconfig")})
    assert kube.calls == []
    assert result["success"] is False
    # 之后默认配置仍可正常绑定
    await server.execute_tool(tool, dict(params))
    assert set(_hosts(kube.calls)) == {DEFAULT_HOST}


async def test_resource_tool_bound_to_alt_cannot_label_default_and_apply_client_is_independent(kube, gates, server):
    gates(write=True, repair=True, allowed=["staging"])
    await server.execute_tool(
        "k8s_resource_operations",
        {"operation": "describe_resource", "resource_type": "pod", "resource_name": "web-0", "namespace": "staging", "config_path": kube.alt},
    )
    assert kube.dynamic_hosts == [ALT_HOST]
    before = list(kube.calls)
    result = await server.execute_tool(
        "k8s_resource_operations",
        {"operation": "add_labels", "resource_type": "pod", "resource_name": "web-0", "namespace": "staging", "labels": {"a": "b"}},
    )
    assert kube.calls == before
    assert result["success"] is False and "切换集群" in result["message"]
