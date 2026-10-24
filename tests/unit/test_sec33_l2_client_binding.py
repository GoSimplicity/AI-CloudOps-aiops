"""SEC-33 第二层 P1-1 / SEC-46（L3 P3-A）：K8s 工具客户端按配置来源分开缓存，缓存客户端不能绕过写门禁的 config_path 限制。

SEC-46 起不再「绑定首个来源」：默认来源客户端单独保存（读写），白名单目录内的自定义 kubeconfig 按 realpath
缓存且只读。原 L2 断言「切换来源即拒绝」改为断言「每个请求只落在它自己的来源上，写/Secret 读只落在 default」。

全部离线：kubeconfig 加载被替换为把合成 host 写入 Configuration 的假实现，所有 K8s API 方法在类上替换为
只记录「方法名 + 实际使用的 host」的记录器（不替换 _initialize_clients，真实走工具的初始化与缓存逻辑）。
"""

from unittest.mock import Mock

from kubernetes import client, config as kube_config
import pytest
import yaml

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
    allowed = tmp_path / "kubeconfigs"
    allowed.mkdir()
    monkeypatch.setattr(config.mcp, "kubeconfig_allowed_dir", str(allowed))
    alt = allowed / "alt-kubeconfig"
    alt.write_text(yaml.safe_dump({"clusters": [{"name": "c", "cluster": {"server": ALT_HOST}}], "users": [{"name": "u", "user": {"token": "t"}}]}))
    alt_link = allowed / "alt-link"
    alt_link.symlink_to(alt)
    monkeypatch.setattr(client.Configuration, "_default", None)
    loads = []

    def fake_load_kube_config(config_file=None, context=None, client_configuration=None, persist_config=True, temp_file_path=None):
        # SEC-46：按路径加载只用于默认来源（~/.kube/config）
        assert config_file is None
        loads.append(("default", client_configuration is None))
        client_configuration.host = DEFAULT_HOST

    def fake_load_from_dict(config_dict, context=None, client_configuration=None, persist_config=True, temp_file_path=None):
        loads.append((config_dict["clusters"][0]["cluster"]["server"], client_configuration is None))
        client_configuration.host = config_dict["clusters"][0]["cluster"]["server"]

    monkeypatch.setattr(kube_config, "load_kube_config", fake_load_kube_config)
    monkeypatch.setattr(kube_config, "load_kube_config_from_dict", fake_load_from_dict)
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


async def test_list_pods_with_alt_config_then_delete_pod_without_config_goes_to_default_only(kube, gates, server):
    gates(write=True, repair=True, allowed=["staging"])
    await server.execute_tool("k8s_pod_management", {"operation": "list_pods", "namespace": "staging", "config_path": kube.alt})
    assert ("list_namespaced_pod", ALT_HOST) in kube.calls
    await server.execute_tool("k8s_pod_management", {"operation": "delete_pod", "namespace": "staging", "pod_name": "web-0"})
    assert _hosts(kube.calls, "delete_") == [DEFAULT_HOST]


async def test_list_configmaps_with_alt_config_then_get_secret_never_reads_alt_secret(kube, gates, server):
    gates(secret=True, allowed=["staging"])
    await server.execute_tool("k8s_config_management", {"operation": "list_configmaps", "namespace": "staging", "config_path": kube.alt})
    assert ("list_namespaced_config_map", ALT_HOST) in kube.calls
    await server.execute_tool("k8s_config_management", {"operation": "get_secret", "namespace": "staging", "resource_name": "db"})
    secret_hosts = [host for name, host in kube.calls if "secret" in name]
    assert secret_hosts and set(secret_hosts) == {DEFAULT_HOST}


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
async def test_every_k8s_tool_keeps_alt_and_default_sources_apart(kube, gates, server, tool, params):
    await server.execute_tool(tool, {**params, "config_path": kube.alt})
    first = list(kube.calls)
    assert first and set(_hosts(first)) == {ALT_HOST}
    # 独立 Configuration：不修改 kubernetes 全局默认配置
    assert client.Configuration.get_default_copy().host != ALT_HOST
    assert all(not global_load for _, global_load in kube.loads)
    await server.execute_tool(tool, dict(params))
    later = kube.calls[len(first) :]
    assert later and set(_hosts(later)) == {DEFAULT_HOST}, "未带 config_path 的请求绝不能复用备用配置的客户端"


@pytest.mark.parametrize("tool,params", READS, ids=[r[0] for r in READS])
async def test_default_first_then_alt_read_uses_alt(kube, gates, server, tool, params):
    await server.execute_tool(tool, dict(params))
    assert kube.calls and set(_hosts(kube.calls)) == {DEFAULT_HOST}
    before = len(kube.calls)
    await server.execute_tool(tool, {**params, "config_path": kube.alt})
    assert kube.calls[before:] and set(_hosts(kube.calls[before:])) == {ALT_HOST}


