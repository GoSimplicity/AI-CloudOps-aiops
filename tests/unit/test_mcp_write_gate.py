"""SEC-33：MCP 写工具门禁、Secret 读取开关、config_path 拒绝与拒绝契约（全部离线，K8s 客户端为计数 mock）"""

import base64
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from kubernetes import client as k8s_client
import pytest
import yaml

from app.common.exceptions import FeatureDisabledError
from app.config.settings import MCPConfig, config
from app.mcp import main
from app.mcp.server import write_gate
from app.mcp.server.mcp_server import MCPServer
from app.mcp.server.tools import create_tools

MARKER = "sec33-private-VALUE-90417"
WRITE_PREFIXES = ("create_", "delete_", "patch_", "replace_", "server_side_apply")
DOC = Path(__file__).resolve().parents[2] / "docs" / "release" / "SEC-33-mcp-write-gate.md"


def _registered_operations():
    table = {}
    for tool in create_tools():
        enum = (tool.parameters.get("properties", {}).get("operation") or {}).get("enum")
        table[tool.name] = list(enum) if enum else None
    return table


REGISTERED = _registered_operations()
READ_PAIRS = [(tool, op) for tool, ops in REGISTERED.items() if ops for op in ops if op in write_gate.READ_OPERATIONS.get(tool, ())]
SECRET_PAIRS = [(tool, op) for tool, ops in REGISTERED.items() if ops for op in ops if op in write_gate.SECRET_READ_OPERATIONS.get(tool, ())]
WRITE_PAIRS = [(tool, op) for tool, ops in REGISTERED.items() if ops for op in ops if (tool, op) not in READ_PAIRS and (tool, op) not in SECRET_PAIRS]
CLUSTER_TOOLS = write_gate.CLUSTER_SCOPED_WRITE_TOOLS

YAML_TWO = """apiVersion: v1
kind: ConfigMap
metadata:
  name: a
  namespace: staging
data:
  k: v
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: b
  namespace: {second}
data:
  k: v
"""


def write_params(tool, op, namespace="staging", **extra):
    params = {"operation": op, "namespace": namespace, "resource_name": "x", "deployment_name": "x", "pod_name": "x"}
    if tool == "k8s_resource_operations" and op in write_gate.RESOURCE_LABEL_OPERATIONS:
        params["resource_type"] = "pod"
    if op == "apply_yaml":
        params = {"operation": op, "namespace": namespace, "yaml_content": YAML_TWO.format(second=namespace).replace("staging", namespace)}
    params.update(extra)
    return params


@pytest.fixture
def gates(monkeypatch):
    """默认：写关闭、Secret 读关闭、repair 关闭、白名单为空（与出厂默认一致）"""

    def set_gates(write=False, secret=False, repair=False, allowed=()):
        monkeypatch.setattr(config.mcp, "write_tools_enabled", write)
        monkeypatch.setattr(config.mcp, "secret_read_enabled", secret)
        monkeypatch.setattr(config.autofix, "repair_enabled", repair)
        monkeypatch.setattr(config.autofix, "allowed_namespaces", list(allowed))

    set_gates()
    return set_gates


class _Clients(dict):
    """任意 key 都返回同一个记录 mock，用来统计所有 K8s API 调用"""

    def __init__(self, api):
        super().__init__()
        self.api = api

    def __getitem__(self, key):
        return self.api

    def get(self, key, default=None):
        return self.api


def k8s_writes(api):
    return [call for call in api.mock_calls if any(prefix in call[0] for prefix in WRITE_PREFIXES)]


@pytest.fixture
async def server_with_mocks():
    server = MCPServer()
    api = Mock(name="k8s-api")
    init = Mock(name="initialize_clients", return_value=_Clients(api))
    for tool in create_tools():
        if hasattr(tool, "_initialize_clients"):
            tool._initialize_clients = init
        await server.register_tool(tool)
    try:
        yield server, api, init
    finally:
        await server.shutdown()


# ---------- 分类表 ----------


