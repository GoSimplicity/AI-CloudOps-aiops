#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
K8s错误检测Agent - 专门负责检测Kubernetes集群中的各种问题
Author: AI Assistant
License: Apache 2.0
Description: 基于真实K8s API的集群状态检测和问题识别Agent
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from app.core.agents.detector_helpers import DetectorHelpers
from app.core.agents.detector_rules import DetectionRules
from app.services.kubernetes import KubernetesService
from app.services.prometheus import PrometheusService
from app.utils.time_utils import iso_utc_now

logger = logging.getLogger("aiops.detector")


class K8sDetectorAgent:
    """Kubernetes错误检测Agent"""

    def __init__(self):
        self.k8s_service = KubernetesService()
        self.prometheus_service = PrometheusService()
        self.detection_rules = self._load_detection_rules()
        # 保存当前扫描周期内的 Pod 列表，供 Service 端点检查使用
        self._current_pods: List[Dict[str, Any]] = []

    def _load_detection_rules(self) -> Dict[str, Any]:
        """加载检测规则"""
        rules = DetectionRules.get_all_rules()
        
        # 为规则添加检测条件函数
        rules["pod_issues"]["crash_loop"]["condition"] = self._has_crash_loop
        rules["pod_issues"]["image_pull_error"]["condition"] = self._has_image_pull_error
        rules["pod_issues"]["mount_failure"]["condition"] = self._has_mount_failure
        rules["pod_issues"]["resource_pressure"]["condition"] = self._has_resource_pressure
        rules["pod_issues"]["pending_timeout"]["condition"] = self._is_pending_timeout
        
        rules["deployment_issues"]["replica_mismatch"]["condition"] = self._has_replica_mismatch
        rules["deployment_issues"]["unavailable_replicas"]["condition"] = self._has_unavailable_replicas
        
        rules["service_issues"]["no_endpoints"]["condition"] = self._has_no_endpoints
        
        return rules

    async def detect_all_issues(self, namespace: str = None) -> Dict[str, Any]:
        """检测所有类型的问题"""
        try:
            namespace = namespace or "default"

            # 获取所有资源
            deployments = await self.k8s_service.get_deployments(namespace) or []
            pods = await self.k8s_service.get_pods(namespace) or []
            services = await self.k8s_service.get_services(namespace) or []

            issues = {
                "timestamp": iso_utc_now(),
                "namespace": namespace,
                "summary": {
                    "total_issues": 0,
                    "critical": 0,
                    "high": 0,
                    "medium": 0,
                    "low": 0,
                },
                "details": [],
            }

            # 检测Pod问题
            pod_issues = await self._detect_pod_issues(pods)
            issues["details"].extend(pod_issues)

            # 检测Deployment问题
            deployment_issues = self._detect_deployment_issues_sync(deployments)
            issues["details"].extend(deployment_issues)

            # 检测Service问题
            # 将本轮 Pod 缓存，供 Service 端点匹配逻辑使用
            self._current_pods = pods
            service_issues = self._detect_service_issues_sync(services)
            issues["details"].extend(service_issues)

            # 汇总统计
            for issue in issues["details"]:
                severity = issue["severity"]
                issues["summary"][severity] += 1
                issues["summary"]["total_issues"] += 1

            logger.info(
                f"检测到 {issues['summary']['total_issues']} 个问题在命名空间 {namespace}"
            )
            return issues

        except Exception as e:
            logger.error(f"检测问题失败: {str(e)}")
            return {"error": str(e), "timestamp": iso_utc_now()}

    async def detect_deployment_issues(
        self, deployment_name: str, namespace: str
    ) -> Dict[str, Any]:
        """检测特定部署的问题"""
        try:
            deployment = await self.k8s_service.get_deployment(
                deployment_name, namespace
            )
            if not deployment:
                return {"error": f"未找到部署: {deployment_name}"}

            pods = await self.k8s_service.get_pods(
                namespace=namespace, label_selector=f"app={deployment_name}"
            )

            issues = {
                "deployment": deployment_name,
                "namespace": namespace,
                "timestamp": iso_utc_now(),
                "issues": [],
            }

            # 检测部署本身的问题
            deployment_issues = await self._check_deployment_health(deployment)
            issues["issues"].extend(deployment_issues)

            # 检测相关Pod的问题
            pod_issues = await self._detect_pod_issues(pods)
            for issue in pod_issues:
                issue["deployment"] = deployment_name
                issues["issues"].append(issue)

            return issues

        except Exception as e:
            logger.error(f"检测部署问题失败: {str(e)}")
            return {"error": str(e)}

    async def _detect_pod_issues(
        self, pods: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """检测Pod问题"""
        issues = []

        for pod in pods or []:
            try:
                for issue_type, rule in self.detection_rules["pod_issues"].items():
                    try:
                        if rule["condition"](pod):
                            issues.append(
                                {
                                    "type": "pod_issue",
                                    "sub_type": issue_type,
                                    "severity": rule["severity"],
                                    "auto_fix": rule["auto_fix"],
                                    "resource_name": pod.get("metadata", {}).get(
                                        "name"
                                    ),
                                    "namespace": pod.get("metadata", {}).get(
                                        "namespace"
                                    ),
                                    "message": self._get_issue_message(pod, issue_type),
                                    "details": self._get_pod_details(pod),
                                    "timestamp": iso_utc_now(),
                                }
                            )
                    except Exception as e:
                        logger.warning(f"检测Pod问题类型 {issue_type} 失败: {str(e)}")
                        continue
            except Exception as e:
                logger.warning(f"处理Pod数据失败: {str(e)}")
                continue

        return issues

    def _detect_deployment_issues_sync(
        self, deployments: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """检测Deployment问题"""
        return self._detect_resource_issues(
            resources=deployments,
            resource_type="deployment",
            rules_key="deployment_issues",
            message_func=DetectorHelpers.get_deployment_message,
            details_func=DetectorHelpers.get_deployment_details
        )

    def _detect_service_issues_sync(
        self, services: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """检测Service问题"""
        return self._detect_resource_issues(
            resources=services,
            resource_type="service",
            rules_key="service_issues",
            message_func=DetectorHelpers.get_service_message,
            details_func=DetectorHelpers.get_service_details
        )

    def _detect_resource_issues(
        self,
        resources: List[Dict[str, Any]],
        resource_type: str,
        rules_key: str,
        message_func,
        details_func
    ) -> List[Dict[str, Any]]:
        """通用资源问题检测方法"""
        issues = []

        for resource in resources or []:
            try:
                for issue_type, rule in self.detection_rules[rules_key].items():
                    try:
                        if rule["condition"](resource):
                            issues.append({
                                "type": f"{resource_type}_issue",
                                "sub_type": issue_type,
                                "severity": rule["severity"],
                                "auto_fix": rule["auto_fix"],
                                "resource_name": resource.get("metadata", {}).get("name"),
                                "namespace": resource.get("metadata", {}).get("namespace"),
                                "message": message_func(resource, issue_type),
                                "details": details_func(resource),
                                "timestamp": iso_utc_now(),
                            })
                    except Exception as e:
                        logger.warning(f"检测{resource_type}问题类型 {issue_type} 失败: {str(e)}")
                        continue
            except Exception as e:
                logger.warning(f"处理{resource_type}数据失败: {str(e)}")
                continue

        return issues

    def _has_crash_loop(self, pod: Dict[str, Any]) -> bool:
        """检查是否有CrashLoopBackOff"""
        container_statuses = pod.get("status", {}).get("container_statuses", [])
        for status in container_statuses:
            waiting = status.get("state", {}).get("waiting", {})
            if waiting.get("reason") == "CrashLoopBackOff":
                return True
        return False

    def _has_image_pull_error(self, pod: Dict[str, Any]) -> bool:
        """检查是否有镜像拉取错误"""
        container_statuses = pod.get("status", {}).get("container_statuses", [])
        for status in container_statuses:
            waiting = status.get("state", {}).get("waiting", {})
            if waiting.get("reason") in ["ImagePullBackOff", "ErrImagePull"]:
                return True
        return False

    def _has_mount_failure(self, pod: Dict[str, Any]) -> bool:
        """检查是否有卷挂载失败/配置错误。

        采用保守启发：
        - 容器 state.waiting.reason 为 CreateContainerConfigError/ContainerCreating 且 message 含 mount/volume/MountVolume
        - PodScheduled 条件 Unschedulable 且 message 含 volume/persistentvolumeclaim
        """
        status = pod.get("status", {}) or {}
        # 检查容器等待态的 message 提示
        for cs in status.get("container_statuses", []) or []:
            waiting = (cs.get("state", {}) or {}).get("waiting", {}) or {}
            reason = (waiting.get("reason") or "").lower()
            message = (waiting.get("message") or "").lower()
            if reason in ("createcontainerconfigerror", "containercreating") and (
                "mount" in message or "volume" in message or "mountvolume" in message
            ):
                return True
        # 检查条件中的 Unschedulable 提示
        for cond in status.get("conditions", []) or []:
            if (cond.get("type") == "PodScheduled" and cond.get("reason") == "Unschedulable"):
                msg = (cond.get("message") or "").lower()
                if "volume" in msg or "persistentvolumeclaim" in msg or "pvc" in msg:
                    return True
        return False

    def _has_resource_pressure(self, pod: Dict[str, Any]) -> bool:
        """检查是否有资源压力"""
        conditions = pod.get("status", {}).get("conditions", [])
        for condition in conditions:
            if (
                condition.get("type") == "PodScheduled"
                and condition.get("reason") == "Unschedulable"
            ):
                return "Insufficient" in condition.get("message", "")
        return False

    def _is_pending_timeout(self, pod: Dict[str, Any]) -> bool:
        """检查是否Pending超时"""
        phase = pod.get("status", {}).get("phase")
        if phase != "Pending":
            return False

        creation_time = pod.get("metadata", {}).get("creation_timestamp")
        if creation_time:
            creation_dt = datetime.fromisoformat(creation_time.replace("Z", "+00:00"))
            return datetime.now(timezone.utc).replace(tzinfo=creation_dt.tzinfo) - creation_dt > timedelta(minutes=5)
        return False

    def _has_replica_mismatch(self, deployment: Dict[str, Any]) -> bool:
        """检查副本数不匹配"""
        spec = deployment.get("spec", {})
        status = deployment.get("status", {})

        desired = spec.get("replicas", 0)
        available = status.get("available_replicas", 0)

        return desired != available

    def _has_unavailable_replicas(self, deployment: Dict[str, Any]) -> bool:
        """检查不可用副本"""
        status = deployment.get("status", {})
        unavailable = status.get("unavailable_replicas", 0)
        return unavailable > 0

    def _has_no_endpoints(self, service: Dict[str, Any]) -> bool:
        """检查Service是否缺少有效端点。

        简化但更贴近实际的判定策略：
        - ExternalName 类型不参与端点判定
        - 若无 selector：视为缺少端点（常见误配），但排除 ExternalName
        - 若有 selector：与当前命名空间 Pod 标签匹配，若匹配且 Running 的 Pod 数为 0，则视为缺少端点
        """
        spec = service.get("spec", {}) or {}
        service_type = (spec.get("type") or "").upper()
        if service_type == "EXTERNALNAME":
            return False

        selector = spec.get("selector") or {}
        if not selector:
            return True

        # 基于 selector 匹配 Pod
        matched_running_pods = 0
        for pod in self._current_pods or []:
            try:
                labels = (pod.get("metadata", {}) or {}).get("labels", {}) or {}
                # 选择器要求子集匹配
                if all(labels.get(k) == v for k, v in selector.items()):
                    if (pod.get("status", {}) or {}).get("phase") == "Running":
                        matched_running_pods += 1
            except Exception:
                continue

        return matched_running_pods == 0

    async def _check_deployment_health(
        self, deployment: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """检查部署健康状态"""
        issues = []

        # 检查副本状态
        spec = deployment.get("spec", {})
        status = deployment.get("status", {})

        desired = spec.get("replicas", 0)
        available = status.get("available_replicas", 0)
        ready = status.get("ready_replicas", 0)

        if desired == 0:
            issues.append(
                {
                    "type": "deployment_issue",
                    "sub_type": "zero_replicas",
                    "severity": "warning",
                    "auto_fix": False,
                    "resource_name": deployment.get("metadata", {}).get("name"),
                    "message": "部署副本数设置为0",
                    "details": {"desired": desired},
                }
            )
        elif desired != ready:
            issues.append(
                {
                    "type": "deployment_issue",
                    "sub_type": "replica_mismatch",
                    "severity": "high",
                    "auto_fix": True,
                    "resource_name": deployment.get("metadata", {}).get("name"),
                    "message": f"副本不匹配: 期望{desired}, 实际就绪{ready}",
                    "details": {
                        "desired": desired,
                        "ready": ready,
                        "available": available,
                    },
                }
            )

        return issues



    async def get_cluster_overview(self, namespace: str = None) -> Dict[str, Any]:
        """获取集群概览信息"""
        try:
            namespace = namespace or "default"

            nodes = await self.k8s_service.get_nodes()
            deployments = await self.k8s_service.get_deployments(namespace)
            pods = await self.k8s_service.get_pods(namespace)
            services = await self.k8s_service.get_services(namespace)

            # 计算资源使用情况
            total_pods = len(pods)
            running_pods = len(
                [p for p in pods if p.get("status", {}).get("phase") == "Running"]
            )

            total_deployments = len(deployments)
            healthy_deployments = len(
                [
                    d
                    for d in deployments
                    if d.get("status", {}).get("available_replicas", 0)
                    == d.get("spec", {}).get("replicas", 0)
                ]
            )

            return {
                "timestamp": iso_utc_now(),
                "namespace": namespace,
                "nodes": len(nodes),
                "deployments": {
                    "total": total_deployments,
                    "healthy": healthy_deployments,
                },
                "pods": {"total": total_pods, "running": running_pods},
                "services": len(services),
            }

        except Exception as e:
            logger.error(f"获取集群概览失败: {str(e)}")
            return {"error": str(e)}


class Detector:
    def __init__(self):
        self.agent = K8sDetectorAgent()

    async def diagnose(self, namespace: str = "default"):
        return await self.agent.get_cluster_overview(namespace=namespace)
