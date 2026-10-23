"""SEC-46（P1）/ SEC-33 L3 P3-A、P3-G：config_path 白名单目录、exec/auth-provider 拒绝、自定义来源只读缓存。

攻击链：upload_knowledge_file 上传含 exec 插件的 kubeconfig（.txt，落在 platform 与 mcp 共享的 data/knowledge_base），
再让读工具 config_path 指向它 → MCP 主机执行命令。

全部离线：
- 默认 kubeconfig 加载替换为把合成 host 写进 Configuration 的假实现（断言不传 config_file）；
- 自定义 kubeconfig 是真实 YAML（server 为 *.invalid），由真实的 kubernetes 加载器解析；
- 所有 K8s API 方法在类上替换为只记录「方法名 + host」的记录器；
- subprocess.Popen / os.execv* / os.system / os.posix_spawn* 全部替换为记录后抛错的探针，每个用例结束断言从未被调用。
exec 插件 command 指向不存在的无害路径，即便探针失效也不会执行任何真实程序。
"""

import os
from pathlib import Path
import subprocess

from kubernetes import client, config as kube_config
import pytest
import yaml

from app.config.settings import config
from app.mcp.server.mcp_server import MCPServer
from app.mcp.server.tools import create_tools, k8s_resource_tool

DEFAULT_HOST = "https://default-cluster.invalid"
ALT_HOST = "https://alt-cluster.invalid"
OTHER_HOST = "https://other-cluster.invalid"
HARMLESS_COMMAND = "/nonexistent/sec46-probe-must-never-run"
API_CLASSES = (client.CoreV1Api, client.AppsV1Api, client.NetworkingV1Api, client.AutoscalingV1Api, client.AutoscalingV2Api, client.VersionApi)
API_PREFIXES = ("list_", "read_", "delete_", "patch_", "create_", "replace_", "get_code")
UNIFIED = "config_path 不可用"

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
# k8s_resource_monitor 的 _create_api_clients 引用不存在的 MetricsV1beta1Api（已登记的独立缺陷），
# 只能覆盖「拒绝发生在创建客户端之前」的路径
ALL_CONFIG_PATH_TOOLS = READS + [("k8s_resource_monitor", {"operation": "get_node_metrics"})]
IDS = [r[0] for r in ALL_CONFIG_PATH_TOOLS]
READ_IDS = [r[0] for r in READS]

WRITES = [
    ("k8s_pod_management", {"operation": "delete_pod", "namespace": "staging", "pod_name": "web-0"}),
    ("k8s_deployment_management", {"operation": "restart_deployment", "namespace": "staging", "deployment_name": "web"}),
    ("k8s_application_scaling", {"operation": "scale_deployment", "namespace": "staging", "resource_name": "web", "replicas": 2}),
    ("k8s_config_management", {"operation": "delete_configmap", "namespace": "staging", "resource_name": "x"}),
    (
        "k8s_resource_operations",
        {"operation": "add_labels", "resource_type": "pod", "resource_name": "web-0", "namespace": "staging", "labels": {"a": "b"}},
    ),
    (
        "k8s_resource_operations",
        {"operation": "apply_yaml", "namespace": "staging", "yaml_content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n  namespace: staging\n"},
    ),
]
WRITE_IDS = [f"{t}-{p['operation']}" for t, p in WRITES]


def kubeconfig(server, user):
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "c", "cluster": {"server": server}}],
        "users": [{"name": "u", "user": user}],
        "contexts": [{"name": "ctx", "context": {"cluster": "c", "user": "u"}}],
        "current-context": "ctx",
    }


EXEC_USER = {"exec": {"apiVersion": "client.authentication.k8s.io/v1beta1", "command": HARMLESS_COMMAND, "args": ["pwned"], "interactiveMode": "Never"}}
AUTH_PROVIDER_USER = {"auth-provider": {"name": "gcp", "config": {"cmd-path": HARMLESS_COMMAND, "cmd-args": "pwned", "token-key": "{.token}", "expiry-key": "{.expiry}"}}}
TOKEN_USER = {"token": "synthetic-offline-token"}


def write_kubeconfig(path, server=ALT_HOST, user=None, extra_users=()):
    doc = kubeconfig(server, user or TOKEN_USER)
    doc["users"].extend(extra_users)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return str(path)