def test_classification_table_covers_every_registered_tool_and_operation():
    assert set(REGISTERED) == set(write_gate.READ_ONLY_TOOLS) | set(write_gate.READ_OPERATIONS)
    for tool, ops in REGISTERED.items():
        if ops is None:
            assert tool in write_gate.READ_ONLY_TOOLS
            continue
        assert tool not in write_gate.READ_ONLY_TOOLS
        listed = set(write_gate.READ_OPERATIONS.get(tool, ())) | set(write_gate.SECRET_READ_OPERATIONS.get(tool, ()))
        assert listed <= set(ops), f"{tool} 分类表含已不存在的 operation"
    # 写 operation 清单（文档里的分类表必须与之一致）
    assert {op for _, op in WRITE_PAIRS} >= {"delete_pod", "restart_pod", "scale", "apply_yaml", "create_namespace", "delete_namespace", "create_secret"}
    assert ("k8s_config_management", "get_secret") in SECRET_PAIRS and ("k8s_config_management", "list_secrets") in SECRET_PAIRS


def test_release_doc_lists_every_write_operation_and_error_code():
    text = DOC.read_text(encoding="utf-8")
    for code in write_gate.ERROR_CODES:
        assert code in text
    for _, op in WRITE_PAIRS + SECRET_PAIRS + READ_PAIRS:
        assert f"`{op}`" in text, op
    for needle in ("MCP_WRITE_TOOLS_ENABLED", "mcp.write_tools_enabled", "MCP_SECRET_READ_ENABLED", "mcp.secret_read_enabled", "SEC-39", "git revert", "MCP_FILE_ROOT", "HTTP 200"):
        assert needle in text, needle


# ---------- 默认关闭：每个写 operation 都被拒，且 K8s 零调用 ----------


@pytest.mark.parametrize(("tool", "op"), WRITE_PAIRS)
async def test_default_off_denies_every_write_operation_with_zero_k8s_calls(gates, server_with_mocks, tool, op):
    server, api, init = server_with_mocks
    with pytest.raises(FeatureDisabledError) as denied:
        await server.execute_tool(tool, write_params(tool, op))
    expected = write_gate.MCP_CLUSTER_SCOPED_DENIED if tool in CLUSTER_TOOLS else write_gate.MCP_WRITE_DISABLED
    assert denied.value.error_code == expected
    assert denied.value.status_code == 403
    init.assert_not_called()
    assert api.mock_calls == [] and k8s_writes(api) == []


@pytest.mark.parametrize(("tool", "op"), [pair for pair in WRITE_PAIRS if pair[0] not in CLUSTER_TOOLS])
async def test_switch_on_but_namespace_not_whitelisted_is_denied(gates, server_with_mocks, tool, op):
    gates(write=True, repair=True, allowed=["staging"])
    server, api, init = server_with_mocks
    with pytest.raises(FeatureDisabledError) as denied:
        await server.execute_tool(tool, write_params(tool, op, namespace="prod"))
    assert denied.value.error_code == write_gate.MCP_NAMESPACE_NOT_ALLOWED
    assert "prod" not in denied.value.message
    init.assert_not_called()
    assert k8s_writes(api) == []


@pytest.mark.parametrize("namespace", ["kube-system", "kube-public"])
def test_restricted_namespace_denied_even_if_whitelisted(gates, namespace):
    gates(write=True, repair=True, allowed=[namespace])
    with pytest.raises(FeatureDisabledError) as denied:
        write_gate.enforce_write_gate("k8s_pod_management", {"operation": "delete_pod", "namespace": namespace})
    assert denied.value.error_code == write_gate.MCP_NAMESPACE_NOT_ALLOWED


@pytest.mark.parametrize("namespace", [None, "", 7, ["staging"]])
def test_undeterminable_namespace_fails_closed(gates, namespace):
    gates(write=True, repair=True, allowed=["staging"])
    with pytest.raises(FeatureDisabledError) as denied:
        write_gate.enforce_write_gate("k8s_pod_management", {"operation": "delete_pod", "namespace": namespace})
    assert denied.value.error_code == write_gate.MCP_NAMESPACE_NOT_ALLOWED


def test_missing_namespace_defaults_to_default_and_must_be_whitelisted(gates):
    gates(write=True, repair=True, allowed=["staging"])
    with pytest.raises(FeatureDisabledError):
        write_gate.enforce_write_gate("k8s_pod_management", {"operation": "delete_pod"})
    gates(write=True, repair=True, allowed=["default"])
    write_gate.enforce_write_gate("k8s_pod_management", {"operation": "delete_pod"})


