#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: SEC-33 MCP 写工具门禁

所有 MCP 工具调用在进入 tool.execute 之前都经过 enforce_write_gate：
- 只读白名单按「工具名 + operation」精确匹配；未归类的工具或 operation 一律按写处理。
- 集群级写（patch_namespace / patch_node、create/delete_namespace、集群级 YAML kind）始终拒绝。
- 写操作带非默认 config_path（切换 kubeconfig/集群）一律拒绝，后续由 SEC-39 处理。
- list_secrets/get_secret 需要 mcp.secret_read_enabled=true 且命名空间在白名单内（不允许 all_namespaces）。
- 写操作需要 mcp.write_tools_enabled=true 且 autofix.repair_enabled=true，
  且目标命名空间通过 autofix.allowed_namespaces 白名单（复用 ensure_namespace_allowed）。
拒绝时抛 FeatureDisabledError（固定文案，不带内部细节），不发起任何 Kubernetes 调用。
"""

from dataclasses import dataclass, field
import logging
from typing import Any, Dict, FrozenSet, List, Mapping, Optional

from app.common.exceptions import FeatureDisabledError, NamespaceNotAllowedError

logger = logging.getLogger("aiops.mcp.write_gate")

MCP_WRITE_DISABLED = "MCP_WRITE_DISABLED"
MCP_NAMESPACE_NOT_ALLOWED = "MCP_NAMESPACE_NOT_ALLOWED"
MCP_CLUSTER_SCOPED_DENIED = "MCP_CLUSTER_SCOPED_DENIED"
MCP_CONFIG_PATH_DENIED = "MCP_CONFIG_PATH_DENIED"
MCP_SECRET_READ_DISABLED = "MCP_SECRET_READ_DISABLED"

ERROR_CODES = (
    MCP_WRITE_DISABLED,
    MCP_NAMESPACE_NOT_ALLOWED,
    MCP_CLUSTER_SCOPED_DENIED,
    MCP_CONFIG_PATH_DENIED,
    MCP_SECRET_READ_DISABLED,
)

WRITE_DISABLED_MESSAGE = "MCP 写工具已关闭（mcp.write_tools_enabled / MCP_WRITE_TOOLS_ENABLED 默认 false），未执行任何操作"
REPAIR_DISABLED_MESSAGE = "MCP 写工具要求 autofix.repair_enabled=true，当前未开启，未执行任何操作"
NAMESPACE_DENIED_MESSAGE = "目标命名空间不在 autofix.allowed_namespaces 白名单内、属于受限命名空间或无法确定，未执行任何操作"
CLUSTER_SCOPED_MESSAGE = "MCP 不允许集群级写操作（如创建/删除/修改 Namespace、修改 Node），未执行任何操作"
CONFIG_PATH_DENIED_MESSAGE = "MCP 写操作和 Secret 读取不允许指定 config_path（切换 kubeconfig），未执行任何操作"
SECRET_READ_DISABLED_MESSAGE = "MCP Secret 读取已关闭（mcp.secret_read_enabled / MCP_SECRET_READ_ENABLED 默认 false），未执行任何操作"

# 整个工具只读（无 operation 参数）
READ_ONLY_TOOLS: FrozenSet[str] = frozenset(
    {
        "get_current_time",
        "get_system_info",
        "read_file",
        "list_directory",
        "get_file_stats",
        "calculate",
        "calculate_statistics",
        "convert_units",
        "k8s_cluster_check",
    }
)

# 按 operation 区分读写的工具：这里只列只读 operation，其余一律按写
READ_OPERATIONS: Mapping[str, FrozenSet[str]] = {
    "k8s_pod_management": frozenset({"list_pods", "get_pod_details", "get_pod_events"}),
    "k8s_logs_viewer": frozenset({"get_pod_logs", "get_container_logs", "get_previous_logs", "tail_logs", "search_logs"}),
    "k8s_resource_monitor": frozenset(
        {
            "get_node_metrics",
            "get_pod_metrics",
            "get_resource_quotas",
            "get_limit_ranges",
            "get_top_pods",
            "get_top_nodes",
        }
    ),
    "k8s_namespace_management": frozenset({"list_namespaces", "get_namespace", "get_namespace_resources"}),
    "k8s_service_management": frozenset({"list_services", "get_service_details", "get_endpoints"}),
    "k8s_deployment_management": frozenset({"list_deployments", "get_deployment_status", "get_rollout_history"}),
    "k8s_application_scaling": frozenset({"list_hpa", "get_hpa_status"}),
    "k8s_ingress_management": frozenset({"list_ingresses", "get_ingress", "get_ingress_classes"}),
    "k8s_config_management": frozenset({"list_configmaps", "get_configmap"}),
    "k8s_resource_operations": frozenset({"describe_resource", "get_resource_yaml"}),
}

# Secret 读取：单独开关 + 命名空间白名单，不按普通只读放行
SECRET_READ_OPERATIONS: Mapping[str, FrozenSet[str]] = {
    "k8s_config_management": frozenset({"list_secrets", "get_secret"}),
}

# 所有写 operation 都是集群级的工具（create_namespace / delete_namespace）
CLUSTER_SCOPED_WRITE_TOOLS: FrozenSet[str] = frozenset({"k8s_namespace_management"})

# k8s_resource_operations 标签/注解写：resource_type 必须精确属于命名空间级集合，
# 否则（namespace→patch_namespace、node→patch_node、pv、未知类型）按集群级拒绝
RESOURCE_LABEL_OPERATIONS: FrozenSet[str] = frozenset({"add_labels", "remove_labels", "add_annotations", "remove_annotations"})
NAMESPACED_RESOURCE_TYPES: FrozenSet[str] = frozenset({"pod", "service", "deployment", "replicaset", "configmap", "secret", "ingress"})

# apply_yaml 中可静态识别的集群级 kind；其余 kind 由工具在 discovery 阶段再校验 namespaced
CLUSTER_SCOPED_YAML_KINDS: FrozenSet[str] = frozenset(
    {
        "Namespace",
        "Node",
        "PersistentVolume",
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "StorageClass",
        "PriorityClass",
        "RuntimeClass",
        "IngressClass",
        "APIService",
        "MutatingWebhookConfiguration",
        "ValidatingWebhookConfiguration",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
        "CertificateSigningRequest",
        "VolumeAttachment",
        "CSIDriver",
        "CSINode",
    }
)


@dataclass(frozen=True)
class GateDecision:
    """门禁分类结果：kind ∈ {read, secret_read, write, cluster}；namespaces 为 None 表示无法确定（fail-closed）"""

    kind: str
    namespaces: Optional[List[str]] = field(default=None)


def _operation(parameters: Mapping[str, Any]) -> Any:
    return parameters.get("operation") if isinstance(parameters, Mapping) else None


def _deny(tool_name: str, message: str, switch: str, error_code: str) -> FeatureDisabledError:
    # 审计日志只记工具名（已注册名称）与错误码/开关名，不记录任何参数值（含 operation、namespace、config_path）
    logger.warning("MCP 工具调用被门禁拒绝: tool=%s, error_code=%s, switch=%s", tool_name, error_code, switch)
    return FeatureDisabledError(message, switch=switch, error_code=error_code)


def _ensure_namespaces(tool_name: str, namespaces: Optional[List[str]], subject: str) -> None:
    from app.core.autofix.policy import ensure_namespace_allowed

    if not namespaces:
        raise _deny(tool_name, NAMESPACE_DENIED_MESSAGE, "autofix.allowed_namespaces", MCP_NAMESPACE_NOT_ALLOWED)
    for namespace in namespaces:
        try:
            ensure_namespace_allowed(namespace, subject=subject, outcome="未执行任何操作")
        except NamespaceNotAllowedError:
            raise _deny(tool_name, NAMESPACE_DENIED_MESSAGE, "autofix.allowed_namespaces", MCP_NAMESPACE_NOT_ALLOWED) from None


def _apply_yaml_targets(parameters: Mapping[str, Any]) -> Optional[List[Dict[str, str]]]:
    """复用 apply_yaml 的本地解析拿到 (kind, namespace)；任何解析失败返回 None（fail-closed）"""
    try:
        from app.mcp.server.tools.yaml_apply import _parse

        _, identities = _parse(dict(parameters))
    except Exception:  # noqa: BLE001 - 解析失败一律按无法确定处理
        return None
    return identities


def classify(tool_name: str, parameters: Mapping[str, Any]) -> GateDecision:
    """按工具名+operation 分类；未归类一律按写"""
    if tool_name in READ_ONLY_TOOLS:
        return GateDecision("read")
    if not isinstance(parameters, Mapping):
        return GateDecision("write", None)
    operation = _operation(parameters)
    read_ops = READ_OPERATIONS.get(tool_name, frozenset())
    if isinstance(operation, str) and operation in read_ops:
        return GateDecision("read")

    if isinstance(operation, str) and operation in SECRET_READ_OPERATIONS.get(tool_name, frozenset()):
        if parameters.get("all_namespaces", False) is not False:
            return GateDecision("secret_read", None)
        return GateDecision("secret_read", _single_namespace(parameters))

    if tool_name in CLUSTER_SCOPED_WRITE_TOOLS:
        return GateDecision("cluster")

    if tool_name == "k8s_resource_operations":
        if operation in RESOURCE_LABEL_OPERATIONS:
            resource_type = parameters.get("resource_type")
            if not isinstance(resource_type, str) or resource_type not in NAMESPACED_RESOURCE_TYPES:
                return GateDecision("cluster")
        elif operation == "apply_yaml":
            identities = _apply_yaml_targets(parameters)
            if identities is None:
                return GateDecision("write", None)
            if any(item.get("kind") in CLUSTER_SCOPED_YAML_KINDS for item in identities):
                return GateDecision("cluster")
            return GateDecision("write", sorted({item["namespace"] for item in identities}))

    return GateDecision("write", _single_namespace(parameters))


def _single_namespace(parameters: Mapping[str, Any]) -> Optional[List[str]]:
    """与各 K8s 工具一致：namespace 缺省为 default；非字符串或空串按无法确定处理"""
    namespace = parameters.get("namespace", "default")
    if not isinstance(namespace, str) or not namespace:
        return None
    return [namespace]


def has_custom_config_path(parameters: Mapping[str, Any]) -> bool:
    """config_path 缺省、None 或空串时工具走默认 kubeconfig/in-cluster；其它任何取值都视为非默认"""
    if "config_path" not in parameters:
        return False
    value = parameters.get("config_path")
    return not (value is None or value == "")


def enforce_write_gate(tool_name: str, parameters: Mapping[str, Any]) -> None:
    """读放行；Secret 读与写按固定顺序检查，任何一步不满足即拒绝（抛 FeatureDisabledError）。

    Secret 读：secret_read_enabled → config_path → 命名空间白名单
    写：集群级 → config_path → write_tools_enabled → repair_enabled → 命名空间白名单
    """
    from app.config.settings import config

    decision = classify(tool_name, parameters)
    if decision.kind == "read":
        return
    if decision.kind == "secret_read":
        if config.mcp.secret_read_enabled is not True:
            raise _deny(tool_name, SECRET_READ_DISABLED_MESSAGE, "mcp.secret_read_enabled", MCP_SECRET_READ_DISABLED)
        if has_custom_config_path(parameters):
            raise _deny(tool_name, CONFIG_PATH_DENIED_MESSAGE, "mcp.config_path", MCP_CONFIG_PATH_DENIED)
        _ensure_namespaces(tool_name, decision.namespaces, "MCP Secret 读取")
        return
    if decision.kind == "cluster":
        raise _deny(tool_name, CLUSTER_SCOPED_MESSAGE, "mcp.cluster_scoped", MCP_CLUSTER_SCOPED_DENIED)
    if isinstance(parameters, Mapping) and has_custom_config_path(parameters):
        raise _deny(tool_name, CONFIG_PATH_DENIED_MESSAGE, "mcp.config_path", MCP_CONFIG_PATH_DENIED)
    if config.mcp.write_tools_enabled is not True:
        raise _deny(tool_name, WRITE_DISABLED_MESSAGE, "mcp.write_tools_enabled", MCP_WRITE_DISABLED)
    if config.autofix.repair_enabled is not True:
        raise _deny(tool_name, REPAIR_DISABLED_MESSAGE, "autofix.repair_enabled", MCP_WRITE_DISABLED)
    _ensure_namespaces(tool_name, decision.namespaces, "MCP 写工具")
