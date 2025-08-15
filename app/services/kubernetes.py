#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Kubernetes 客户端
"""

import logging
import asyncio
import os
import time
from typing import Any, Dict, List, Optional

from kubernetes import client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

from app.config.settings import config

logger = logging.getLogger("aiops.kubernetes")


class KubernetesService:
    def __init__(self):
        self.apps_v1 = None
        self.core_v1 = None
        self.initialized = False
        self.last_init_attempt = 0
        self._init_retry_interval = 60  # 60秒后重试初始化
        self._try_init()

    def _clean_metadata(self, resource_dict: Dict[str, Any]) -> Dict[str, Any]:
        """清理Kubernetes资源的敏感元数据信息"""
        if "metadata" in resource_dict:
            metadata = resource_dict["metadata"]
            sensitive_keys = ["managed_fields", "resource_version", "uid", "self_link"]
            for key in sensitive_keys:
                metadata.pop(key, None)
        return resource_dict

    def _try_init(self):
        """尝试初始化Kubernetes客户端"""
        try:
            if time.time() - self.last_init_attempt < self._init_retry_interval:
                return  # 避免频繁重试

            self.last_init_attempt = time.time()
            self._load_config()
            self.apps_v1 = client.AppsV1Api()
            self.core_v1 = client.CoreV1Api()

            # 测试连接
            try:
                api = client.VersionApi()
                api.get_code()

                # 尝试列出命名空间，再次确认连接
                self.core_v1.list_namespace(limit=1)

                self.initialized = True
                logger.info("Kubernetes服务初始化完成")
            except Exception as e:
                self.initialized = False
                logger.error(f"Kubernetes连接测试失败: {str(e)}")
                raise

        except Exception as e:
            self.initialized = False
            logger.error(f"Kubernetes初始化失败: {str(e)}")

    def _load_config(self):
        """加载Kubernetes配置"""
        try:
            base_dir = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            config_file = os.path.join(base_dir, config.k8s.config_path)
            logger.info(
                f"尝试加载K8s配置: in_cluster={config.k8s.in_cluster}, config_path={config_file}"
            )

            # 检查配置文件是否存在
            if not config.k8s.in_cluster and config_file:
                # 检查文件是否存在
                if not os.path.exists(config_file):
                    logger.error(f"K8s配置文件不存在: {config_file}")
                    # 尝试查找其他可能的位置
                    alternate_paths = [
                        os.path.join(os.getcwd(), "deploy/kubernetes/config"),
                        os.path.join(os.getcwd(), "config"),
                        os.path.expanduser("~/.kube/config"),
                    ]

                    for path in alternate_paths:
                        if os.path.exists(path):
                            logger.info(f"找到替代配置文件: {path}")
                            config_file = path
                            break
                    else:
                        logger.info("尝试从默认位置加载配置")
                        try:
                            k8s_config.load_kube_config()
                            logger.info("成功从默认位置加载K8s配置")
                            return
                        except Exception as e:
                            logger.error(f"从默认位置加载K8s配置失败: {str(e)}")
                            raise

            if config.k8s.in_cluster:
                k8s_config.load_incluster_config()
                logger.info("使用集群内K8s配置")
            else:
                k8s_config.load_kube_config(config_file=config_file)
                logger.info(f"使用本地K8s配置文件: {config_file}")

        except Exception as e:
            logger.error(f"无法加载K8s配置: {str(e)}")
            raise

    def _ensure_initialized(self):
        """确保Kubernetes客户端已初始化"""
        # 已初始化直接返回
        if self.initialized:
            return True

        # 单元测试或上层可注入 mock API 客户端：
        # 若检测到注入的 `core_v1_api`/`apps_v1_api`，则视为可用，避免真实集群依赖
        if getattr(self, "core_v1_api", None) or getattr(self, "apps_v1_api", None):
            return True

        # 尝试初始化真实客户端
        self._try_init()
        if not self.initialized:
            logger.warning("Kubernetes未初始化，相关功能将返回模拟数据或空值")
            logger.info("提示：请确保Kubernetes集群正在运行，或检查kubeconfig配置")

        return self.initialized  # 返回实际的初始化状态

    def get_nodes(self) -> List[Any]:
        """获取节点列表（同步，便于简单统计与单元测试）。"""
        if not self._ensure_initialized():
            return []
        try:
            nodes = self.core_v1.list_node()
            return nodes.items or []
        except Exception:
            return []

    async def get_deployment(self, name: str, namespace: str = None) -> Optional[Dict]:
        """获取Deployment信息"""
        if not self._ensure_initialized():
            logger.warning("Kubernetes未初始化，无法获取Deployment信息")
            return None

        try:
            namespace = namespace or config.k8s.namespace
            deployment = await asyncio.to_thread(
                self.apps_v1.read_namespaced_deployment, name=name, namespace=namespace
            )

            deployment_dict = deployment.to_dict()
            # 清理敏感信息
            deployment_dict = self._clean_metadata(deployment_dict)

            logger.info(f"获取Deployment成功: {name}")
            return deployment_dict

        except ApiException as e:
            logger.error(f"获取Deployment失败: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"获取Deployment异常: {str(e)}")
            return None

    # 新增：同步封装，供单元测试与简单调用使用
    def get_pods(self, namespace: str = None, label_selector: str = None) -> List[Any]:
        if not self._ensure_initialized():
            return []
        try:
            namespace = namespace or config.k8s.namespace
            api = getattr(self, "core_v1_api", None) or self.core_v1
            pods = api.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector
            )
            return pods.items or []
        except Exception:
            return []

    def get_deployments(self, namespace: str = None) -> List[Any]:
        if not self._ensure_initialized():
            return []
        try:
            namespace = namespace or config.k8s.namespace
            api = getattr(self, "apps_v1_api", None) or self.apps_v1
            deployments = api.list_namespaced_deployment(namespace=namespace)
            return deployments.items or []
        except Exception:
            return []

    def get_services(self, namespace: str = None) -> List[Any]:
        if not self._ensure_initialized():
            return []
        try:
            namespace = namespace or config.k8s.namespace
            api = getattr(self, "core_v1_api", None) or self.core_v1
            services = api.list_namespaced_service(namespace=namespace)
            return services.items or []
        except Exception:
            return []

    async def get_pods_async(
        self, namespace: str = None, label_selector: str = None
    ) -> List[Dict]:
        """获取Pod列表（异步版本）"""
        if not self._ensure_initialized():
            logger.warning("Kubernetes未初始化，无法获取Pod列表")
            return []

        try:
            namespace = namespace or config.k8s.namespace
            pods = await asyncio.to_thread(
                self.core_v1.list_namespaced_pod,
                namespace=namespace,
                label_selector=label_selector,
            )

            pod_list = []
            for pod in pods.items:
                pod_dict = pod.to_dict()
                # 清理不必要的字段
                pod_dict = self._clean_metadata(pod_dict)
                pod_list.append(pod_dict)

            logger.info(f"获取到 {len(pod_list)} 个Pod")
            return pod_list

        except ApiException as e:
            logger.error(f"获取Pod列表失败: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"获取Pod列表异常: {str(e)}")
            return []

    async def get_events(
        self, namespace: str = None, field_selector: str = None, limit: int = 100
    ) -> List[Dict]:
        """获取事件列表"""
        if not self._ensure_initialized():
            logger.warning("Kubernetes未初始化，无法获取事件列表")
            return []

        try:
            namespace = namespace or config.k8s.namespace
            events = await asyncio.to_thread(
                self.core_v1.list_namespaced_event,
                namespace=namespace,
                field_selector=field_selector,
                limit=limit,
            )

            event_list: List[Dict[str, Any]] = []
            for event in events.items:
                event_dict = event.to_dict()
                # 清理不必要的字段
                event_dict = self._clean_metadata(event_dict)
                event_list.append(event_dict)

            logger.info(f"获取到 {len(event_list)} 个事件")
            return event_list

        except ApiException as e:
            logger.error(f"获取事件列表失败: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"获取事件列表异常: {str(e)}")
            return []

    async def get_pod_logs(
        self,
        pod_name: str,
        namespace: Optional[str] = None,
        container: Optional[str] = None,
        tail_lines: int = 200,
        previous: bool = False,
    ) -> Optional[str]:
        """获取指定 Pod（可选容器）的最近日志。

        说明：
        - 为RCA日志采集提供最小必要能力，避免一次性返回过多内容。
        """
        if not self._ensure_initialized():
            logger.warning("Kubernetes未初始化，无法获取Pod日志")
            return None

        try:
            namespace = namespace or config.k8s.namespace
            logs = await asyncio.to_thread(
                self.core_v1.read_namespaced_pod_log,
                name=pod_name,
                namespace=namespace,
                container=container,
                previous=previous,
                tail_lines=tail_lines if tail_lines and tail_lines > 0 else None,
                timestamps=True,
            )
            return logs
        except ApiException as e:
            logger.error(
                f"获取Pod日志失败: pod={pod_name}, ns={namespace}, err={str(e)}"
            )
            return None
        except Exception as e:
            logger.error(
                f"获取Pod日志异常: pod={pod_name}, ns={namespace}, err={str(e)}"
            )
            return None

    async def get_recent_pod_logs(
        self,
        namespace: Optional[str] = None,
        label_selector: Optional[str] = None,
        max_pods: int = 5,
        tail_lines: int = 200,
        include_previous: bool = False,
    ) -> List[Dict[str, Any]]:
        """获取命名空间内若干Pod的最近日志（每个Pod仅取一个容器）。"""
        pods = await self.get_pods_async(
            namespace=namespace, label_selector=label_selector
        )
        results: List[Dict[str, Any]] = []
        if not pods:
            return results

        ns = namespace or config.k8s.namespace
        for pod in pods[: max(1, int(max_pods))]:
            pod_name = ((pod or {}).get("metadata", {}) or {}).get("name")
            spec = (pod or {}).get("spec", {}) or {}
            containers = spec.get("containers") or []
            container_name = containers[0].get("name") if containers else None

            current_logs = await self.get_pod_logs(
                pod_name=pod_name,
                namespace=ns,
                container=container_name,
                tail_lines=tail_lines,
                previous=False,
            )
            prev_logs = None
            if include_previous:
                prev_logs = await self.get_pod_logs(
                    pod_name=pod_name,
                    namespace=ns,
                    container=container_name,
                    tail_lines=tail_lines,
                    previous=True,
                )

            results.append(
                {
                    "pod": pod_name,
                    "namespace": ns,
                    "container": container_name,
                    "logs": current_logs or "",
                    "previous_logs": prev_logs or "",
                }
            )

        return results

    async def get_deployment_status(
        self, name: str, namespace: str = None
    ) -> Optional[Dict[str, Any]]:
        """获取Deployment状态详情"""
        if not self._ensure_initialized():
            logger.warning("Kubernetes未初始化，无法获取Deployment状态")
            return None

        try:
            deployment = await self.get_deployment(name, namespace)
            if not deployment:
                return None

            status = deployment.get("status", {})
            spec = deployment.get("spec", {})

            return {
                "name": name,
                "namespace": namespace or config.k8s.namespace,
                "replicas": spec.get("replicas", 0),
                "ready_replicas": status.get("ready_replicas", 0),
                "available_replicas": status.get("available_replicas", 0),
                "updated_replicas": status.get("updated_replicas", 0),
                "conditions": status.get("conditions", []),
                "strategy": spec.get("strategy", {}),
                "creation_timestamp": deployment.get("metadata", {}).get(
                    "creation_timestamp"
                ),
            }

        except Exception as e:
            logger.error(f"获取Deployment状态失败: {str(e)}")
            return None

    async def get_nodes_async(self) -> List[Dict[str, Any]]:
        """获取节点列表（异步版本，返回字典）。"""
        if not self._ensure_initialized():
            logger.warning("Kubernetes未初始化，无法获取节点列表")
            return []
        try:
            nodes = await asyncio.to_thread(self.core_v1.list_node)
            node_list: List[Dict[str, Any]] = []
            for n in nodes.items or []:
                node_dict = n.to_dict() if hasattr(n, "to_dict") else {}
                node_dict = self._clean_metadata(node_dict)
                node_list.append(node_dict)
            return node_list
        except Exception as e:
            logger.error(f"获取节点列表失败: {str(e)}")
            return []

    async def patch_deployment(
        self,
        name: str,
        patch: Dict[str, Any],
        namespace: Optional[str] = None,
        *,
        dry_run: bool = False,
        field_manager: str = "aiops",
    ) -> bool:
        """对 Deployment 执行 JSON Merge Patch。

        说明：
        - 统一由各 Agent 调用；若 dry_run=True 则仅做服务端验证。
        - 返回是否成功。
        """
        if not self._ensure_initialized():
            logger.warning("Kubernetes未初始化，无法Patch Deployment")
            return False
        try:
            ns = namespace or config.k8s.namespace
            kwargs: Dict[str, Any] = {
                "name": name,
                "namespace": ns,
                "body": patch,
                "field_manager": field_manager,
            }
            if dry_run:
                kwargs["dry_run"] = "All"
            # 使用 AppsV1Api 的 patch_namespaced_deployment
            resp = await asyncio.to_thread(
                self.apps_v1.patch_namespaced_deployment, **kwargs
            )
            return resp is not None
        except ApiException as e:
            logger.error(f"Patch Deployment失败: {name}, ns={namespace}, err={str(e)}")
            return False
        except Exception as e:
            logger.error(f"Patch Deployment异常: {name}, ns={namespace}, err={str(e)}")
            return False

    async def get_deployments_async(self, namespace: str = None) -> List[Dict]:
        """获取所有Deployment列表（异步版本）"""
        if not self._ensure_initialized():
            logger.warning("Kubernetes未初始化，无法获取Deployment列表")
            return []

        try:
            namespace = namespace or config.k8s.namespace
            deployments = await asyncio.to_thread(
                self.apps_v1.list_namespaced_deployment, namespace=namespace
            )

            deployment_list = []
            for deployment in deployments.items:
                deployment_dict = deployment.to_dict()
                # 清理不必要的字段
                deployment_dict = self._clean_metadata(deployment_dict)
                deployment_list.append(deployment_dict)

            logger.info(f"获取到 {len(deployment_list)} 个Deployment")
            return deployment_list

        except ApiException as e:
            logger.error(f"获取Deployment列表失败: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"获取Deployment列表异常: {str(e)}")
            return []

    async def get_services_async(self, namespace: str = None) -> List[Dict]:
        """获取所有Service列表（异步版本）"""
        if not self._ensure_initialized():
            logger.warning("Kubernetes未初始化，无法获取Service列表")
            return []

        try:
            namespace = namespace or config.k8s.namespace
            services = await asyncio.to_thread(
                self.core_v1.list_namespaced_service, namespace=namespace
            )

            service_list = []
            for service in services.items:
                service_dict = service.to_dict()
                # 清理不必要的字段
                service_dict = self._clean_metadata(service_dict)
                service_list.append(service_dict)

            logger.info(f"获取到 {len(service_list)} 个Service")
            return service_list

        except ApiException as e:
            logger.error(f"获取Service列表失败: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"获取Service列表异常: {str(e)}")
            return []

    def is_healthy(self) -> bool:
        """检查Kubernetes连接是否健康"""
        if not self.initialized:
            logger.warning("Kubernetes未初始化")
            return False

        try:
            # 尝试获取API版本并列出命名空间以确认连接
            client.VersionApi().get_code()
            self.core_v1.list_namespace(limit=1)

            return True
        except Exception as e:
            logger.error(f"Kubernetes健康检查失败: {str(e)}")
            self.initialized = False
            return False

    def check_connectivity(self) -> bool:
        """检查与Kubernetes API的连通性（供单元测试使用）。"""
        try:
            if not self._ensure_initialized():
                return False
            # 优先使用测试中可能注入的 core_v1_api，其次使用 core_v1
            api = getattr(self, "core_v1_api", None) or self.core_v1
            api.list_node(limit=1)
            return True
        except Exception as e:
            logger.error(f"Kubernetes连通性检查失败: {str(e)}")
            return False