@pytest.fixture
def probes(monkeypatch):
    """任何进程创建都记录并抛错；用例结束断言为空"""
    spawned = []

    def probe(name):
        def boom(*args, **kwargs):
            spawned.append((name, args))
            raise AssertionError(f"{name} must never be called")

        return boom

    monkeypatch.setattr(subprocess, "Popen", probe("subprocess.Popen"))
    for name in ("system", "execv", "execve", "execvp", "execvpe", "posix_spawn", "posix_spawnp", "fork"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, probe(f"os.{name}"))
    yield spawned
    assert spawned == [], "kubeconfig 处理过程中绝不能创建进程"


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
def kube(monkeypatch, tmp_path, probes):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    (home / ".kube").mkdir(parents=True)
    (home / ".kube" / "config").write_text("synthetic-default-kubeconfig\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.delenv("MCP_KUBECONFIG_ALLOWED_DIR", raising=False)
    # 与生产一致：知识库是 <cwd>/data/knowledge_base，platform 与 mcp 共享 ./data
    knowledge = tmp_path / "data" / "knowledge_base"
    knowledge.mkdir(parents=True)
    monkeypatch.setattr(config.rag, "knowledge_base_path", "data/knowledge_base")
    allowed = tmp_path / "ops-kubeconfigs"
    allowed.mkdir()
    monkeypatch.setattr(config.mcp, "kubeconfig_allowed_dir", "", raising=False)
    monkeypatch.setattr(client.Configuration, "_default", None)
    default_loads = []
    real_load_kube_config = kube_config.load_kube_config

    def fake_default_load(config_file=None, context=None, client_configuration=None, persist_config=True, temp_file_path=None):
        # 默认来源（config_file=None）替换为合成 host；显式文件交给真实加载器，
        # 这样旧代码上攻击链会真实走到 exec 插件 → 被 Popen 探针拦截并记录（证明 P1 存在）
        if config_file is not None:
            return real_load_kube_config(config_file=config_file, context=context, client_configuration=client_configuration, persist_config=False)
        default_loads.append(config_file)
        target = client_configuration if client_configuration is not None else client.Configuration()
        target.host = DEFAULT_HOST

    monkeypatch.setattr(kube_config, "load_kube_config", fake_default_load)
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

    class FakeDynamic:
        def __init__(self, api_client):
            dynamic_hosts.append(api_client.configuration.host)
            self.api_client = api_client

        @property
        def resources(self):
            raise RuntimeError("synthetic offline dynamic api")

    monkeypatch.setattr(k8s_resource_tool, "BoundedDynamicClient", FakeDynamic)

    class Env:
        pass

    env = Env()
    env.tmp, env.knowledge, env.allowed = tmp_path, knowledge, allowed
    env.calls, env.default_loads, env.dynamic_hosts, env.spawned = calls, default_loads, dynamic_hosts, probes
    env.alt = write_kubeconfig(allowed / "alt.yaml")
    env.other = write_kubeconfig(allowed / "other.yaml", server=OTHER_HOST)
    # 攻击载荷：上传到知识库的 .txt
    env.uploaded = write_kubeconfig(knowledge / "evil.txt", server=ALT_HOST, user=EXEC_USER)

    def allow(path):
        monkeypatch.setattr(config.mcp, "kubeconfig_allowed_dir", str(path), raising=False)

    env.allow = allow
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


async def _rejected(server, kube, tool, params, config_path):
    result = await server.execute_tool(tool, {**params, "config_path": config_path})
    assert result["success"] is False
    assert UNIFIED in result["message"], result
    assert kube.calls == [] and kube.dynamic_hosts == []
    return result


# ---------- 攻击链本身 ----------


@pytest.mark.parametrize("tool,params", ALL_CONFIG_PATH_TOOLS, ids=IDS)
async def test_uploaded_knowledge_file_with_exec_plugin_is_rejected_and_never_executes(kube, gates, server, tool, params):
    await _rejected(server, kube, tool, params, kube.uploaded)
    assert kube.spawned == []


@pytest.mark.parametrize("relative", ["data/knowledge_base/evil.txt", "./data/../data/knowledge_base/evil.txt"])
async def test_uploaded_knowledge_file_relative_path_is_rejected(kube, gates, server, relative):
    await _rejected(server, kube, *READS[0], relative)


async def test_uploaded_file_rejected_even_when_allowlist_points_at_knowledge_base(kube, gates, server):
    safe = write_kubeconfig(kube.knowledge / "safe.yaml")
    kube.allow(kube.knowledge)
    await _rejected(server, kube, *READS[0], kube.uploaded)
    await _rejected(server, kube, *READS[0], safe)


@pytest.mark.parametrize("which", ["data", "data_sub", "contains_data", "knowledge_sub", "root"])
async def test_allowlist_overlapping_data_or_knowledge_base_is_invalid(kube, gates, server, which):
    targets = {
        "data": kube.tmp / "data",
        "data_sub": kube.tmp / "data" / "kubeconfigs",
        "contains_data": kube.tmp,
        "knowledge_sub": kube.knowledge / "nested",
        "root": Path("/"),
    }
    allowed = targets[which]
    allowed.mkdir(parents=True, exist_ok=True)
    safe = write_kubeconfig(allowed / "safe-kubeconfig.yaml") if which != "root" else kube.alt
    kube.allow(allowed)
    await _rejected(server, kube, *READS[0], safe)


async def test_allowlist_symlink_into_data_dir_is_invalid(kube, gates, server):
    real = kube.tmp / "data" / "kubeconfigs"
    real.mkdir()
    safe = write_kubeconfig(real / "safe.yaml")
    link = kube.tmp / "innocent-looking-dir"
    link.symlink_to(real, target_is_directory=True)
    kube.allow(link)
    await _rejected(server, kube, *READS[0], str(link / "safe.yaml"))
    await _rejected(server, kube, *READS[0], safe)


# ---------- 白名单为空（出厂默认） ----------


@pytest.mark.parametrize("tool,params", ALL_CONFIG_PATH_TOOLS, ids=IDS)
async def test_empty_allowlist_rejects_any_config_path(kube, gates, server, tool, params):
    await _rejected(server, kube, tool, params, kube.alt)
    assert kube.default_loads == []


@pytest.mark.parametrize("value", ["   ", "relative-dir-that-does-not-exist", "/definitely/missing/dir"])
async def test_blank_or_missing_allowlist_dir_disables_custom_paths(kube, gates, server, value):
    kube.allow(value)
    await _rejected(server, kube, *READS[0], kube.alt)


def test_allowlist_default_is_empty():
    from app.config.settings import MCPConfig

    assert MCPConfig().kubeconfig_allowed_dir == ""


def test_allowlist_env_override(monkeypatch):
    from app.config.settings import MCPConfig

    monkeypatch.setenv("MCP_KUBECONFIG_ALLOWED_DIR", "/etc/aiops/kubeconfigs")
    assert MCPConfig().kubeconfig_allowed_dir == "/etc/aiops/kubeconfigs"


# ---------- exec / auth-provider ----------


@pytest.mark.parametrize(
    "user,extra",
    [
        (EXEC_USER, ()),
        (AUTH_PROVIDER_USER, ()),
        (TOKEN_USER, ({"name": "second", "user": EXEC_USER},)),
        (TOKEN_USER, ({"name": "second", "user": AUTH_PROVIDER_USER},)),
        ({"token": "t", "exec": {"command": HARMLESS_COMMAND}}, ()),
        ({"auth-provider": {"name": "oidc", "config": {}}}, ()),
    ],
    ids=["exec", "auth-provider-gcp-cmd-path", "exec-in-unused-user", "auth-provider-in-unused-user", "exec-next-to-token", "auth-provider-oidc"],
)
@pytest.mark.parametrize("tool,params", ALL_CONFIG_PATH_TOOLS, ids=IDS)
async def test_allowlisted_kubeconfig_with_credential_plugin_is_rejected_before_loading(kube, gates, server, tool, params, user, extra):
    kube.allow(kube.allowed)
    path = write_kubeconfig(kube.allowed / "plugin.yaml", user=user, extra_users=extra)
    await _rejected(server, kube, tool, params, path)
    assert kube.spawned == []


@pytest.mark.parametrize(
    "content",
    [
        "users: not-a-list\n",
        "users:\n- just-a-string\n",
        "users:\n- name: u\n  user: [exec]\n",
        "- a\n- b\n",
        "{not: [valid yaml\n",
        "",
    ],
    ids=["users-not-list", "user-entry-not-mapping", "user-not-mapping", "top-level-list", "invalid-yaml", "empty"],
)
async def test_malformed_kubeconfig_is_rejected_with_unified_message(kube, gates, server, content):
    kube.allow(kube.allowed)
    path = kube.allowed / "malformed.yaml"
    path.write_text(content, encoding="utf-8")
    await _rejected(server, kube, *READS[0], str(path))


# ---------- 路径约束 ----------


async def test_symlink_inside_allowlist_pointing_outside_is_rejected(kube, gates, server):
    kube.allow(kube.allowed)
    outside = write_kubeconfig(kube.tmp / "outside" / "safe.yaml")
    (kube.allowed / "to-outside.yaml").symlink_to(outside)
    (kube.allowed / "to-upload.yaml").symlink_to(kube.uploaded)
    await _rejected(server, kube, *READS[0], str(kube.allowed / "to-outside.yaml"))
    await _rejected(server, kube, *READS[0], str(kube.allowed / "to-upload.yaml"))
    assert kube.spawned == []


async def test_symlink_inside_allowlist_pointing_inside_is_allowed(kube, gates, server):
    kube.allow(kube.allowed)
    (kube.allowed / "alias.yaml").symlink_to(kube.alt)
    await server.execute_tool(*READS[0][:1], {**READS[0][1], "config_path": str(kube.allowed / "alias.yaml")})
    assert _hosts(kube.calls) == [ALT_HOST]


@pytest.mark.parametrize("value", ["../data/knowledge_base/evil.txt", "/etc/passwd", "missing.yaml", ".", "", 0, False, ["alt.yaml"]])
async def test_traversal_missing_and_non_string_paths_are_rejected(kube, gates, server, value):
    kube.allow(kube.allowed)
    params = {**READS[0][1], "config_path": value}
    result = await server.execute_tool(READS[0][0], params)
    if value == "":
        # 空串与缺省一致：走默认来源
        assert _hosts(kube.calls) == [DEFAULT_HOST]
        return
    assert result["success"] is False and UNIFIED in result["message"]
    assert kube.calls == []


async def test_relative_path_resolves_inside_allowlist(kube, gates, server):
    kube.allow(kube.allowed)
    await server.execute_tool(READS[0][0], {**READS[0][1], "config_path": "alt.yaml"})
    assert _hosts(kube.calls) == [ALT_HOST]


async def test_fifo_and_directory_in_allowlist_are_rejected_without_hanging(kube, gates, server):
    kube.allow(kube.allowed)
    fifo = kube.allowed / "pipe.yaml"
    os.mkfifo(fifo)
    (kube.allowed / "dir.yaml").mkdir()
    await _rejected(server, kube, *READS[0], str(fifo))
    await _rejected(server, kube, *READS[0], str(kube.allowed / "dir.yaml"))


async def test_oversized_and_hardlinked_kubeconfig_rejected(kube, gates, server):
    kube.allow(kube.allowed)
    big = kube.allowed / "big.yaml"
    big.write_text(Path(kube.alt).read_text() + "#" * (1024 * 1024 + 1))
    await _rejected(server, kube, *READS[0], str(big))
    os.link(kube.uploaded, kube.allowed / "hardlink.yaml")
    await _rejected(server, kube, *READS[0], str(kube.allowed / "hardlink.yaml"))
    assert kube.spawned == []


async def test_all_rejection_reasons_share_one_message(kube, gates, server):
    messages = set()
    result = await server.execute_tool(READS[0][0], {**READS[0][1], "config_path": kube.alt})
    messages.add(result["message"])
    kube.allow(kube.allowed)
    for path in (kube.uploaded, str(kube.allowed / "missing.yaml"), write_kubeconfig(kube.allowed / "e.yaml", user=EXEC_USER)):
        result = await server.execute_tool(READS[0][0], {**READS[0][1], "config_path": path})
        messages.add(result["message"])
    bad = kube.allowed / "bad.yaml"
    bad.write_text(yaml.safe_dump(kubeconfig(ALT_HOST, TOKEN_USER) | {"current-context": "nope"}))
    result = await server.execute_tool(READS[0][0], {**READS[0][1], "config_path": str(bad)})
    messages.add(result["message"])
    assert len(messages) == 1 and UNIFIED in messages.pop()
    assert kube.calls == []


# ---------- 自定义来源只读、写只走 default ----------


@pytest.mark.parametrize("tool,params", READS, ids=READ_IDS)
async def test_allowlisted_custom_source_reads_from_its_own_cluster_without_global_side_effects(kube, gates, server, tool, params):
    kube.allow(kube.allowed)
    await server.execute_tool(tool, {**params, "config_path": kube.alt})
    assert kube.calls and set(_hosts(kube.calls)) == {ALT_HOST}
    assert client.Configuration.get_default_copy().host != ALT_HOST
    assert kube.default_loads == []
    # 默认来源随后照常可用，不再「绑定首个来源」
    await server.execute_tool(tool, dict(params))
    assert DEFAULT_HOST in _hosts(kube.calls)
    # 再切回自定义来源也可以，且走缓存不重复加载
    count = len(kube.calls)
    await server.execute_tool(tool, {**params, "config_path": kube.alt})
    assert set(_hosts(kube.calls[count:])) == {ALT_HOST}
    assert kube.default_loads == [None]


async def test_custom_sources_are_cached_per_realpath(kube, gates, server, monkeypatch):
    from app.mcp.server.tools import k8s_base_tool

    kube.allow(kube.allowed)
    (kube.allowed / "alias.yaml").symlink_to(kube.alt)
    loads = []
    real = k8s_base_tool.config.load_kube_config_from_dict

    def counting(config_dict, *args, **kwargs):
        loads.append(config_dict["clusters"][0]["cluster"]["server"])
        return real(config_dict, *args, **kwargs)

    monkeypatch.setattr(k8s_base_tool.config, "load_kube_config_from_dict", counting)
    tool, params = READS[0]
    for path in (kube.alt, str(kube.allowed / "alias.yaml"), "alt.yaml", kube.other, kube.alt):
        await server.execute_tool(tool, {**params, "config_path": path})
    assert _hosts(kube.calls) == [ALT_HOST, ALT_HOST, ALT_HOST, OTHER_HOST, ALT_HOST]
    assert loads == [ALT_HOST, OTHER_HOST]


@pytest.mark.parametrize("tool,params", WRITES, ids=WRITE_IDS)
async def test_read_via_custom_source_then_default_write_goes_to_default(kube, gates, server, tool, params):
    gates(write=True, repair=True, allowed=["staging"])
    kube.allow(kube.allowed)
    read = next(p for t, p in READS if t == tool)
    await server.execute_tool(tool, {**read, "config_path": kube.alt})
    assert kube.calls and set(_hosts(kube.calls)) == {ALT_HOST}
    before_calls, before_dynamic = len(kube.calls), len(kube.dynamic_hosts)
    result = await server.execute_tool(tool, dict(params))
    assert "切换集群" not in str(result)
    # 写请求产生的任何 K8s 调用（含写前读取）都只能落在 default；绝不能复用自定义来源的客户端
    new_hosts = _hosts(kube.calls[before_calls:]) + kube.dynamic_hosts[before_dynamic:]
    assert new_hosts and set(new_hosts) == {DEFAULT_HOST}, (result, kube.calls)


@pytest.mark.parametrize("tool,params", WRITES, ids=WRITE_IDS)
async def test_write_with_config_path_is_rejected_at_tool_layer_even_without_gate(kube, gates, server, tool, params):
    """纵深：绕过 MCPServer 门禁直接调 tool.execute，写操作带任何 config_path 也只能拒绝"""
    gates(write=True, repair=True, allowed=["staging"])
    kube.allow(kube.allowed)
    result = await server.tools[tool].execute({**params, "config_path": kube.alt})
    assert result["success"] is False
    assert kube.calls == [] and kube.dynamic_hosts == []


async def test_secret_read_with_config_path_is_rejected_at_tool_layer(kube, gates, server):
    gates(secret=True, allowed=["staging"])
    kube.allow(kube.allowed)
    result = await server.tools["k8s_config_management"].execute({"operation": "get_secret", "namespace": "staging", "resource_name": "db", "config_path": kube.alt})
    assert result["success"] is False
    assert kube.calls == []


async def test_cleanup_closes_default_and_custom_clients(kube, gates, server):
    kube.allow(kube.allowed)
    tool_name, params = READS[0]
    await server.execute_tool(tool_name, {**params, "config_path": kube.alt})
    await server.execute_tool(tool_name, dict(params))
    tool = server.tools[tool_name]
    closed = []
    for clients in [tool._api_clients, *tool._custom_clients.values()]:
        api_client = clients["v1"].api_client
        original = api_client.close
        api_client.close = lambda original=original, host=api_client.configuration.host: (closed.append(host), original())
    await tool.cleanup()
    assert sorted(closed) == sorted([DEFAULT_HOST, ALT_HOST])
    assert tool._api_clients is None and tool._custom_clients == {}


# ---------- 变异测试补洞（存活变异 R2 / D3 / E3 / E4 / L3 / T2） ----------


async def test_allowlist_given_as_symlink_to_dedicated_dir_still_works(kube, gates, server):
    """R2：白名单目录按 realpath 解析；配置成指向专用目录的符号链接时，目录内文件照常可用"""
    link = kube.tmp / "kubeconfigs-link"
    link.symlink_to(kube.allowed, target_is_directory=True)
    kube.allow(link)
    await server.execute_tool(READS[0][0], {**READS[0][1], "config_path": str(link / "alt.yaml")})
    await server.execute_tool(READS[0][0], {**READS[0][1], "config_path": "other.yaml"})
    assert _hosts(kube.calls) == [ALT_HOST, OTHER_HOST]


@pytest.mark.parametrize("which", ["knowledge", "contains_knowledge"])
async def test_knowledge_base_outside_data_is_also_forbidden(kube, gates, server, monkeypatch, which):
    """D3：知识库目录不在 data/ 下（rag.knowledge_base_path 指向别处）时同样不能作为白名单"""
    knowledge = kube.tmp / "elsewhere" / "kb"
    knowledge.mkdir(parents=True)
    monkeypatch.setattr(config.rag, "knowledge_base_path", str(knowledge))
    safe = write_kubeconfig(knowledge / "safe.yaml")
    sibling = write_kubeconfig(kube.tmp / "elsewhere" / "sibling.yaml")
    kube.allow(knowledge if which == "knowledge" else knowledge.parent)
    await _rejected(server, kube, *READS[0], safe)
    await _rejected(server, kube, *READS[0], sibling)


@pytest.mark.parametrize("key", ["Exec", " exec", "EXEC ", "Auth-Provider", " auth-provider\t"])
async def test_credential_plugin_keys_are_matched_case_and_space_insensitively(kube, gates, server, key):
    """E3：键名 strip + 小写后比较（fail-closed，即使加载器只认小写键）"""
    kube.allow(kube.allowed)
    path = write_kubeconfig(kube.allowed / "variant.yaml", user={"token": "t", key: {"command": HARMLESS_COMMAND}})
    await _rejected(server, kube, *READS[0], path)


@pytest.mark.parametrize(
    "content",
    [
        yaml.safe_dump({"users": {"u": {"exec": {"command": HARMLESS_COMMAND}}}}),
        yaml.safe_dump(kubeconfig(ALT_HOST, TOKEN_USER) | {"users": {"name": "u", "user": TOKEN_USER}}),
        "users:\n- name: u\n  user: [exec]\n",
        "- a\n",
    ],
    ids=["users-mapping-with-exec", "users-mapping", "user-not-mapping", "top-level-list"],
)
async def test_malformed_kubeconfig_is_rejected_before_reaching_the_loader(kube, gates, server, monkeypatch, content):
    """E4：结构异常在解析阶段就拒绝，绝不交给 kubernetes 加载器"""
    from app.mcp.server.tools import k8s_base_tool

    loaded = []

    def recorder(*args, **kwargs):
        loaded.append(args)
        raise AssertionError("loader must not be reached")

    monkeypatch.setattr(k8s_base_tool.config, "load_kube_config_from_dict", recorder)
    kube.allow(kube.allowed)
    path = kube.allowed / "malformed.yaml"
    path.write_text(content, encoding="utf-8")
    await _rejected(server, kube, *READS[0], str(path))
    assert loaded == []


async def test_hardlinked_safe_kubeconfig_is_rejected(kube, gates, server):
    """L3：白名单内的硬链接（即使内容是无害的 token kubeconfig）也拒绝，防止借硬链接把白名单外文件带进来"""
    kube.allow(kube.allowed)
    outside = write_kubeconfig(kube.tmp / "outside" / "safe.yaml")
    os.link(outside, kube.allowed / "hardlink.yaml")
    await _rejected(server, kube, *READS[0], str(kube.allowed / "hardlink.yaml"))


@pytest.mark.parametrize("tool,params", WRITES, ids=WRITE_IDS)
async def test_tool_layer_config_path_rejection_uses_unified_message(kube, gates, server, tool, params):
    """T2：工具层（绕过门禁）拒绝写/Secret 读的 config_path 时也用统一文案"""
    gates(write=True, secret=True, repair=True, allowed=["staging"])
    kube.allow(kube.allowed)
    result = await server.tools[tool].execute({**params, "config_path": kube.alt})
    assert result["success"] is False and UNIFIED in str(result.get("message", result)), result
    secret = await server.tools["k8s_config_management"].execute({"operation": "get_secret", "namespace": "staging", "resource_name": "db", "config_path": kube.alt})
    assert secret["success"] is False and UNIFIED in secret["message"], secret
