#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 自动修复写操作的统一门禁（开关 + 命名空间白名单/受限命名空间）
"""

from __future__ import annotations

from app.common.exceptions import FeatureDisabledError, NamespaceNotAllowedError
from app.config.settings import config

from .remediation import AutoFixRemediationEngine

REPAIR_DISABLED_MESSAGE = "自动修复入口 /autofix/repair 已关闭（autofix.repair_enabled / AUTOFIX_REPAIR_ENABLED=false）；请使用 /autofix/workflow 生成计划并经 /autofix/workflow/confirm 确认"
AUTO_EXECUTE_DISABLED_MESSAGE = "自动执行已关闭（autofix.auto_execute_enabled / AUTOFIX_AUTO_EXECUTE_ENABLED=false）；请经 /autofix/workflow/confirm 确认后执行"


def build_namespace_policy(k8s_client=None) -> AutoFixRemediationEngine:
    """按 autofix 配置构造执行引擎（白名单为空即 fail-closed，默认不自动执行）。

    k8s_client=None 时只作为策略对象使用，不会触达 Kubernetes。
    """
    return AutoFixRemediationEngine(
        k8s_client=k8s_client,
        allowed_namespaces=list(config.autofix.allowed_namespaces or []),
        auto_execute=bool(config.autofix.auto_execute_enabled),
    )


def ensure_repair_enabled() -> None:
    if not config.autofix.repair_enabled:
        raise FeatureDisabledError(REPAIR_DISABLED_MESSAGE, switch="autofix.repair_enabled")


def ensure_namespace_allowed(namespace: str, subject: str, outcome: str) -> None:
    """namespace 必须在 allowed_namespaces 内且不属于受限命名空间，否则抛 NamespaceNotAllowedError。

    与 run/confirm 共用 AutoFixRemediationEngine._namespace_allowed 判定；白名单大小写敏感、不支持通配符。
    """
    policy = build_namespace_policy(k8s_client=None)
    if policy._namespace_allowed(namespace):
        return
    prefix = f"{subject}，但目标命名空间 {namespace} "
    if namespace in policy.restricted_namespaces:
        reason = "属于受限命名空间，即使写入 autofix.allowed_namespaces / AUTOFIX_ALLOWED_NAMESPACES 也不允许执行"
    elif not policy.allowed_namespaces:
        reason = f"不在白名单内：autofix.allowed_namespaces / AUTOFIX_ALLOWED_NAMESPACES 为空（fail-closed），请将 {namespace} 加入 AUTOFIX_ALLOWED_NAMESPACES"
    else:
        reason = f"不在 autofix.allowed_namespaces / AUTOFIX_ALLOWED_NAMESPACES 白名单内，请将 {namespace} 加入 AUTOFIX_ALLOWED_NAMESPACES"
    raise NamespaceNotAllowedError(f"{prefix}{reason}；{outcome}", namespace=str(namespace or ""))


def ensure_auto_execute_enabled() -> None:
    if config.autofix.auto_execute_enabled is not True:
        raise FeatureDisabledError(AUTO_EXECUTE_DISABLED_MESSAGE, switch="autofix.auto_execute_enabled")


def ensure_mutation_allowed(
    namespace: str,
    operation: str,
    *,
    require_repair_enabled: bool = True,
    require_auto_execute: bool = False,
) -> None:
    """写执行点门禁：开关关闭或命名空间不允许即拒绝，且不触达 Kubernetes。

    - K8sFixerAgent（/autofix/repair 链路）：默认参数，要求 autofix.repair_enabled。
    - SEC-44 G2 AutoFixRemediationEngine._execute_action（/workflow 自动执行与 /workflow/confirm）：
      ``require_repair_enabled=False``——repair_enabled 只管 /autofix/repair 入口，默认关闭，若要求它会让默认配置下
      的确认流程不可用；自动执行时 ``require_auto_execute=True``，按当前配置再校验 autofix.auto_execute_enabled。
    - 命名空间一律按当前配置（allowed_namespaces + 受限命名空间）校验。
    """
    if require_repair_enabled:
        ensure_repair_enabled()
    if require_auto_execute:
        ensure_auto_execute_enabled()
    switch = "autofix.repair_enabled" if require_repair_enabled else "autofix 写操作门禁"
    ensure_namespace_allowed(
        namespace,
        subject=f"自动修复写操作 {operation} 已被 {switch} 放行",
        outcome="已拒绝执行写操作",
    )