@pytest.mark.parametrize(("tool", "op"), [pair for pair in WRITE_PAIRS if pair[0] not in CLUSTER_TOOLS])
async def test_repair_disabled_denies_writes_even_with_switch_and_whitelist(gates, server_with_mocks, tool, op):
    gates(write=True, repair=False, allowed=["staging"])
    server, api, init = server_with_mocks
    with pytest.raises(FeatureDisabledError) as denied:
        await server.execute_tool(tool, write_params(tool, op))
    assert denied.value.error_code == write_gate.MCP_WRITE_DISABLED
    assert denied.value.details == {"switch": "autofix.repair_enabled"}
    init.assert_not_called()
    assert k8s_writes(api) == []


@pytest.mark.parametrize(("tool", "op"), [pair for pair in WRITE_PAIRS if pair[0] not in CLUSTER_TOOLS])
def test_all_gates_open_and_whitelisted_namespace_passes(gates, tool, op):
    gates(write=True, repair=True, allowed=["staging"])
    write_gate.enforce_write_gate(tool, write_params(tool, op))


async def test_allowed_write_actually_reaches_k8s_once_gates_open(gates, server_with_mocks):
    gates(write=True, repair=True, allowed=["staging"])
    server, api, init = server_with_mocks
    await server.execute_tool("k8s_pod_management", {"operation": "delete_pod", "namespace": "staging", "pod_name": "p"})
    init.assert_called()
    assert [call[0] for call in k8s_writes(api)] == ["delete_namespaced_pod"]


# ---------- 集群级：始终拒绝 ----------


@pytest.mark.parametrize("resource_type", ["namespace", "node", "pv", "pvc", "clusterrole", "Pod", None])
@pytest.mark.parametrize("op", sorted(write_gate.RESOURCE_LABEL_OPERATIONS))
async def test_patch_namespace_and_patch_node_always_denied(gates, server_with_mocks, resource_type, op):
    gates(write=True, repair=True, allowed=["staging", "default"])
    server, api, init = server_with_mocks
    params = {"operation": op, "namespace": "staging", "resource_type": resource_type, "resource_name": "n", "labels": {"a": "b"}, "annotations": {"a": "b"}, "label_keys": ["a"], "annotation_keys": ["a"]}
    with pytest.raises(FeatureDisabledError) as denied:
        await server.execute_tool("k8s_resource_operations", params)
    assert denied.value.error_code == write_gate.MCP_CLUSTER_SCOPED_DENIED
    init.assert_not_called()
    assert not any(name in call[0] for call in api.mock_calls for name in ("patch_namespace", "patch_node"))
    assert api.mock_calls == []


@pytest.mark.parametrize("op", ["create_namespace", "delete_namespace", "unknown_namespace_op"])
@pytest.mark.parametrize("open_all", [False, True])
async def test_create_and_delete_namespace_always_denied(gates, server_with_mocks, op, open_all):
    if open_all:
        gates(write=True, repair=True, allowed=["staging", "victim"])
    server, api, init = server_with_mocks
    with pytest.raises(FeatureDisabledError) as denied:
        await server.execute_tool("k8s_namespace_management", {"operation": op, "namespace_name": "victim", "namespace": "staging"})
    assert denied.value.error_code == write_gate.MCP_CLUSTER_SCOPED_DENIED
    init.assert_not_called()
    assert api.mock_calls == []


# ---------- 读始终放行 ----------


@pytest.mark.parametrize(("tool", "op"), READ_PAIRS)
def test_reads_always_pass_even_with_every_switch_off(gates, tool, op):
    write_gate.enforce_write_gate(tool, {"operation": op, "namespace": "kube-system", "config_path": "/tmp/x"})


@pytest.mark.parametrize("tool", sorted(write_gate.READ_ONLY_TOOLS))
def test_read_only_tools_always_pass(gates, tool):
    write_gate.enforce_write_gate(tool, {"operation": "delete_pod", "namespace": "kube-system"})


async def test_read_operation_reaches_k8s_with_switches_off(gates, server_with_mocks):
    server, api, init = server_with_mocks
    api.list_namespaced_pod.return_value = SimpleNamespace(items=[])
    await server.execute_tool("k8s_pod_management", {"operation": "list_pods", "namespace": "staging"})
    init.assert_called()
    assert k8s_writes(api) == []


# ---------- 未归类 tool / operation 一律按写 ----------


