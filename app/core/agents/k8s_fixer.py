#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

import logging
import os
from typing import Any, Dict, List

from app.config.settings import config
from app.core.agents.k8s_fixer_enhanced import EnhancedK8sFixerAgent
from app.services.kubernetes import KubernetesService
from app.services.llm import LLMService

logger = logging.getLogger("aiops.k8s_fixer")


class K8sFixerAgent:
    """Kubernetes集群问题诊断和自动修复代理"""

    def __init__(self):
        self.k8s_service = KubernetesService()
        self.llm_service = LLMService()
        self.llm = self.llm_service
        self.enhanced_agent = EnhancedK8sFixerAgent()  # 重用实例
        self.max_retries = 3
        self.retry_delay = 2
        logger.info("K8s Fixer Agent initialized")

    async def analyze_and_fix_deployment(
        self, deployment_name: str, namespace: str, error_description: str
    ) -> str:
        """分析并修复Deployment问题"""
        try:
            return await self.enhanced_agent.analyze_and_fix_deployment(
                deployment_name, namespace, error_description
            )
        except Exception as e:
            logger.error(f"修复失败: {str(e)}")
            return f"修复失败: {str(e)}"

    async def _check_and_fix_k8s_connection(self) -> bool:
        """检查并尝试修复K8s连接"""
        if self.k8s_service.is_healthy():
            return True

        logger.warning("Kubernetes连接不健康，尝试修复")
        possible_paths = [
            "deploy/kubernetes/config",
            "../deploy/kubernetes/config",
            "config",
            os.path.expanduser("~/.kube/config"),
        ]

        for path in possible_paths:
            if os.path.exists(path):
                try:
                    os.environ["KUBECONFIG"] = os.path.abspath(path)
                    config.k8s.config_path = os.path.abspath(path)
                    self.k8s_service._try_init()
                    if self.k8s_service.is_healthy():
                        logger.info(f"成功连接到K8s集群，使用配置: {path}")
                        return True
                except Exception as e:
                    logger.warning(f"使用配置 {path} 连接K8s失败: {str(e)}")
        return False

    async def _identify_and_fix_common_issues(
        self,
        deployment: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """识别并修复常见问题"""
        try:
            deployment_name = deployment.get("metadata", {}).get("name", "unknown")
            namespace = deployment.get("metadata", {}).get("namespace", "default")
            containers = (
                deployment.get("spec", {})
                .get("template", {})
                .get("spec", {})
                .get("containers", [])
            )

            if not containers:
                return {"fixed": False, "message": "无法找到容器配置"}

            main_container = containers[0]
            issues_found = []
            fixes_applied = []
            patch = {"spec": {"template": {"spec": {"containers": [{}]}}}}
            container_patch = patch["spec"]["template"]["spec"]["containers"][0]
            container_patch["name"] = main_container.get("name", "app")
            need_to_patch = False

            # 检查CrashLoopBackOff
            has_crash_loop = any(
                c_status.get("state", {}).get("waiting", {}).get("reason")
                == "CrashLoopBackOff"
                for pod in context.get("pods", [])
                for c_status in pod.get("status", {}).get("container_statuses", [])
            )

            # 修复资源配置
            resources = main_container.get("resources", {})
            if "requests" in resources:
                memory = resources["requests"].get("memory", "")
                if memory.endswith("Mi") and int(memory[:-2]) > 512:
                    container_patch["resources"] = {"requests": {"memory": "256Mi"}}
                    issues_found.append("内存请求过高")
                    fixes_applied.append("调整内存请求")
                    need_to_patch = True

            # 修复健康检查
            if has_crash_loop and "livenessProbe" in main_container:
                probe = main_container["livenessProbe"]
                if probe.get("httpGet", {}).get("path") != "/":
                    container_patch["livenessProbe"] = {
                        "httpGet": {"path": "/", "port": 80}
                    }
                    issues_found.append("探针路径错误")
                    fixes_applied.append("修复探针路径")
                    need_to_patch = True

            # 添加缺失的readinessProbe
            if has_crash_loop and "readinessProbe" not in main_container:
                container_patch["readinessProbe"] = {
                    "httpGet": {"path": "/", "port": 80},
                    "initialDelaySeconds": 5,
                    "periodSeconds": 10,
                }
                issues_found.append("缺少readinessProbe")
                fixes_applied.append("添加readinessProbe")
                need_to_patch = True

            if need_to_patch:
                patch_result = await self.k8s_service.patch_deployment(
                    deployment_name, patch, namespace
                )
                if patch_result:
                    return {
                        "fixed": True,
                        "message": f"修复完成: {', '.join(fixes_applied)}",
                    }
                else:
                    return {"fixed": False, "message": "应用修复失败"}
            else:
                return {"fixed": False, "message": "未发现需要修复的问题"}
        except Exception as e:
            logger.error(f"修复失败: {str(e)}")
            return {"fixed": False, "message": f"修复失败: {str(e)}"}

    async def _execute_fix(
        self, deployment_name: str, namespace: str, analysis: Dict[str, Any]
    ) -> str:
        """执行修复操作"""
        try:
            action = analysis.get("action")
            if not action:
                return "分析未提供修复操作"

            if "修改资源限制" in action or "修改资源请求" in action:
                patch = {
                    "spec": {"template": {"spec": {"containers": [{"resources": {}}]}}}
                }
                resources = patch["spec"]["template"]["spec"]["containers"][0][
                    "resources"
                ]

                if "requests" in analysis:
                    resources["requests"] = analysis["requests"]
                if "limits" in analysis:
                    resources["limits"] = analysis["limits"]

                result = await self.k8s_service.patch_deployment(
                    deployment_name, patch, namespace
                )
                return "资源配置修复完成" if result else "资源配置修复失败"

            return f"执行修复操作: {action}"
        except Exception as e:
            logger.error(f"执行修复失败: {str(e)}")
            return f"执行修复失败: {str(e)}"

    async def _verify_fix(self, deployment_name: str, namespace: str) -> str:
        """验证修复结果"""
        try:
            pods = await self.k8s_service.get_pods_async(
                namespace=namespace, label_selector=f"app={deployment_name}"
            )
            if not pods:
                return "验证失败：未找到Pod"

            healthy_count = 0
            for pod in pods:
                status = pod.get("status", {})
                if status.get("phase") == "Running" and self._is_pod_ready(status):
                    healthy_count += 1

            return f"验证结果：{healthy_count}/{len(pods)} Pod运行正常"
        except Exception as e:
            logger.error(f"验证修复结果失败: {str(e)}")
            return f"验证失败: {str(e)}"

    def _extract_pod_info(self, pod: Dict[str, Any]) -> Dict[str, Any]:
        """提取Pod信息"""
        return {
            "name": pod.get("metadata", {}).get("name"),
            "status": pod.get("status", {}).get("phase"),
            "ready": self._is_pod_ready(pod.get("status", {})),
            "restart_count": self._get_restart_count(pod.get("status", {})),
        }

    def _is_pod_ready(self, status: Dict[str, Any]) -> bool:
        """检查Pod是否就绪"""
        conditions = status.get("conditions", [])
        for condition in conditions:
            if condition.get("type") == "Ready":
                return condition.get("status") == "True"
        return False

    def _get_restart_count(self, status: Dict[str, Any]) -> int:
        """获取Pod重启次数"""
        container_statuses = status.get("container_statuses", [])
        if container_statuses:
            return container_statuses[0].get("restart_count", 0)
        return 0

    async def diagnose_cluster_health(self, namespace: str = None) -> str:
        """诊断集群健康状态"""
        try:
            if not await self._check_and_fix_k8s_connection():
                return "无法连接到Kubernetes集群"

            namespace = namespace or config.k8s.namespace

            # 检查节点状态
            nodes_status = "节点状态未知"
            try:
                nodes = self.k8s_service.core_v1.list_node()
                ready_nodes = sum(
                    1
                    for node in nodes.items
                    for condition in node.status.conditions
                    if condition.type == "Ready" and condition.status == "True"
                )
                nodes_status = f"节点: {ready_nodes}/{len(nodes.items)} 就绪"
            except Exception as e:
                logger.error(f"获取节点状态失败: {str(e)}")

            # 检查Pod状态
            pods_status = "Pod状态未知"
            try:
                pods = await self.k8s_service.get_pods_async(namespace=namespace)
                running_pods = sum(
                    1
                    for pod in pods
                    if pod.get("status", {}).get("phase") == "Running"
                    and self._is_pod_ready(pod.get("status", {}))
                )
                pods_status = f"Pod: {running_pods}/{len(pods)} 运行中"
            except Exception as e:
                logger.error(f"获取Pod状态失败: {str(e)}")

            # 检查Deployment状态
            deployments_status = "部署状态未知"
            try:
                deployments = self.k8s_service.apps_v1.list_namespaced_deployment(
                    namespace
                )
                healthy_deployments = sum(
                    1
                    for deployment in deployments.items
                    if (deployment.status.available_replicas or 0)
                    == deployment.spec.replicas
                )
                deployments_status = (
                    f"部署: {healthy_deployments}/{len(deployments.items)} 健康"
                )
            except Exception as e:
                logger.error(f"获取部署状态失败: {str(e)}")

            return f"集群健康状态:\n{nodes_status}\n{pods_status}\n{deployments_status}"
        except Exception as e:
            logger.error(f"集群健康诊断失败: {str(e)}")
            return f"诊断失败: {str(e)}"

    def get_available_tools(self) -> List[str]:
        """获取可用工具列表"""
        return [
            "analyze_and_fix_deployment",
            "diagnose_cluster_health",
            "_check_and_fix_k8s_connection",
            "_identify_and_fix_common_issues",
            "_execute_fix",
            "_verify_fix",
        ]

    async def process_agent_state(self, state) -> Any:
        """处理代理状态"""
        try:
            if hasattr(state, "deployment_name") and hasattr(state, "namespace"):
                return await self.analyze_and_fix_deployment(
                    state.deployment_name,
                    state.namespace,
                    getattr(state, "error_description", ""),
                )
            return await self.diagnose_cluster_health()
        except Exception as e:
            logger.error(f"处理代理状态失败: {str(e)}")
            return f"处理失败: {str(e)}"
