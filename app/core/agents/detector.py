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
from typing import Dict, Any, List
from datetime import datetime, timedelta
from app.services.kubernetes import KubernetesService
from app.services.prometheus import PrometheusService

logger = logging.getLogger("aiops.detector")


class K8sDetectorAgent:
    """Kubernetes错误检测Agent"""

    def __init__(self):
        self.k8s_service = KubernetesService()
        self.prometheus_service = PrometheusService()
        self.detection_rules = self._load_detection_rules()

    def _load_detection_rules(self) -> Dict[str, Any]:
        """加载检测规则"""
        return {
            "pod_issues": {
                "crash_loop": {
                    "condition": lambda pod: self._has_crash_loop(pod),
                    "severity": "critical",
                    "auto_fix": True,
                },
                "image_pull_error": {
                    "condition": lambda pod: self._has_image_pull_error(pod),
                    "severity": "high",
                    "auto_fix": False,
                },
                "resource_pressure": {
                    "condition": lambda pod: self._has_resource_pressure(pod),
                    "severity": "medium",
                    "auto_fix": True,
                },
                "pending_timeout": {
                    "condition": lambda pod: self._is_pending_timeout(pod),
                    "severity": "medium",
                    "auto_fix": True,
                },
            },
            "deployment_issues": {
                "replica_mismatch": {
                    "condition": lambda deploy: self._has_replica_mismatch(deploy),
                    "severity": "high",
                    "auto_fix": True,
                },
                "unavailable_replicas": {
                    "condition": lambda deploy: self._has_unavailable_replicas(deploy),
                    "severity": "critical",
                    "auto_fix": True,
                },
            },
            "service_issues": {
                "no_endpoints": {
                    "condition": lambda svc: self._has_no_endpoints(svc),
                    "severity": "high",
                    "auto_fix": True,
                }
            },
        }

    async def detect_all_issues(self, namespace: str = None) -> Dict[str, Any]:
        """检测所有类型的问题"""
        try:
            namespace = namespace or "default"

            # 获取所有资源
            deployments = await self.k8s_service.get_deployments(namespace) or []
            pods = await self.k8s_service.get_pods(namespace) or []
            services = await self.k8s_service.get_services(namespace) or []

            issues = {
                "timestamp": datetime.now().isoformat(),
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
            return {"error": str(e), "timestamp": datetime.now().isoformat()}

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
                "timestamp": datetime.now().isoformat(),
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
                                    "timestamp": datetime.now().isoformat(),
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
        issues = []

        for deployment in deployments or []:
            try:
                for issue_type, rule in self.detection_rules[
                    "deployment_issues"
                ].items():
                    try:
                        if rule["condition"](deployment):
                            issues.append(
                                {
                                    "type": "deployment_issue",
                                    "sub_type": issue_type,
                                    "severity": rule["severity"],
                                    "auto_fix": rule["auto_fix"],
                                    "resource_name": deployment.get("metadata", {}).get(
                                        "name"
                                    ),
                                    "namespace": deployment.get("metadata", {}).get(
                                        "namespace"
                                    ),
                                    "message": self._get_deployment_message(
                                        deployment, issue_type
                                    ),
                                    "details": self._get_deployment_details(deployment),
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )
                    except Exception as e:
                        logger.warning(f"检测部署问题失败: {str(e)}")
                        continue
            except Exception as e:
                logger.warning(f"处理部署数据失败: {str(e)}")
                continue

        return issues

    def _detect_service_issues_sync(
        self, services: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """检测Service问题"""
        issues = []

        for service in services or []:
            try:
                for issue_type, rule in self.detection_rules["service_issues"].items():
                    try:
                        if rule["condition"](service):
                            issues.append(
                                {
                                    "type": "service_issue",
                                    "sub_type": issue_type,
                                    "severity": rule["severity"],
                                    "auto_fix": rule["auto_fix"],
                                    "resource_name": service.get("metadata", {}).get(
                                        "name"
                                    ),
                                    "namespace": service.get("metadata", {}).get(
                                        "namespace"
                                    ),
                                    "message": self._get_service_message(
                                        service, issue_type
                                    ),
                                    "details": self._get_service_details(service),
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )
                    except Exception as e:
                        logger.warning(
                            f"检测Service问题类型 {issue_type} 失败: {str(e)}"
                        )
                        continue
            except Exception as e:
                logger.warning(f"处理Service数据失败: {str(e)}")
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
            return datetime.now(creation_dt.tzinfo) - creation_dt > timedelta(minutes=5)
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
        """检查Service是否有Endpoints"""
        # 这里需要获取Endpoints信息
        # 简化版本：检查selector是否匹配到Pod
        selector = service.get("spec", {}).get("selector", {})
        return len(selector) == 0  # 修复逻辑：无selector表示无endpoints

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

    def _get_issue_message(self, pod: Dict[str, Any], issue_type: str) -> str:
        """获取问题描述消息"""
        pod_name = pod.get("metadata", {}).get("name", "unknown")

        messages = {
            "crash_loop": f"Pod {pod_name} 处于CrashLoopBackOff状态",
            "image_pull_error": f"Pod {pod_name} 镜像拉取失败",
            "resource_pressure": f"Pod {pod_name} 因资源压力无法调度",
            "pending_timeout": f"Pod {pod_name} 长时间处于Pending状态",
        }

        return messages.get(issue_type, f"Pod {pod_name} 出现问题")

    def _get_deployment_message(
        self, deployment: Dict[str, Any], issue_type: str
    ) -> str:
        """获取部署问题描述"""
        name = deployment.get("metadata", {}).get("name", "unknown")
        messages = {
            "replica_mismatch": f"部署 {name} 副本数不匹配",
            "unavailable_replicas": f"部署 {name} 有不可用副本",
        }
        return messages.get(issue_type, f"部署 {name} 出现问题")

    def _get_service_message(self, service: Dict[str, Any], issue_type: str) -> str:
        """获取服务问题描述"""
        name = service.get("metadata", {}).get("name", "unknown")
        messages = {"no_endpoints": f"服务 {name} 没有可用的Endpoints"}
        return messages.get(issue_type, f"服务 {name} 出现问题")

    def _get_pod_details(self, pod: Dict[str, Any]) -> Dict[str, Any]:
        """获取Pod详细信息"""
        return {
            "name": pod.get("metadata", {}).get("name"),
            "namespace": pod.get("metadata", {}).get("namespace"),
            "phase": pod.get("status", {}).get("phase"),
            "restart_count": sum(
                cs.get("restart_count", 0)
                for cs in pod.get("status", {}).get("container_statuses", [])
            ),
            "conditions": [
                {
                    "type": c.get("type"),
                    "status": c.get("status"),
                    "reason": c.get("reason"),
                }
                for c in pod.get("status", {}).get("conditions", [])
            ],
        }

    def _get_deployment_details(self, deployment: Dict[str, Any]) -> Dict[str, Any]:
        """获取部署详细信息"""
        return {
            "name": deployment.get("metadata", {}).get("name"),
            "namespace": deployment.get("metadata", {}).get("namespace"),
            "replicas": {
                "desired": deployment.get("spec", {}).get("replicas", 0),
                "available": deployment.get("status", {}).get("available_replicas", 0),
                "ready": deployment.get("status", {}).get("ready_replicas", 0),
            },
        }

    def _get_service_details(self, service: Dict[str, Any]) -> Dict[str, Any]:
        """获取服务详细信息"""
        return {
            "name": service.get("metadata", {}).get("name"),
            "namespace": service.get("metadata", {}).get("namespace"),
            "type": service.get("spec", {}).get("type"),
            "selector": service.get("spec", {}).get("selector", {}),
            "ports": service.get("spec", {}).get("ports", []),
        }

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
                "timestamp": datetime.now().isoformat(),
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