@pytest.mark.parametrize("op", ["drain_node", "LIST_PODS", " list_pods", "list_pods ", None, 123, ["list_pods"], ""])
async def test_unclassified_operation_is_treated_as_write(gates, server_with_mocks, op):
    server, api, init = server_with_mocks
    params = {"namespace": "staging"} if op is None else {"operation": op, "namespace": "staging"}
    with pytest.raises(FeatureDisabledError) as denied:
        await server.execute_tool("k8s_pod_management", params)
    assert denied.value.error_code == write_gate.MCP_WRITE_DISABLED
    init.assert_not_called()
    assert api.mock_calls == []


async def test_unclassified_tool_is_treated_as_write(gates):
    server = MCPServer()
    execute = Mock()

    async def run(parameters):
        execute(parameters)

    await server.register_tool(SimpleNamespace(name="brand_new_tool", execute=run))
    with pytest.raises(FeatureDisabledError) as denied:
        await server.execute_tool("brand_new_tool", {"operation": "list_pods"})
    assert denied.value.error_code == write_gate.MCP_WRITE_DISABLED
    execute.assert_not_called()


def test_non_mapping_parameters_fail_closed(gates):
    assert write_gate.classify("k8s_pod_management", ["list_pods"]).kind == "write"


# ---------- apply_yaml ----------


def test_apply_yaml_default_off_denied(gates):
    with pytest.raises(FeatureDisabledError) as denied:
        write_gate.enforce_write_gate("k8s_resource_operations", {"operation": "apply_yaml", "yaml_content": YAML_TWO.format(second="staging")})
    assert denied.value.error_code == write_gate.MCP_WRITE_DISABLED


def test_apply_yaml_checks_every_document_namespace(gates):
    gates(write=True, repair=True, allowed=["staging"])
    write_gate.enforce_write_gate("k8s_resource_operations", {"operation": "apply_yaml", "yaml_content": YAML_TWO.format(second="staging").replace("name: b", "name: c")})
    with pytest.raises(FeatureDisabledError) as denied:
        write_gate.enforce_write_gate("k8s_resource_operations", {"operation": "apply_yaml", "yaml_content": YAML_TWO.format(second="prod")})
    assert denied.value.error_code == write_gate.MCP_NAMESPACE_NOT_ALLOWED


def test_apply_yaml_document_without_namespace_uses_default(gates):
    gates(write=True, repair=True, allowed=["staging"])
    content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n"
    with pytest.raises(FeatureDisabledError) as denied:
        write_gate.enforce_write_gate("k8s_resource_operations", {"operation": "apply_yaml", "yaml_content": content})
    assert denied.value.error_code == write_gate.MCP_NAMESPACE_NOT_ALLOWED


@pytest.mark.parametrize("kind", ["ClusterRole", "ClusterRoleBinding", "Namespace", "Node", "CustomResourceDefinition"])
def test_apply_yaml_cluster_scoped_kind_always_denied(gates, kind):
    gates(write=True, repair=True, allowed=["staging"])
    content = f"apiVersion: v1\nkind: {kind}\nmetadata:\n  name: a\n  namespace: staging\n"
    with pytest.raises(FeatureDisabledError) as denied:
        write_gate.enforce_write_gate("k8s_resource_operations", {"operation": "apply_yaml", "yaml_content": content})
    assert denied.value.error_code == write_gate.MCP_CLUSTER_SCOPED_DENIED


@pytest.mark.parametrize("content", ["", "::: not yaml :::", "a: &x 1\nb: *x\n", None, "apiVersion: v1\nkind: List\nmetadata:\n  name: a\n"])
async def test_apply_yaml_parse_failure_fails_closed_without_k8s(gates, server_with_mocks, content):
    gates(write=True, repair=True, allowed=["staging", "default"])
    server, api, init = server_with_mocks
    with pytest.raises(FeatureDisabledError) as denied:
        await server.execute_tool("k8s_resource_operations", {"operation": "apply_yaml", "yaml_content": content})
    assert denied.value.error_code == write_gate.MCP_NAMESPACE_NOT_ALLOWED
    init.assert_not_called()
    assert api.mock_calls == []


# ---------- config_path ----------


