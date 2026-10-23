#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 诊断结果到可执行修复动作的转换、风险评估和执行记录
"""

from __future__ import annotations

import asyncio
import copy
from copy import deepcopy
from datetime import datetime
import logging
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from .audit import PHASE_INTENT, PHASE_REFUSED, PHASE_RESULT, build_audit_record, emit_audit, new_audit_id

logger = logging.getLogger("aiops.autofix.remediation")

_MAX_REASON_LEN = 64

# SEC-44 G6：现有全部可执行动作及其风险等级；只有白名单内（且风险评估为 low）的动作才可能免确认自动执行
ACTION_RISK_GRADES: Dict[str, str] = {
    "restart_deployment": "low",
    "patch_image_pull_policy": "low",
    "scale_deployment": "medium",
    "adjust_resource_requests": "medium",
}
AUTO_EXECUTE_ACTION_TYPES = frozenset({"restart_deployment", "patch_image_pull_policy"})

# SEC-44 G1：动作 → 对应故障类型 → 必须在实时 Pod 状态中看到的信号（任一即匹配）
ACTION_FAULT_TYPES: Dict[str, str] = {
    "restart_deployment": "crash_loop_backoff",
    "patch_image_pull_policy": "image_pull_failure",
    "scale_deployment": "resource_insufficient",
    "adjust_resource_requests": "resource_insufficient",
}
FAULT_EVIDENCE_SIGNALS: Dict[str, frozenset] = {
    "crash_loop_backoff": frozenset({"crash_loop_backoff", "high_restart_count", "not_ready_after_restart"}),
    "image_pull_failure": frozenset({"image_pull_backoff"}),
    "resource_insufficient": frozenset({"oom_killed", "unschedulable"}),
}
HIGH_RESTART_COUNT = 3
EVIDENCE_TIMEOUT_SECONDS = 15.0
_IMAGE_PULL_REASONS = {"ImagePullBackOff", "ErrImagePull"}


def auto_execution_refusal(action: Dict[str, Any]) -> Optional[str]:
    """SEC-44：动作能否免确认自动执行；返回拒绝原因（需人工确认）或 None。

    条件全部满足才放行：动作类型在低风险白名单内、风险评估为 low、风险评估里附带的证据来自实时读取
    （source=live）且与该动作对应的故障类型匹配。请求文字或 diagnosis 本身永远不能单独满足这些条件。
    """
    action_type = action.get("action_type")
    if action_type not in AUTO_EXECUTE_ACTION_TYPES:
        return "action not in auto-execute whitelist"
    risk = action.get("risk_assessment") or {}
    if risk.get("risk_level") != "low":
        return "risk level not low"
    evidence = risk.get("live_evidence") or {}
    if evidence.get("source") != "live" or evidence.get("matched") is not True:
        return "live evidence missing or mismatched"
    if evidence.get("fault_type") != ACTION_FAULT_TYPES.get(action_type):
        return "live evidence missing or mismatched"
    return None


def api_error_summary(exc: BaseException) -> Dict[str, Any]:
    """异常日志摘要：只含异常类型、HTTP status 与 reason，不含 body/headers（可能含集群内部信息）"""
    status = getattr(exc, "status", None)
    reason = getattr(exc, "reason", None)
    return {
        "type": type(exc).__name__,
        "status": status if isinstance(status, int) else None,
        "reason": reason[:_MAX_REASON_LEN] if isinstance(reason, str) else None,
    }


@runtime_checkable
class RemediationEngine(Protocol):
    """修复执行引擎协议：autofix_service 对所有引擎（含测试替身）都在执行前调用 namespace_refusal 预检"""

    async def build_plan(self, diagnosis_result: Dict[str, Any]) -> Dict[str, Any]: ...

    async def execute_plan(self, plan: Dict[str, Any], *, trigger: str = "auto") -> Dict[str, Any]: ...

    def namespace_refusal(self, namespace: str, actions: List[Dict[str, Any]]) -> Optional[str]:
        """返回拒绝原因（不允许执行）或 None（允许）"""
        ...


class AutoFixRemediationEngine:
    """将诊断结果转换为修复计划，并只执行风险可控的动作。

    SEC-26：默认只出计划。``auto_execute=False`` 时所有可执行动作（含 low）都标记为
    需要人工确认；``allowed_namespaces`` 为空时命名空间一律不允许执行（fail-closed）。
    """

    DEFAULT_RESTRICTED_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}

    def __init__(
        self,
        k8s_client: Any,
        allowed_namespaces: Optional[List[str]] = None,
        restricted_namespaces: Optional[List[str]] = None,
        max_auto_replicas: int = 1,
        rollout_timeout_seconds: int = 120,
        auto_execute: bool = False,
    ) -> None:
        self.k8s_client = k8s_client
        # 空白名单即不允许任何命名空间执行（fail-closed），不再表示“全部允许”
        self.allowed_namespaces = {str(ns).strip() for ns in (allowed_namespaces or []) if str(ns).strip()}
        self.auto_execute = bool(auto_execute)
        self.restricted_namespaces = set(restricted_namespaces or self.DEFAULT_RESTRICTED_NAMESPACES)
        self.max_auto_replicas = max(1, max_auto_replicas)
        self.rollout_timeout_seconds = rollout_timeout_seconds

    async def build_plan(self, diagnosis_result: Dict[str, Any]) -> Dict[str, Any]:
        """根据诊断结果生成候选修复动作并完成执行前风险评估。"""
        target = self._extract_target(diagnosis_result)
        deployment = await self._get_deployment(target["deployment"], target["namespace"])
        fault_type = self._classify_fault(diagnosis_result)
        candidate_actions = self._generate_candidate_actions(fault_type, target, deployment)
        evidence: Optional[Dict[str, Any]] = None
        if self.auto_execute and any(a.get("executable") for a in candidate_actions):
            # SEC-44 G1：只在开启自动执行时实时读取证据（非白名单动作也记录证据，便于人工确认时参考）；
            # 默认配置（auto_execute=False）不增加任何集群读取
            evidence = await self._collect_live_evidence(fault_type, target, deployment)

        assessed_actions = []
        for action in candidate_actions:
            action = deepcopy(action)
            if action.get("executable"):
                action["risk_assessment"] = await self._assess_risk(action, target, deployment, evidence)
            else:
                action["risk_assessment"] = {
                    "allowed": False,
                    "risk_level": "none",
                    "resource_exists": deployment is not None,
                    "permission_allowed": None,
                    "namespace_allowed": self._namespace_allowed(target["namespace"]),
                    "affects_multiple_replicas": False,
                    "rollback_available": False,
                    "requires_human_confirmation": False,
                    "reasons": ["manual inspection action"],
                }
            assessed_actions.append(action)

        executable_actions = [a for a in assessed_actions if a.get("executable")]
        allowed_actions = [a for a in executable_actions if a.get("risk_assessment", {}).get("allowed")]
        blocked_actions = [a for a in executable_actions if not a.get("risk_assessment", {}).get("allowed")]

        return {
            "plan_id": f"autofix-{target['namespace']}-{target['deployment']}-{int(datetime.now().timestamp())}",
            "fault_type": fault_type,
            "deployment": target["deployment"],
            "namespace": target["namespace"],
            "resource_exists": deployment is not None,
            "candidate_actions": assessed_actions,
            "allowed_actions": allowed_actions,
            "blocked_actions": blocked_actions,
            "created_at": datetime.now().isoformat(),
            "summary": self._summarize_plan(fault_type, assessed_actions),
        }

    async def execute_plan(self, plan: Dict[str, Any], dry_run: bool = False, *, trigger: str = "auto") -> Dict[str, Any]:
        """执行计划中风险可控的动作，并记录执行结果和事后状态检查。

        SEC-44：``trigger="auto"``（默认，最严）表示免确认自动执行，每个动作还必须通过 auto_execution_refusal
        （低风险白名单 + 实时证据匹配）且实例 auto_execute 开启；``trigger="confirm"`` 只由 /workflow/confirm 在人工
        确认后使用。两种触发都经过 _execute_action 内的写操作门禁（G2）与意图/结果审计（G4）。
        """
        if trigger not in {"auto", "confirm"}:
            raise ValueError("trigger 只能是 auto 或 confirm")
        auto = trigger == "auto"
        deployment = plan.get("deployment", "")
        namespace = plan.get("namespace", "default")
        actions = plan.get("allowed_actions")
        if actions is None:
            actions = [
                action
                for action in plan.get("candidate_actions", [])
                if action.get("executable") and action.get("risk_assessment", {}).get("allowed")
            ]

        blocked_actions = plan.get("blocked_actions")
        if blocked_actions is None:
            blocked_actions = [
                action
                for action in plan.get("candidate_actions", [])
                if action.get("executable") and not action.get("risk_assessment", {}).get("allowed")
            ]

        if actions and not dry_run:
            refused = self.namespace_refusal(namespace, actions)
            refusal_kind = "namespace"
            if not refused and auto:
                refusal_kind = "auto_eligibility"
                if not self.auto_execute:
                    refused = "自动执行未开启，动作需经 /workflow/confirm 人工确认"
                else:
                    reasons = [auto_execution_refusal(action) for action in actions]
                    if any(reasons):
                        refused = f"自动执行条件未满足（{next(r for r in reasons if r)}），动作需经 /workflow/confirm 人工确认"
            if refused:
                return {
                    "status": "blocked",
                    "deployment": deployment,
                    "namespace": namespace,
                    "fault_type": plan.get("fault_type", "unknown"),
                    "executed_actions": [],
                    "blocked_actions": list(blocked_actions) + deepcopy(actions),
                    "post_check": None,
                    "not_attempted_actions": deepcopy(actions),
                    "error": refused,
                    "refusal": refusal_kind,
                    "dry_run": dry_run,
                    "timestamp": datetime.now().isoformat(),
                }

        if not actions:
            return {
                "status": "blocked" if blocked_actions else "planned",
                "deployment": deployment,
                "namespace": namespace,
                "fault_type": plan.get("fault_type", "unknown"),
                "executed_actions": [],
                "blocked_actions": blocked_actions,
                "post_check": None,
                "dry_run": dry_run,
                "timestamp": datetime.now().isoformat(),
            }

        executed_actions = []
        not_attempted_actions = []
        refusal: Optional[Dict[str, Any]] = None
        for index, action in enumerate(actions):
            execution_record = await self._execute_action(action, deployment, namespace, dry_run=dry_run, auto_executed=auto)
            if execution_record.get("status") == "refused":
                # SEC-44 G2/G4：门禁未放行或意图审计失败，该动作未写集群，其后动作也不尝试
                refusal = execution_record
                not_attempted_actions = deepcopy(actions[index:])
                break
            executed_actions.append(execution_record)
            if execution_record.get("status") == "failed":
                not_attempted_actions = deepcopy(actions[index + 1:])
                break

        if refusal is not None and not executed_actions:
            return {
                "status": "blocked",
                "deployment": deployment,
                "namespace": namespace,
                "fault_type": plan.get("fault_type", "unknown"),
                "executed_actions": [],
                "blocked_actions": list(blocked_actions) + deepcopy(actions),
                "post_check": None,
                "not_attempted_actions": not_attempted_actions,
                "error": refusal.get("error"),
                "refusal": refusal.get("refusal"),
                "dry_run": dry_run,
                "timestamp": datetime.now().isoformat(),
            }

        post_check = None
        if not dry_run and executed_actions:
            post_check = await self._post_check(deployment, namespace)

        failed = [record for record in executed_actions if record.get("status") == "failed"]
        if refusal is not None:
            failed.append(refusal)
        # API接受补丁只代表动作已提交，必须确认工作负载实际就绪才算修复完成
        rollout_failed = not dry_run and bool(executed_actions) and (
            not isinstance(post_check, dict) or post_check.get("ready") is not True
        )
        return {
            "status": "failed" if failed or rollout_failed else "completed",
            "deployment": deployment,
            "namespace": namespace,
            "fault_type": plan.get("fault_type", "unknown"),
            "executed_actions": executed_actions,
            "blocked_actions": blocked_actions,
            "post_check": post_check,
            "not_attempted_actions": not_attempted_actions,
            "error": "修复动作或工作负载就绪检查未确认成功，请核对实际状态，勿盲目重试" if failed or rollout_failed else None,
            "dry_run": dry_run,
            "timestamp": datetime.now().isoformat(),
        }

    async def plan_and_execute(self, diagnosis_result: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
        """一站式生成计划并执行允许范围内的动作（属于免确认执行，按 trigger="auto" 校验）。"""
        plan = await self.build_plan(diagnosis_result)
        result = await self.execute_plan(plan, dry_run=dry_run, trigger="auto")
        result["plan"] = plan
        return result

    def _extract_target(self, diagnosis_result: Dict[str, Any]) -> Dict[str, str]:
        diagnosis = diagnosis_result.get("diagnosis") or {}
        deployment = (
            diagnosis_result.get("deployment")
            or diagnosis.get("deployment_name")
            or diagnosis.get("deployment", {}).get("metadata", {}).get("name")
            or diagnosis_result.get("resource_name")
            or "unknown"
        )
        namespace = (
            diagnosis_result.get("namespace")
            or diagnosis.get("namespace")
            or diagnosis.get("deployment", {}).get("metadata", {}).get("namespace")
            or "default"
        )
        return {"deployment": str(deployment), "namespace": str(namespace)}

    async def _get_deployment(self, name: str, namespace: str) -> Optional[Dict[str, Any]]:
        if not name or name == "unknown" or not hasattr(self.k8s_client, "get_deployment"):
            return None
        try:
            return await self.k8s_client.get_deployment(name, namespace)
        except Exception as exc:
            # 承接 SEC-26 s7：只记异常类型、HTTP status 和 reason，不记 body
            summary = api_error_summary(exc)
            logger.warning(
                "获取Deployment失败，将按资源不存在评估风险: 错误类型=%s, status=%s, reason=%s",
                summary["type"], summary["status"], summary["reason"],
            )
            return None

    async def _collect_live_evidence(
        self,
        fault_type: str,
        target: Dict[str, str],
        deployment: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """SEC-44 G1：从 Kubernetes 实时读取目标 Deployment 的 Pod 状态作为自动执行证据。

        只使用实时读取的 Deployment（selector）与 Pod 列表，从不读取请求里的 diagnosis；读取失败、超时、
        没有 Pod 或信号与故障类型不匹配时 matched=False，自动执行一律转人工确认。
        """
        evidence: Dict[str, Any] = {
            "source": "live",
            "fault_type": fault_type,
            "checked": False,
            "matched": False,
            "signals": [],
            "pods_checked": 0,
            "reason": None,
        }
        if deployment is None:
            evidence["reason"] = "target deployment not found in live cluster state"
            return evidence
        selector = self._label_selector(deployment)
        if not selector:
            evidence["reason"] = "live deployment selector unavailable"
            return evidence
        if not hasattr(self.k8s_client, "get_pods"):
            evidence["reason"] = "live pod reader unavailable"
            return evidence
        try:
            pods = await asyncio.wait_for(
                self.k8s_client.get_pods(target["namespace"], selector, raise_errors=True),
                timeout=EVIDENCE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("实时证据读取超时，自动执行转人工确认")
            evidence["reason"] = "live evidence read timed out"
            return evidence
        except Exception as exc:
            summary = api_error_summary(exc)
            logger.warning(
                "实时证据读取失败，自动执行转人工确认: 错误类型=%s, status=%s",
                summary["type"], summary["status"],
            )
            evidence["reason"] = "live evidence read failed"
            return evidence
        if not isinstance(pods, list) or not pods:
            evidence["reason"] = "no live pods for target deployment"
            return evidence
        signals = self._pod_signals(pods)
        evidence["checked"] = True
        evidence["pods_checked"] = len(pods)
        evidence["signals"] = sorted(signals)
        evidence["matched"] = bool(signals & FAULT_EVIDENCE_SIGNALS.get(fault_type, frozenset()))
        if not evidence["matched"]:
            evidence["reason"] = "live pod state does not match diagnosed fault type"
        return evidence

    @staticmethod
    def _label_selector(deployment: Dict[str, Any]) -> str:
        selector = ((deployment.get("spec") or {}).get("selector") or {})
        labels = selector.get("match_labels") or selector.get("matchLabels") or {}
        if not isinstance(labels, dict) or not labels:
            return ""
        return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))

    @staticmethod
    def _pod_signals(pods: List[Any]) -> set:
        signals: set = set()
        for pod in pods:
            if not isinstance(pod, dict):
                continue
            status = pod.get("status") or {}
            conditions = status.get("conditions") or []
            not_ready = any(
                isinstance(c, dict) and c.get("type") == "Ready" and str(c.get("status")) != "True" for c in conditions
            )
            if any(
                isinstance(c, dict) and c.get("type") == "PodScheduled" and c.get("reason") == "Unschedulable" for c in conditions
            ):
                signals.add("unschedulable")
            for cs in status.get("container_statuses") or status.get("containerStatuses") or []:
                if not isinstance(cs, dict):
                    continue
                waiting = ((cs.get("state") or {}).get("waiting") or {})
                waiting_reason = waiting.get("reason")
                restarts = cs.get("restart_count", cs.get("restartCount")) or 0
                restarts = restarts if isinstance(restarts, int) and not isinstance(restarts, bool) else 0
                terminated = ((cs.get("last_state") or cs.get("lastState") or {}).get("terminated") or {})
                if waiting_reason == "CrashLoopBackOff":
                    signals.add("crash_loop_backoff")
                if waiting_reason in _IMAGE_PULL_REASONS:
                    signals.add("image_pull_backoff")
                if restarts >= HIGH_RESTART_COUNT:
                    signals.add("high_restart_count")
                if not_ready and restarts >= 1:
                    signals.add("not_ready_after_restart")
                if terminated.get("reason") == "OOMKilled":
                    signals.add("oom_killed")
        return signals

    def _classify_fault(self, diagnosis_result: Dict[str, Any]) -> str:
        text = self._diagnosis_text(diagnosis_result)
        if any(keyword in text for keyword in ["imagepullbackoff", "errimagepull", "failed to pull image", "back-off pulling image", "pull access denied"]):
            return "image_pull_failure"
        if any(keyword in text for keyword in ["crashloopbackoff", "restarting failed container", "back-off restarting failed container"]):
            return "crash_loop_backoff"
        if any(keyword in text for keyword in ["oomkilled", "insufficient cpu", "insufficient memory", "failedscheduling", "0/", "资源不足", "内存不足", "cpu不足"]):
            return "resource_insufficient"
        return "unknown"

    def _diagnosis_text(self, diagnosis_result: Dict[str, Any]) -> str:
        chunks: List[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, str):
                chunks.append(value)
            elif isinstance(value, dict):
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(diagnosis_result)
        return " ".join(chunks).lower()

    def _generate_candidate_actions(
        self,
        fault_type: str,
        target: Dict[str, str],
        deployment: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        generators = {
            "crash_loop_backoff": self._crashloop_actions,
            "resource_insufficient": self._resource_actions,
            "image_pull_failure": self._image_pull_actions,
        }
        return generators.get(fault_type, self._unknown_actions)(target, deployment)

    def _crashloop_actions(self, target: Dict[str, str], deployment: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            self._action("inspect-logs", "inspect_logs", target, "查看Pod最近日志，确认进程退出原因", executable=False),
            self._action("inspect-configuration", "inspect_configuration", target, "检查环境变量、挂载配置和探针配置", executable=False),
            self._action("restart-deployment", "restart_deployment", target, "滚动重启Deployment以恢复异常Pod", rollback_available=True),
        ]

    def _resource_actions(self, target: Dict[str, str], deployment: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        current_replicas = self._replicas(deployment)
        container_name = self._main_container_name(deployment, target["deployment"])
        return [
            self._action("inspect-resource-quota", "inspect_resource_quota", target, "检查ResourceQuota、LimitRange和节点剩余资源", executable=False),
            self._action(
                "scale-deployment",
                "scale_deployment",
                target,
                "资源不足时扩容一个副本以分摊负载",
                parameters={"replicas": current_replicas + 1},
                rollback_available=True,
            ),
            self._action(
                "adjust-resource-requests",
                "adjust_resource_requests",
                target,
                "按保守值调整容器requests，减少调度失败概率",
                parameters={"container": container_name, "cpu": "200m", "memory": "256Mi"},
                rollback_available=True,
            ),
        ]

    def _image_pull_actions(self, target: Dict[str, str], deployment: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        container_name = self._main_container_name(deployment, target["deployment"])
        return [
            self._action("check-image-reference", "check_image_reference", target, "检查镜像地址、tag和仓库权限", executable=False),
            self._action("check-image-pull-secret", "check_image_pull_secret", target, "检查imagePullSecrets和Secret可用性", executable=False),
            self._action("check-network-connectivity", "check_network_connectivity", target, "检查节点到镜像仓库的网络连通性", executable=False),
            self._action(
                "patch-image-pull-policy",
                "patch_image_pull_policy",
                target,
                "将目标容器imagePullPolicy调整为IfNotPresent，避免重复拉取已缓存镜像",
                parameters={"container": container_name, "imagePullPolicy": "IfNotPresent"},
                rollback_available=True,
            ),
        ]

    def _unknown_actions(self, target: Dict[str, str], deployment: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            self._action("inspect-events", "inspect_events", target, "查看Kubernetes事件以补充诊断", executable=False),
            self._action("inspect-logs", "inspect_logs", target, "查看Pod日志以补充诊断", executable=False),
        ]

    def _action(
        self,
        action_id: str,
        action_type: str,
        target: Dict[str, str],
        description: str,
        executable: bool = True,
        parameters: Optional[Dict[str, Any]] = None,
        rollback_available: bool = False,
    ) -> Dict[str, Any]:
        return {
            "action_id": action_id,
            "action_type": action_type,
            "description": description,
            "target": {
                "kind": "Deployment",
                "name": target["deployment"],
                "namespace": target["namespace"],
            },
            "parameters": parameters or {},
            "executable": executable,
            "rollback_available": rollback_available,
        }

    async def _assess_risk(
        self,
        action: Dict[str, Any],
        target: Dict[str, str],
        deployment: Optional[Dict[str, Any]],
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        reasons: List[str] = []
        resource_exists = deployment is not None
        namespace_allowed = self._namespace_allowed(target["namespace"])
        permission_allowed = await self._permission_allowed(action, target["namespace"])
        rollback_available = bool(action.get("rollback_available"))
        affects_multiple_replicas = self._affects_multiple_replicas(action, deployment)

        if not resource_exists:
            reasons.append("resource missing")
        if not namespace_allowed:
            reasons.append("namespace not allowed")
        if permission_allowed is not True:
            reasons.append("permission denied" if permission_allowed is False else "permission unavailable")
        if affects_multiple_replicas:
            reasons.append("multiple replicas")
        if not rollback_available:
            reasons.append("rollback unavailable")

        risk_level = "low"
        requires_human_confirmation = False
        if not resource_exists or not namespace_allowed or permission_allowed is not True or not rollback_available:
            risk_level = "high"
            requires_human_confirmation = True
        elif affects_multiple_replicas:
            risk_level = "high"
            requires_human_confirmation = True
        elif ACTION_RISK_GRADES.get(str(action.get("action_type"))) == "medium":
            risk_level = "medium"

        live_evidence = deepcopy(evidence) if evidence else {"source": "none", "checked": False, "matched": False}
        if not requires_human_confirmation and not self.auto_execute:
            # SEC-26：默认只出计划，风险可控的动作也必须经 /workflow/confirm 显式确认
            requires_human_confirmation = True
            reasons.append("explicit confirmation required")
        elif not requires_human_confirmation:
            # SEC-44 G1/G6：自动执行只放行低风险白名单动作，且必须有与故障类型匹配的实时证据
            refusal = auto_execution_refusal(
                {"action_type": action.get("action_type"), "risk_assessment": {"risk_level": risk_level, "live_evidence": live_evidence}}
            )
            if refusal:
                requires_human_confirmation = True
                reasons.append(refusal)

        allowed = not requires_human_confirmation and risk_level in {"low", "medium"}
        return {
            "allowed": allowed,
            "risk_level": risk_level,
            "resource_exists": resource_exists,
            "permission_allowed": permission_allowed,
            "namespace_allowed": namespace_allowed,
            "affects_multiple_replicas": affects_multiple_replicas,
            "rollback_available": rollback_available,
            "requires_human_confirmation": requires_human_confirmation,
            "reasons": reasons,
            "live_evidence": live_evidence,
        }

    async def _permission_allowed(self, action: Dict[str, Any], namespace: str) -> Optional[bool]:
        if not hasattr(self.k8s_client, "can_i"):
            return None

        verb = "patch"
        resource = "deployments"
        if action.get("action_type") in {"inspect_events", "inspect_logs", "inspect_configuration", "check_image_reference", "check_image_pull_secret", "check_network_connectivity"}:
            verb = "get"
            resource = "pods"

        try:
            return bool(await self.k8s_client.can_i(verb=verb, resource=resource, namespace=namespace))
        except TypeError:
            return bool(await self.k8s_client.can_i(verb, resource, namespace))
        except Exception as exc:
            # 只记录 action_id、异常类型、HTTP status 和 reason：ApiException 的 body/headers 可能含集群内部信息，不写入日志
            summary = api_error_summary(exc)
            logger.warning(
                "权限检查失败: action_id=%s, 错误类型=%s, status=%s, reason=%s",
                action.get("action_id"), summary["type"], summary["status"], summary["reason"],
            )
            return False

    def _namespace_allowed(self, namespace: str) -> bool:
        if not namespace or namespace in self.restricted_namespaces:
            return False
        # 未配置 allowed_namespaces 时 fail-closed
        return namespace in self.allowed_namespaces

    def with_policy(self, policy: "AutoFixRemediationEngine") -> "AutoFixRemediationEngine":
        """返回按 policy 覆盖命名空间策略的局部副本，不修改 self（共享单例）。

        覆盖 allowed_namespaces 与 auto_execute；受限命名空间取两者并集（只会更严）。
        k8s_client 等其它属性沿用 self。
        """
        local = copy.copy(self)
        local.allowed_namespaces = set(policy.allowed_namespaces)
        local.auto_execute = bool(policy.auto_execute)
        local.restricted_namespaces = set(policy.restricted_namespaces) | set(self.restricted_namespaces)
        return local

    def namespace_refusal(self, namespace: str, actions: List[Dict[str, Any]]) -> Optional[str]:
        """执行前硬校验：计划及每个动作的目标命名空间都必须在白名单内（公开接口，见 RemediationEngine）"""
        targets = {namespace}
        for action in actions:
            target_ns = (action.get("target") or {}).get("namespace")
            if target_ns:
                targets.add(target_ns)
        if len(targets) > 1:
            return "动作目标命名空间与计划不一致，拒绝执行"
        if not self._namespace_allowed(namespace):
            if not self.allowed_namespaces:
                return "未配置 autofix.allowed_namespaces，自动修复不允许在任何命名空间执行"
            return f"命名空间 {namespace} 不在 autofix.allowed_namespaces 白名单内，拒绝执行"
        return None

    def _namespace_refusal(self, namespace: str, actions: List[Dict[str, Any]]) -> Optional[str]:
        """兼容别名：保留旧私有名，统一走公开的 namespace_refusal"""
        return self.namespace_refusal(namespace, actions)

    def _affects_multiple_replicas(self, action: Dict[str, Any], deployment: Optional[Dict[str, Any]]) -> bool:
        if action.get("action_type") not in {"restart_deployment", "scale_deployment", "adjust_resource_requests"}:
            return False
        return self._replicas(deployment) > self.max_auto_replicas

    def _replicas(self, deployment: Optional[Dict[str, Any]]) -> int:
        if not deployment:
            return 0
        return int((deployment.get("spec") or {}).get("replicas") or 1)

    def _main_container_name(self, deployment: Optional[Dict[str, Any]], fallback: str) -> str:
        containers = (((deployment or {}).get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []
        if containers:
            return containers[0].get("name") or fallback
        return fallback

    def _main_container(self, deployment: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        containers = (((deployment or {}).get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []
        return containers[0] if containers else {}

    async def _execute_action(
        self,
        action: Dict[str, Any],
        deployment: str,
        namespace: str,
        dry_run: bool = False,
        *,
        auto_executed: bool = True,
    ) -> Dict[str, Any]:
        started_at = datetime.now().isoformat()
        record = {
            "action_id": action.get("action_id"),
            "action_type": action.get("action_type"),
            "status": "skipped" if dry_run else "running",
            "risk_assessment": action.get("risk_assessment", {}),
            "started_at": started_at,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        if dry_run:
            record["finished_at"] = datetime.now().isoformat()
            record["result"] = {"dry_run": True}
            return record

        action_type = action.get("action_type")
        audit_id = new_audit_id()

        def audit(phase: str, **fields: Any) -> None:
            emit_audit(
                build_audit_record(
                    phase,
                    audit_id=audit_id,
                    namespace=namespace,
                    name=deployment,
                    action=action,
                    auto_executed=auto_executed,
                    **fields,
                )
            )

        # SEC-44 G2：实际写入口按当前配置再过一次写操作门禁；不放行即不写集群
        refusal = self._mutation_refusal(namespace, str(action_type or ""), auto_executed)
        if refusal is not None:
            record.update({"status": "refused", "refusal": "mutation_gate", "error": refusal[1], "finished_at": datetime.now().isoformat()})
            try:
                audit(PHASE_REFUSED, result="refused", reason=refusal[0])
            except Exception as exc:
                logger.warning("拒绝审计写入失败: 错误类型=%s", type(exc).__name__)
            return record

        # SEC-44 G4：先写意图审计，失败则不执行
        try:
            audit(PHASE_INTENT, result="pending")
        except Exception as exc:
            logger.error("意图审计写入失败，未执行写操作: 错误类型=%s", type(exc).__name__)
            record.update({"status": "refused", "refusal": "audit", "error": "审计记录写入失败，未执行写操作", "finished_at": datetime.now().isoformat()})
            return record

        error_type: Optional[str] = None
        try:
            if action_type == "restart_deployment":
                result = await self.k8s_client.restart_deployment(deployment, namespace)
            elif action_type == "scale_deployment":
                result = await self.k8s_client.scale_deployment(deployment, int(action.get("parameters", {}).get("replicas", 1)), namespace)
            elif action_type == "patch_image_pull_policy":
                patch = self._image_pull_policy_patch(action)
                result = await self.k8s_client.patch_deployment(deployment, patch, namespace)
            elif action_type == "adjust_resource_requests":
                patch = self._resource_requests_patch(action)
                result = await self.k8s_client.patch_deployment(deployment, patch, namespace)
            else:
                raise ValueError(f"不支持的可执行动作: {action_type}")

            record["status"] = "succeeded" if result else "failed"
            record["result"] = {"ok": bool(result)}
        except Exception as exc:
            error_type = type(exc).__name__
            record["status"] = "failed"
            record["error"] = f"Kubernetes动作未确认成功（{error_type}），请核对实际状态"
        finally:
            record["finished_at"] = datetime.now().isoformat()
        try:
            audit(PHASE_RESULT, result=record["status"], error_type=error_type)
        except Exception as exc:
            # 写操作已提交，无法回退：标记审计缺失，由调用方/运维核对
            logger.error("结果审计写入失败: 错误类型=%s, audit_id=%s", type(exc).__name__, audit_id)
            record["audit_result_recorded"] = False
        return record

    @staticmethod
    def _mutation_refusal(namespace: str, operation: str, auto_executed: bool) -> Optional[tuple]:
        """调用 policy.ensure_mutation_allowed；返回 (原因代码, 提示) 或 None。门禁自身异常同样按拒绝处理（fail-closed）。"""
        from app.common.exceptions import FeatureDisabledError, NamespaceNotAllowedError

        from .policy import ensure_mutation_allowed

        try:
            ensure_mutation_allowed(
                namespace,
                operation,
                require_repair_enabled=False,
                require_auto_execute=auto_executed,
            )
        except FeatureDisabledError as exc:
            switch = str((exc.details or {}).get("switch") or "")
            return ("switch disabled", f"写操作门禁未放行（{switch} 已关闭），未写入集群")
        except NamespaceNotAllowedError:
            return ("namespace not allowed", f"写操作门禁未放行（命名空间 {namespace} 不在当前允许范围内），未写入集群")
        except Exception as exc:
            logger.error("写操作门禁检查异常，按拒绝处理: 错误类型=%s", type(exc).__name__)
            return ("gate error", "写操作门禁检查异常，未写入集群")
        return None

    def _image_pull_policy_patch(self, action: Dict[str, Any]) -> Dict[str, Any]:
        params = action.get("parameters", {})
        return {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": params.get("container") or action.get("target", {}).get("name"),
                                "imagePullPolicy": params.get("imagePullPolicy", "IfNotPresent"),
                            }
                        ]
                    }
                }
            }
        }

    def _resource_requests_patch(self, action: Dict[str, Any]) -> Dict[str, Any]:
        params = action.get("parameters", {})
        container = params.get("container") or action.get("target", {}).get("name")
        return {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container,
                                "resources": {
                                    "requests": {
                                        "cpu": params.get("cpu", "200m"),
                                        "memory": params.get("memory", "256Mi"),
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        }

    async def _post_check(self, deployment: str, namespace: str) -> Optional[Dict[str, Any]]:
        if hasattr(self.k8s_client, "wait_for_deployment_rollout"):
            try:
                return await self.k8s_client.wait_for_deployment_rollout(deployment, namespace, timeout_seconds=self.rollout_timeout_seconds)
            except Exception as exc:
                return {"ready": False, "error": f"就绪检查失败（{type(exc).__name__}）"}
        if hasattr(self.k8s_client, "get_deployment_status"):
            try:
                return await self.k8s_client.get_deployment_status(deployment, namespace)
            except Exception as exc:
                return {"ready": False, "error": f"状态检查失败（{type(exc).__name__}）"}
        return None

    def _summarize_plan(self, fault_type: str, actions: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "fault_type": fault_type,
            "total_actions": len(actions),
            "executable_actions": len([a for a in actions if a.get("executable")]),
            "allowed_actions": len([a for a in actions if a.get("risk_assessment", {}).get("allowed")]),
            "blocked_actions": len([a for a in actions if a.get("executable") and not a.get("risk_assessment", {}).get("allowed")]),
        }
