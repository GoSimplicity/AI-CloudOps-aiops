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

from copy import deepcopy
from datetime import datetime
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("aiops.autofix.remediation")


class AutoFixRemediationEngine:
    """将诊断结果转换为修复计划，并只执行风险可控的动作。"""

    DEFAULT_RESTRICTED_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}

    def __init__(
        self,
        k8s_client: Any,
        allowed_namespaces: Optional[List[str]] = None,
        restricted_namespaces: Optional[List[str]] = None,
        max_auto_replicas: int = 1,
        rollout_timeout_seconds: int = 120,
    ) -> None:
        self.k8s_client = k8s_client
        self.allowed_namespaces = set(allowed_namespaces or [])
        self.restricted_namespaces = set(restricted_namespaces or self.DEFAULT_RESTRICTED_NAMESPACES)
        self.max_auto_replicas = max(1, max_auto_replicas)
        self.rollout_timeout_seconds = rollout_timeout_seconds

    async def build_plan(self, diagnosis_result: Dict[str, Any]) -> Dict[str, Any]:
        """根据诊断结果生成候选修复动作并完成执行前风险评估。"""
        target = self._extract_target(diagnosis_result)
        deployment = await self._get_deployment(target["deployment"], target["namespace"])
        fault_type = self._classify_fault(diagnosis_result)
        candidate_actions = self._generate_candidate_actions(fault_type, target, deployment)

        assessed_actions = []
        for action in candidate_actions:
            action = deepcopy(action)
            if action.get("executable"):
                action["risk_assessment"] = await self._assess_risk(action, target, deployment)
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

    async def execute_plan(self, plan: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
        """执行计划中风险可控的动作，并记录执行结果和事后状态检查。"""
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
        for action in actions:
            execution_record = await self._execute_action(action, deployment, namespace, dry_run=dry_run)
            executed_actions.append(execution_record)

        post_check = None
        if not dry_run and executed_actions:
            post_check = await self._post_check(deployment, namespace)

        failed = [record for record in executed_actions if record.get("status") == "failed"]
        return {
            "status": "failed" if failed else "completed",
            "deployment": deployment,
            "namespace": namespace,
            "fault_type": plan.get("fault_type", "unknown"),
            "executed_actions": executed_actions,
            "blocked_actions": blocked_actions,
            "post_check": post_check,
            "dry_run": dry_run,
            "timestamp": datetime.now().isoformat(),
        }

    async def plan_and_execute(self, diagnosis_result: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
        """一站式生成计划并执行允许范围内的动作。"""
        plan = await self.build_plan(diagnosis_result)
        result = await self.execute_plan(plan, dry_run=dry_run)
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
            logger.warning("获取Deployment失败，将按资源不存在评估风险: %s", exc)
            return None

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
        if permission_allowed is False:
            reasons.append("permission denied")
        if affects_multiple_replicas:
            reasons.append("multiple replicas")
        if not rollback_available:
            reasons.append("rollback unavailable")

        risk_level = "low"
        requires_human_confirmation = False
        if not resource_exists or not namespace_allowed or permission_allowed is False or not rollback_available:
            risk_level = "high"
            requires_human_confirmation = True
        elif affects_multiple_replicas:
            risk_level = "high"
            requires_human_confirmation = True
        elif action.get("action_type") in {"scale_deployment", "adjust_resource_requests"}:
            risk_level = "medium"

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
            logger.warning("权限检查失败: %s", exc)
            return False

    def _namespace_allowed(self, namespace: str) -> bool:
        if namespace in self.restricted_namespaces:
            return False
        if self.allowed_namespaces and namespace not in self.allowed_namespaces:
            return False
        return True

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

    async def _execute_action(self, action: Dict[str, Any], deployment: str, namespace: str, dry_run: bool = False) -> Dict[str, Any]:
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

        try:
            action_type = action.get("action_type")
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
            record["status"] = "failed"
            record["error"] = str(exc)
        finally:
            record["finished_at"] = datetime.now().isoformat()
        return record

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
                return {"ready": False, "error": str(exc)}
        if hasattr(self.k8s_client, "get_deployment_status"):
            try:
                return await self.k8s_client.get_deployment_status(deployment, namespace)
            except Exception as exc:
                return {"ready": False, "error": str(exc)}
        return None

    def _summarize_plan(self, fault_type: str, actions: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "fault_type": fault_type,
            "total_actions": len(actions),
            "executable_actions": len([a for a in actions if a.get("executable")]),
            "allowed_actions": len([a for a in actions if a.get("risk_assessment", {}).get("allowed")]),
            "blocked_actions": len([a for a in actions if a.get("executable") and not a.get("risk_assessment", {}).get("allowed")]),
        }