@pytest.mark.parametrize(("tool", "op"), [pair for pair in WRITE_PAIRS if pair[0] not in CLUSTER_TOOLS])
@pytest.mark.parametrize("open_all", [False, True])
async def test_write_with_custom_config_path_is_denied(gates, server_with_mocks, tool, op, open_all, tmp_path):
    if open_all:
        gates(write=True, repair=True, allowed=["staging"])
    kubeconfig = tmp_path / "other-kubeconfig"
    kubeconfig.write_text("offline")
    server, api, init = server_with_mocks
    with pytest.raises(FeatureDisabledError) as denied:
        await server.execute_tool(tool, write_params(tool, op, config_path=str(kubeconfig)))
    assert denied.value.error_code == write_gate.MCP_CONFIG_PATH_DENIED
    assert str(kubeconfig) not in denied.value.message
    init.assert_not_called()
    assert api.mock_calls == []


@pytest.mark.parametrize("value", [None, ""])
def test_default_config_path_values_do_not_trigger_config_path_denial(gates, value):
    gates(write=True, repair=True, allowed=["staging"])
    write_gate.enforce_write_gate("k8s_pod_management", {"operation": "delete_pod", "namespace": "staging", "config_path": value})


# ---------- Secret 读取 ----------


def _secret(name="db"):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            namespace="staging",
            uid="u",
            labels={"l": MARKER},
            annotations={"kubectl.kubernetes.io/last-applied-configuration": json.dumps({"data": {"password": MARKER}})},
            creation_timestamp=datetime.now(timezone.utc),
        ),
        type="Opaque",
        data={"password": base64.b64encode(MARKER.encode()).decode(), "user": base64.b64encode(b"u").decode()},
        string_data={"token": MARKER},
        immutable=False,
    )


@pytest.mark.parametrize(("tool", "op"), SECRET_PAIRS)
async def test_secret_read_default_off_is_denied_with_zero_k8s_calls(gates, server_with_mocks, tool, op):
    gates(write=True, repair=True, allowed=["staging"])  # 写开关不影响 Secret 读
    server, api, init = server_with_mocks
    with pytest.raises(FeatureDisabledError) as denied:
        await server.execute_tool(tool, {"operation": op, "namespace": "staging", "resource_name": "db"})
    assert denied.value.error_code == write_gate.MCP_SECRET_READ_DISABLED
    init.assert_not_called()
    assert api.mock_calls == []


@pytest.mark.parametrize(("tool", "op"), SECRET_PAIRS)
@pytest.mark.parametrize("params", [{"namespace": "prod"}, {"namespace": "kube-system"}, {"all_namespaces": True}, {}])
def test_secret_read_respects_namespace_whitelist(gates, tool, op, params):
    gates(secret=True, allowed=["staging", "kube-system"])
    with pytest.raises(FeatureDisabledError) as denied:
        write_gate.enforce_write_gate(tool, {"operation": op, **params})
    assert denied.value.error_code == write_gate.MCP_NAMESPACE_NOT_ALLOWED


@pytest.mark.parametrize(("tool", "op"), SECRET_PAIRS)
def test_secret_read_with_custom_config_path_is_denied(gates, tool, op):
    gates(secret=True, allowed=["staging"])
    with pytest.raises(FeatureDisabledError) as denied:
        write_gate.enforce_write_gate(tool, {"operation": op, "namespace": "staging", "config_path": "/tmp/other"})
    assert denied.value.error_code == write_gate.MCP_CONFIG_PATH_DENIED


async def test_list_secrets_returns_only_name_type_keys(gates, server_with_mocks):
    gates(secret=True, allowed=["staging"])
    server, api, _ = server_with_mocks
    api.list_namespaced_secret.return_value = SimpleNamespace(items=[_secret("db"), _secret("cache")])
    result = await server.execute_tool("k8s_config_management", {"operation": "list_secrets", "namespace": "staging"})
    assert result["success"] is True
    assert result["secrets"] == [
        {"name": "db", "type": "Opaque", "keys": ["password", "user", "token"]},
        {"name": "cache", "type": "Opaque", "keys": ["password", "user", "token"]},
    ]
    dumped = json.dumps(result)
    assert MARKER not in dumped and base64.b64encode(MARKER.encode()).decode() not in dumped
    api.list_secret_for_all_namespaces.assert_not_called()