async def test_same_source_via_symlink_reuses_clients(kube, gates, server):
    params = {"operation": "list_pods", "namespace": "staging"}
    await server.execute_tool("k8s_pod_management", {**params, "config_path": kube.alt})
    await server.execute_tool("k8s_pod_management", {**params, "config_path": kube.alt_link})
    assert _hosts(kube.calls) == [ALT_HOST, ALT_HOST]
    assert len(kube.loads) == 1


@pytest.mark.parametrize("tool,params", READS[:3], ids=[r[0] for r in READS[:3]])
async def test_missing_explicit_config_never_falls_back_to_default(kube, gates, server, tool, params, tmp_path):
    result = await server.execute_tool(tool, {**params, "config_path": str(tmp_path / "kubeconfigs" / "missing-kubeconfig")})
    assert kube.calls == []
    assert result["success"] is False and "config_path 不可用" in result["message"]
    # 之后默认配置仍可正常使用
    await server.execute_tool(tool, dict(params))
    assert set(_hosts(kube.calls)) == {DEFAULT_HOST}


async def test_resource_tool_alt_read_then_default_label_and_apply_client_is_independent(kube, gates, server):
    gates(write=True, repair=True, allowed=["staging"])
    await server.execute_tool(
        "k8s_resource_operations",
        {"operation": "describe_resource", "resource_type": "pod", "resource_name": "web-0", "namespace": "staging", "config_path": kube.alt},
    )
    assert kube.dynamic_hosts == [ALT_HOST]
    before = len(kube.calls)
    await server.execute_tool(
        "k8s_resource_operations",
        {"operation": "add_labels", "resource_type": "pod", "resource_name": "web-0", "namespace": "staging", "labels": {"a": "b"}},
    )
    assert kube.dynamic_hosts == [ALT_HOST, DEFAULT_HOST]
    assert ALT_HOST not in _hosts(kube.calls[before:])


async def test_cleanup_releases_all_sources_so_tool_can_reinitialize(kube, gates, server):
    params = {"operation": "list_pods", "namespace": "staging"}
    await server.execute_tool("k8s_pod_management", {**params, "config_path": kube.alt})
    tool = server.tools["k8s_pod_management"]
    await tool.cleanup()
    assert tool._api_clients is None and tool._custom_clients == {}
    await server.execute_tool("k8s_pod_management", dict(params))
    assert _hosts(kube.calls) == [ALT_HOST, DEFAULT_HOST]


# ---------------------------------------------------------------- SEC-33 L3 P3-K：评审变异 #5 / #6 的正式回归测试


def test_incluster_load_uses_private_configuration(monkeypatch, tmp_path):
    """变异 #5：in-cluster 分支去掉 client_configuration 会改写 kubernetes 全局默认配置"""
    from app.mcp.server.tools.k8s_pod_tool import K8sPodTool

    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))  # 没有 ~/.kube/config → 走 in-cluster
    monkeypatch.setattr(client.Configuration, "_default", None)
    seen = []

    def fake_incluster(client_configuration=None, try_refresh_token=True):
        seen.append(client_configuration)
        target = client_configuration if client_configuration is not None else client.Configuration()
        target.host = "https://incluster.invalid"
        if client_configuration is None:
            client.Configuration.set_default(target)

    def must_not_load_file(*args, **kwargs):
        raise AssertionError("no ~/.kube/config: file loader must not run")

    monkeypatch.setattr(kube_config, "load_incluster_config", fake_incluster)
    monkeypatch.setattr(kube_config, "load_kube_config", must_not_load_file)
    api_client = K8sPodTool()._new_api_client(None)
    assert len(seen) == 1 and isinstance(seen[0], client.Configuration)
    assert api_client.configuration is seen[0]
    assert api_client.configuration.host == "https://incluster.invalid"
    assert client.Configuration.get_default_copy().host != "https://incluster.invalid"


def test_concurrent_default_initialization_loads_once(monkeypatch):
    """变异 #6：去掉 _client_source_lock 后并发首个请求会各自加载、各自建客户端"""
    import threading
    import time

    from app.mcp.server.tools.k8s_pod_tool import K8sPodTool

    tool = K8sPodTool()
    loads = []
    barrier = threading.Barrier(8)

    def slow_new_api_client(config_path):
        loads.append(config_path)
        time.sleep(0.05)
        return Mock(name="api-client")

    monkeypatch.setattr(tool, "_new_api_client", slow_new_api_client)
    monkeypatch.setattr(tool, "_create_api_clients", lambda api_client: {"v1": api_client})
    results = []

    def worker():
        barrier.wait()
        results.append(tool._initialize_clients(None))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert loads == [None]
    assert len(results) == 8 and all(result is results[0] for result in results)