async def test_get_secret_enabled_returns_keys_without_data_or_annotations(gates, server_with_mocks):
    gates(secret=True, allowed=["staging"])
    server, api, _ = server_with_mocks
    api.read_namespaced_secret.return_value = _secret("db")
    result = await server.execute_tool("k8s_config_management", {"operation": "get_secret", "namespace": "staging", "resource_name": "db"})
    assert result["secret"] == {"name": "db", "namespace": "staging", "type": "Opaque", "keys": ["password", "user", "token"]}
    assert MARKER not in json.dumps(result)


async def test_list_secrets_tool_itself_refuses_all_namespaces():
    from app.mcp.server.tools.k8s_config_tool import K8sConfigTool

    tool = K8sConfigTool()
    v1 = Mock()
    try:
        result = await tool._list_secrets(v1, {"all_namespaces": True})
    finally:
        await tool.cleanup()
    assert result["success"] is False
    v1.list_secret_for_all_namespaces.assert_not_called()


# ---------- SEC-33 P1-1：k8s_resource_operations 读 Secret ----------

RESOURCE_READ_OPS = sorted(write_gate.RESOURCE_TYPED_READ_OPERATIONS["k8s_resource_operations"])
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
MARKER_B64 = base64.b64encode(MARKER.encode()).decode()
_ABSENT = object()


def _v1_secret(kind=None):
    """真实 V1Secret（typed read 返回值 kind 可能为空）；data/stringData/labels/last-applied 都带标记串"""
    return k8s_client.V1Secret(
        api_version="v1",
        kind=kind,
        metadata=k8s_client.V1ObjectMeta(
            name="db",
            namespace="staging",
            uid="u",
            labels={"team": MARKER},
            annotations={LAST_APPLIED: json.dumps({"data": {"password": MARKER_B64}, "stringData": {"token": MARKER}})},
            creation_timestamp=datetime.now(timezone.utc),
        ),
        type="Opaque",
        data={"password": MARKER_B64, "user": base64.b64encode(b"u").decode()},
        string_data={"token": MARKER},
    )


def assert_secret_redacted(payload):
    dumped = json.dumps(payload, default=str)
    assert MARKER not in dumped and MARKER_B64 not in dumped
    assert LAST_APPLIED not in dumped and "last-applied" not in dumped
    for field_name in ('"data"', '"string_data"', '"stringData"', '"annotations"', '"labels"'):
        assert field_name not in dumped, field_name


SECRET_SUMMARY = {"name": "db", "namespace": "staging", "type": "Opaque", "keys": ["password", "user", "token"]}


def resource_read(op, resource_type=_ABSENT, **extra):
    params = {"operation": op, "namespace": "staging", "resource_name": "db", **extra}
    if resource_type is not _ABSENT:
        params["resource_type"] = resource_type
    return params


@pytest.mark.parametrize("op", RESOURCE_READ_OPS)
@pytest.mark.parametrize("resource_type", ["secret", "secrets", "Secret", " SECRETS ", "", None, _ABSENT, "unknown-kind", "secret-ish", 123, ["secret"]])
async def test_resource_read_of_secret_or_unknown_type_denied_by_default_with_zero_k8s_calls(gates, server_with_mocks, op, resource_type):
    gates(write=True, repair=True, allowed=["staging"])  # 写开关、白名单都不影响 Secret 读
    server, api, init = server_with_mocks
    with pytest.raises(FeatureDisabledError) as denied:
        await server.execute_tool("k8s_resource_operations", resource_read(op, resource_type))
    assert denied.value.error_code == write_gate.MCP_SECRET_READ_DISABLED
    init.assert_not_called()
    assert api.mock_calls == []


@pytest.mark.parametrize("op", RESOURCE_READ_OPS)
@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"namespace": "prod"}, write_gate.MCP_NAMESPACE_NOT_ALLOWED),
        ({"all_namespaces": True}, write_gate.MCP_NAMESPACE_NOT_ALLOWED),
        ({"config_path": "/tmp/other"}, write_gate.MCP_CONFIG_PATH_DENIED),
    ],
)
def test_resource_read_of_secret_needs_whitelist_rejects_all_namespaces_and_config_path(gates, op, params, code):
    gates(secret=True, allowed=["staging"])
    with pytest.raises(FeatureDisabledError) as denied:
        write_gate.enforce_write_gate("k8s_resource_operations", resource_read(op, "secrets", **params))
    assert denied.value.error_code == code


@pytest.mark.parametrize("op", RESOURCE_READ_OPS)
@pytest.mark.parametrize("resource_type", ["pod", " Pods ", "configmap", "CM", "deploy", "namespace", "node", "pvc"])
def test_resource_read_of_known_non_secret_type_still_passes_with_switches_off(gates, op, resource_type):
    assert write_gate.classify("k8s_resource_operations", resource_read(op, resource_type)).kind == "read"
    write_gate.enforce_write_gate("k8s_resource_operations", resource_read(op, resource_type, namespace="kube-system"))


@pytest.mark.parametrize("kind", [None, "Secret"])
@pytest.mark.parametrize("op", RESOURCE_READ_OPS)
async def test_resource_read_of_secret_enabled_and_whitelisted_returns_summary_only(gates, server_with_mocks, op, kind):
    gates(secret=True, allowed=["staging"])
    server, api, _ = server_with_mocks
    api.read_namespaced_secret.return_value = _v1_secret(kind)
    result = await server.execute_tool("k8s_resource_operations", resource_read(op, "secret"))
    assert result["success"] is True, result
    api.read_namespaced_secret.assert_called_once_with("db", "staging")
    assert k8s_writes(api) == []
    summary = result["resource_description"] if op == "describe_resource" else yaml.safe_load(result["yaml_content"])
    assert summary == SECRET_SUMMARY
    assert_secret_redacted(result)


@pytest.mark.parametrize("op", RESOURCE_READ_OPS)
async def test_resource_tool_redacts_secret_even_when_gate_is_bypassed(op):
    from app.mcp.server.tools.k8s_resource_tool import K8sResourceTool

    tool = K8sResourceTool()
    api = Mock(name="k8s-api")
    api.read_namespaced_secret.return_value = _v1_secret()
    try:
        method = tool._describe_resource if op == "describe_resource" else tool._get_resource_yaml
        result = await method(_Clients(api), resource_read(op, "secret"))
    finally:
        await tool.cleanup()
    assert result["success"] is True, result
    assert_secret_redacted(result)


def test_resource_to_dict_redacts_secret_objects_and_keeps_other_resources():
    from app.mcp.server.tools.k8s_resource_tool import K8sResourceTool

    tool = K8sResourceTool.__new__(K8sResourceTool)
    for secret in (_v1_secret(), _v1_secret("Secret")):
        assert tool._resource_to_dict(secret) == SECRET_SUMMARY
    # 非 V1Secret 对象：按 kind 或调用方的 resource_type 识别
    dict_secret = Mock(to_dict=Mock(return_value=_v1_secret("Secret").to_dict()), spec=["to_dict"])
    assert tool._resource_to_dict(dict_secret) == SECRET_SUMMARY
    assert tool._resource_to_dict(dict_secret, " Secrets ") == SECRET_SUMMARY
    config_map = k8s_client.V1ConfigMap(api_version="v1", kind="ConfigMap", metadata=k8s_client.V1ObjectMeta(name="c", namespace="staging", labels={"a": "b"}), data={"k": "v"})
    assert tool._resource_to_dict(config_map, "configmap")["data"] == {"k": "v"}


async def test_resource_label_and_annotation_writes_on_secret_return_keys_only(gates, server_with_mocks):
    gates(write=True, repair=True, allowed=["staging"])
    server, api, _ = server_with_mocks
    api.read_namespaced_secret.side_effect = lambda *args, **kwargs: _v1_secret()
    api.patch_namespaced_secret.side_effect = lambda name, namespace, body: body
    for params in (
        {"operation": "add_labels", "labels": {"new": "x"}},
        {"operation": "remove_labels", "label_keys": ["team"]},
        {"operation": "remove_annotations", "annotation_keys": [LAST_APPLIED]},
    ):
        result = await server.execute_tool("k8s_resource_operations", {"resource_type": "secret", "resource_name": "db", "namespace": "staging", **params})
        assert result["success"] is True, result
        dumped = json.dumps(result, default=str)
        assert MARKER not in dumped and MARKER_B64 not in dumped, params["operation"]


# ---------- HTTP 契约与日志 ----------


DENIALS = [
    ("k8s_pod_management", {"operation": "delete_pod", "namespace": MARKER}, {}, write_gate.MCP_WRITE_DISABLED),
    ("k8s_pod_management", {"operation": "delete_pod", "namespace": "prod-" + MARKER.lower()}, {"write": True, "repair": True, "allowed": ["staging"]}, write_gate.MCP_NAMESPACE_NOT_ALLOWED),
    ("k8s_resource_operations", {"operation": "add_labels", "resource_type": "node", "resource_name": MARKER}, {"write": True, "repair": True, "allowed": ["default"]}, write_gate.MCP_CLUSTER_SCOPED_DENIED),
    ("k8s_pod_management", {"operation": "delete_pod", "namespace": "staging", "config_path": "/tmp/" + MARKER}, {"write": True, "repair": True, "allowed": ["staging"]}, write_gate.MCP_CONFIG_PATH_DENIED),
    ("k8s_config_management", {"operation": "get_secret", "namespace": "staging", "resource_name": MARKER}, {}, write_gate.MCP_SECRET_READ_DISABLED),
]


@pytest.mark.parametrize(("tool", "params", "switches", "code"), DENIALS)
async def test_http_denial_is_200_with_error_code_fixed_message_and_no_param_values_in_logs(gates, server_with_mocks, monkeypatch, caplog, tool, params, switches, code):
    gates(**switches)
    server, api, init = server_with_mocks
    monkeypatch.setattr(main, "mcp_server", server)
    app = FastAPI()
    app.post("/tools/execute")(main.execute_tool)
    caplog.set_level(logging.DEBUG)
    response = TestClient(app).post("/tools/execute", json={"tool": tool, "parameters": {**params, "operation_note": MARKER}})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["error_code"] == code
    assert body["result"] is None
    assert body["error"] and MARKER not in response.text and MARKER.lower() not in response.text
    assert MARKER not in caplog.text and MARKER.lower() not in caplog.text
    assert code in caplog.text and tool in caplog.text
    init.assert_not_called()
    assert api.mock_calls == [] and k8s_writes(api) == []


def test_denial_log_has_no_operation_or_namespace_values(gates, caplog):
    caplog.set_level(logging.DEBUG, logger="aiops.mcp.write_gate")
    with pytest.raises(FeatureDisabledError):
        write_gate.enforce_write_gate("k8s_pod_management", {"operation": "delete_pod", "namespace": "ns-x9"})
    assert "delete_pod" not in caplog.text and "ns-x9" not in caplog.text


def test_tool_response_success_has_no_error_code(monkeypatch, gates):
    server = Mock(tools={"calculate": object()})

    async def ok(**kwargs):
        return {"success": True}

    server.execute_tool = ok
    monkeypatch.setattr(main, "mcp_server", server)
    app = FastAPI()
    app.post("/tools/execute")(main.execute_tool)
    body = TestClient(app).post("/tools/execute", json={"tool": "calculate", "parameters": {}}).json()
    assert body["status"] == "success" and body["error_code"] is None


# ---------- 异常与开关解析 ----------


def test_feature_disabled_error_default_code_unchanged_and_custom_code_supported():
    assert FeatureDisabledError("m", switch="s").error_code == "AUTOFIX_FEATURE_DISABLED"
    custom = FeatureDisabledError("m", switch="s", error_code="MCP_WRITE_DISABLED")
    assert custom.error_code == "MCP_WRITE_DISABLED" and custom.status_code == 403 and custom.details == {"switch": "s"}


@pytest.mark.parametrize("env_key", ["MCP_WRITE_TOOLS_ENABLED", "MCP_SECRET_READ_ENABLED"])
@pytest.mark.parametrize(("raw", "expected"), [(None, False), ("true", True), (" TRUE ", True), ("false", False), ("1", False), ("yes", False), ("", False)])
def test_new_switches_default_off_and_parse_like_autofix_switches(monkeypatch, env_key, raw, expected):
    for key in ("MCP_WRITE_TOOLS_ENABLED", "MCP_SECRET_READ_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    if raw is not None:
        monkeypatch.setenv(env_key, raw)
    cfg = MCPConfig()
    field = "write_tools_enabled" if env_key == "MCP_WRITE_TOOLS_ENABLED" else "secret_read_enabled"
    assert getattr(cfg, field) is expected


def test_shipped_yaml_configs_keep_new_switches_off():
    import yaml

    root = Path(__file__).resolve().parents[2] / "config"
    for name in ("config.yaml", "config.production.yaml", "config.test.yaml"):
        mcp = yaml.safe_load((root / name).read_text(encoding="utf-8"))["mcp"]
        assert mcp["write_tools_enabled"] is False and mcp["secret_read_enabled"] is False, name
