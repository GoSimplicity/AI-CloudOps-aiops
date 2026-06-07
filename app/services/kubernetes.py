#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Kubernetes集群管理服务
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from kubernetes import client, config as k8s_config, utils as k8s_utils
from kubernetes.client.rest import ApiException

from app.config.settings import ROOT_DIR, config

logger = logging.getLogger("aiops.kubernetes")


class KubernetesService:
    def __init__(self):
        self.apps_v1 = None
        self.core_v1 = None
        self.initialized = False
        self.last_init_attempt = 0
        self._init_retry_interval = 60  # 60秒后重试初始化
        self._try_init()

    def _try_init(self):
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
                version = api.get_code()
                logger.info(f"Kubernetes连接成功: {version.git_version}")

                # 尝试列出命名空间，再次确认连接
                self.core_v1.list_namespace(limit=1)
                logger.info("成功获取命名空间列表，确认连接正常")

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
        try:
            config_file = config.k8s.config_path
            logger.info(
                f"尝试加载K8s配置: in_cluster={config.k8s.in_cluster}, config_path={config_file}"
            )

            if config.k8s.in_cluster:
                k8s_config.load_incluster_config()
                logger.info("使用集群内K8s配置")
            else:
                config_file = self._resolve_kube_config_path(config_file)
                k8s_config.load_kube_config(config_file=config_file)
                if config_file:
                    logger.info(f"使用本地K8s配置文件: {config_file}")
                else:
                    logger.info("使用默认K8s配置")

        except Exception as e:
            logger.error(f"无法加载K8s配置: {str(e)}")
            raise

    def _resolve_kube_config_path(self, configured_path: Optional[str]) -> Optional[str]:
        explicit_config_path = os.getenv("K8S_CONFIG_PATH")
        if explicit_config_path:
            path = os.path.expanduser(explicit_config_path)
            if os.path.exists(path):
                return path
            raise FileNotFoundError(f"K8s配置文件不存在: {path}")

        configured_path = (configured_path or "").strip()
        configured_is_default = self._is_default_project_kube_config(configured_path)

        candidate_paths: List[Optional[str]] = []
        kubeconfig_env = os.getenv("KUBECONFIG")
        if kubeconfig_env:
            candidate_paths.extend(kubeconfig_env.split(os.pathsep))

        if configured_path and not configured_is_default:
            candidate_paths.append(configured_path)

        candidate_paths.append(os.path.expanduser("~/.kube/config"))

        if configured_path and configured_is_default:
            candidate_paths.append(configured_path)
        elif configured_path:
            logger.debug("使用显式K8s配置候选: %s", configured_path)

        candidate_paths.extend(
            [
                os.path.join(os.getcwd(), "deploy/kubernetes/config"),
                os.path.join(os.getcwd(), "config"),
            ]
        )

        seen = set()
        for raw_path in candidate_paths:
            if not raw_path:
                continue
            path = os.path.abspath(os.path.expanduser(raw_path))
            if path in seen:
                continue
            seen.add(path)
            if os.path.exists(path):
                configured_abs_path = (
                    os.path.abspath(os.path.expanduser(configured_path))
                    if configured_path
                    else None
                )
                if configured_abs_path and configured_abs_path != path:
                    logger.info(f"找到替代K8s配置文件: {path}")
                return path

        if configured_path:
            logger.error(f"K8s配置文件不存在: {configured_path}")

        return None

    def _is_default_project_kube_config(self, path: str) -> bool:
        if not path:
            return False

        normalized = os.path.normpath(os.path.expanduser(path))
        default_suffix = os.path.normpath("deploy/kubernetes/config")
        project_default = os.path.normpath(str(ROOT_DIR / default_suffix))

        return normalized in {
            default_suffix,
            os.path.normpath(f".{os.sep}{default_suffix}"),
            project_default,
        }

    def _ensure_initialized(self):
        """确保Kubernetes客户端已初始化"""
        if not self.initialized:
            self._try_init()
        if not self.initialized:
            logger.warning("Kubernetes未初始化，无法执行操作")

        return True  # 始终返回True，让调用者继续执行

    async def list_nodes(self) -> List[Dict[str, Any]]:
        """列出集群节点"""
        if not self._ensure_initialized():
            return 
        try:
            nodes = self.core_v1.list_node()
            results: List[Dict[str, Any]] = []
            for node in nodes.items:
                d = node.to_dict()
                if "metadata" in d:
                    for key in ["managed_fields", "resource_version", "uid"]:
                        d["metadata"].pop(key, None)
                results.append(d)
            return results
        except Exception as e:
            logger.error(f"获取节点失败: {e}")
            return []

    async def list_services(self, namespace: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出命名空间Service"""
        if not self._ensure_initialized():
            return []
        try:
            ns = namespace or config.k8s.namespace
            svcs = self.core_v1.list_namespaced_service(namespace=ns)
            results: List[Dict[str, Any]] = []
            for svc in svcs.items:
                d = svc.to_dict()
                if "metadata" in d:
                    for key in ["managed_fields", "resource_version", "uid"]:
                        d["metadata"].pop(key, None)
                results.append(d)
            return results
        except Exception as e:
            logger.error(f"获取Service失败: {e}")
            return []

    async def list_endpoints(self, namespace: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出命名空间Endpoints"""
        if not self._ensure_initialized():
            return []
        try:
            ns = namespace or config.k8s.namespace
            eps = self.core_v1.list_namespaced_endpoints(namespace=ns)
            results: List[Dict[str, Any]] = []
            for ep in eps.items:
                d = ep.to_dict()
                if "metadata" in d:
                    for key in ["managed_fields", "resource_version", "uid"]:
                        d["metadata"].pop(key, None)
                results.append(d)
            return results
        except Exception as e:
            logger.error(f"获取Endpoints失败: {e}")
            return []

    async def list_pvcs(self, namespace: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出命名空间PVC"""
        if not self._ensure_initialized():
            return []
        try:
            ns = namespace or config.k8s.namespace
            pvcs = self.core_v1.list_namespaced_persistent_volume_claim(namespace=ns)
            results: List[Dict[str, Any]] = []
            for pvc in pvcs.items:
                d = pvc.to_dict()
                if "metadata" in d:
                    for key in ["managed_fields", "resource_version", "uid"]:
                        d["metadata"].pop(key, None)
                results.append(d)
            return results
        except Exception as e:
            logger.error(f"获取PVC失败: {e}")
            return []

    async def get_deployment(self, name: str, namespace: str = None) -> Optional[Dict]:
        """获取Deployment信息"""
        if not self._ensure_initialized():
            raise RuntimeError("Kubernetes未初始化，无法获取Deployment信息")

        try:
            namespace = namespace or config.k8s.namespace
            deployment = self.apps_v1.read_namespaced_deployment(
                name=name, namespace=namespace
            )

            deployment_dict = deployment.to_dict()
            # 清理敏感信息
            if "metadata" in deployment_dict:
                metadata = deployment_dict["metadata"]
                for key in ["managed_fields", "resource_version", "uid", "self_link"]:
                    metadata.pop(key, None)

            logger.info(f"获取Deployment成功: {name}")
            return deployment_dict

        except ApiException as e:
            logger.error(f"获取Deployment失败: {str(e)}")
            return None

    async def ensure_namespace(self, namespace: str) -> bool:
        """确保命名空间存在，不存在则创建"""
        if not self._ensure_initialized():
            raise RuntimeError("Kubernetes未初始化，无法确保命名空间")

        try:
            self.core_v1.read_namespace(name=namespace)
            logger.info(f"命名空间已存在: {namespace}")
            return True
        except ApiException as e:
            if getattr(e, "status", None) == 404:
                try:
                    body = client.V1Namespace(
                        metadata=client.V1ObjectMeta(name=namespace)
                    )
                    self.core_v1.create_namespace(body=body)
                    logger.info(f"已创建命名空间: {namespace}")
                    return True
                except Exception as ce:
                    logger.error(f"创建命名空间失败: {str(ce)}")
                    return False
            logger.error(f"检查命名空间失败: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"确保命名空间异常: {str(e)}")
            return False

    async def apply_yaml_file(
        self, yaml_path: str, namespace: Optional[str] = None
    ) -> Dict[str, Any]:
        """应用YAML清单，创建或更新资源。

        Args:
            yaml_path: YAML文件路径
            namespace: 覆盖命名空间（可选）

        Returns:
            执行结果字典
        """
        if not self._ensure_initialized():
            raise RuntimeError("Kubernetes未初始化，无法应用YAML")

        try:
            # 使用kubernetes.utils提供的便捷方法加载并创建/更新资源
            k8s_client = self.apps_v1.api_client if self.apps_v1 else client.ApiClient()
            k8s_utils.create_from_yaml(
                k8s_client,
                yaml_path,
                namespace=namespace,
                verbose=False,
            )
            logger.info(
                f"已应用YAML: {yaml_path} (namespace={namespace or 'as-defined'})"
            )
            return {"success": True, "path": yaml_path}
        except Exception as e:
            msg = str(e)
            if "AlreadyExists" in msg or "already exists" in msg:
                logger.info(f"YAML资源已存在，视为成功: {yaml_path}")
                return {"success": True, "path": yaml_path, "note": "already exists"}
            logger.error(f"应用YAML失败 {yaml_path}: {msg}")
            return {"success": False, "path": yaml_path, "error": msg}
        except Exception as e:
            logger.error(f"获取Deployment异常: {str(e)}")
            return None

    async def patch_deployment(
        self, name: str, patch: Dict[str, Any], namespace: str = None
    ) -> bool:
        """更新Deployment"""
        if not self._ensure_initialized():
            raise RuntimeError("Kubernetes未初始化，无法更新Deployment")

        try:
            namespace = namespace or config.k8s.namespace

            logger.info(f"更新Deployment: {name}, patch: {json.dumps(patch, indent=2)}")

            self.apps_v1.patch_namespaced_deployment(
                name=name, namespace=namespace, body=patch
            )

            logger.info(f"成功更新Deployment {name}")
            return True

        except ApiException as e:
            logger.error(f"更新Deployment失败: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"更新Deployment异常: {str(e)}")
            return False

    async def get_pods(
        self, namespace: str = None, label_selector: str = None
    ) -> List[Dict]:
        """获取Pod列表"""
        if not self._ensure_initialized():
            raise RuntimeError("Kubernetes未初始化，无法获取Pod列表")

        try:
            namespace = namespace or config.k8s.namespace
            pods = self.core_v1.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector
            )

            pod_list = []
            for pod in pods.items:
                pod_dict = pod.to_dict()
                # 清理不必要的字段
                if "metadata" in pod_dict:
                    metadata = pod_dict["metadata"]
                    for key in ["managed_fields", "resource_version", "uid"]:
                        metadata.pop(key, None)
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
            raise RuntimeError("Kubernetes未初始化，无法获取事件列表")

        try:
            namespace = namespace or config.k8s.namespace
            events = self.core_v1.list_namespaced_event(
                namespace=namespace, field_selector=field_selector, limit=limit
            )

            event_list = []
            for event in events.items:
                event_dict = event.to_dict()
                # 清理不必要的字段
                if "metadata" in event_dict:
                    metadata = event_dict["metadata"]
                    for key in ["managed_fields", "resource_version", "uid"]:
                        metadata.pop(key, None)
                event_list.append(event_dict)

            logger.info(f"获取到 {len(event_list)} 个事件")
            return event_list

        except ApiException as e:
            logger.error(f"获取事件列表失败: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"获取事件列表异常: {str(e)}")
            return []

    async def restart_deployment(self, name: str, namespace: str = None) -> bool:
        """重启Deployment"""
        if not self._ensure_initialized():
            raise RuntimeError("Kubernetes未初始化，无法重启Deployment")

        try:
            namespace = namespace or config.k8s.namespace

            # 添加重启注解
            patch = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": datetime.now(
                                    timezone.utc
                                ).isoformat()
                            }
                        }
                    }
                }
            }

            result = await self.patch_deployment(name, patch, namespace)
            if result:
                logger.info(f"成功重启Deployment: {name}")

            return result

        except Exception as e:
            logger.error(f"重启Deployment失败: {str(e)}")
            return False

    async def wait_for_deployment_rollout(
        self,
        name: str,
        namespace: str = None,
        timeout_seconds: int = 120,
        poll_interval_seconds: float = 2.0,
    ) -> Dict[str, Any]:
        """等待Deployment滚动升级完成并返回状态。

        返回字段：
        - ready: 是否就绪
        - replicas, updated_replicas, ready_replicas, available_replicas
        - observed_generation, generation
        - conditions: 原始conditions列表（已脱敏）
        - elapsed_seconds: 实际等待时间
        - error: 若失败/异常，包含错误信息
        """
        if not self._ensure_initialized():
            raise RuntimeError("Kubernetes未初始化，无法等待Deployment滚动升级")

        start_ts = time.time()
        namespace = namespace or config.k8s.namespace

        try:
            while True:
                dep = self.apps_v1.read_namespaced_deployment(
                    name=name, namespace=namespace
                )
                dep_dict = dep.to_dict()

                spec = dep_dict.get("spec", {})
                status = dep_dict.get("status", {})
                metadata = dep_dict.get("metadata", {})

                replicas = int(spec.get("replicas", 0) or 0)
                updated_replicas = int(status.get("updated_replicas", 0) or 0)
                ready_replicas = int(status.get("ready_replicas", 0) or 0)
                available_replicas = int(status.get("available_replicas", 0) or 0)

                generation = int(metadata.get("generation", 0) or 0)
                observed_generation = int(status.get("observed_generation", 0) or 0)

                # 判定就绪条件：
                # 1) controller已观察到最新generation
                # 2) updated/available/ready副本均达到期望replicas
                observed_latest = observed_generation >= generation and generation > 0
                replicas_ok = replicas == 0 or (
                    updated_replicas >= replicas
                    and available_replicas >= replicas
                    and ready_replicas >= max(1, min(replicas, available_replicas))
                )

                if observed_latest and replicas_ok:
                    elapsed = time.time() - start_ts
                    return {
                        "ready": True,
                        "replicas": replicas,
                        "updated_replicas": updated_replicas,
                        "ready_replicas": ready_replicas,
                        "available_replicas": available_replicas,
                        "generation": generation,
                        "observed_generation": observed_generation,
                        "conditions": status.get("conditions", []),
                        "elapsed_seconds": round(elapsed, 3),
                    }

                if time.time() - start_ts >= timeout_seconds:
                    elapsed = time.time() - start_ts
                    return {
                        "ready": False,
                        "replicas": replicas,
                        "updated_replicas": updated_replicas,
                        "ready_replicas": ready_replicas,
                        "available_replicas": available_replicas,
                        "generation": generation,
                        "observed_generation": observed_generation,
                        "conditions": status.get("conditions", []),
                        "elapsed_seconds": round(elapsed, 3),
                        "error": f"rollout wait timed out after {timeout_seconds}s",
                    }

                await asyncio.sleep(poll_interval_seconds)

        except ApiException as e:
            logger.error(f"等待Deployment滚动升级失败: {str(e)}")
            return {
                "ready": False,
                "error": str(e),
                "elapsed_seconds": round(time.time() - start_ts, 3),
            }
        except Exception as e:
            logger.error(f"等待Deployment滚动升级异常: {str(e)}")
            return {
                "ready": False,
                "error": str(e),
                "elapsed_seconds": round(time.time() - start_ts, 3),
            }

    async def list_resource_quotas(self, namespace: str) -> List[Dict[str, Any]]:
        """列出命名空间的ResourceQuota"""
        if not self._ensure_initialized():
            raise RuntimeError("Kubernetes未初始化，无法获取ResourceQuota")

        try:
            rq_list = self.core_v1.list_namespaced_resource_quota(namespace=namespace)
            results: List[Dict[str, Any]] = []
            for item in rq_list.items:
                d = item.to_dict()
                # 清理不必要字段
                if "metadata" in d:
                    metadata = d["metadata"]
                    for key in ["managed_fields", "resource_version", "uid"]:
                        metadata.pop(key, None)
                results.append(d)
            logger.info(f"获取到 {len(results)} 个ResourceQuota (ns={namespace})")
            return results
        except ApiException as e:
            logger.error(f"获取ResourceQuota失败: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"获取ResourceQuota异常: {str(e)}")
            return []

    async def scale_deployment(
        self, name: str, replicas: int, namespace: str = None
    ) -> bool:
        """扩缩容Deployment"""
        if not self._ensure_initialized():
            raise RuntimeError("Kubernetes未初始化，无法扩缩容Deployment")

        try:
            namespace = namespace or config.k8s.namespace

            patch = {"spec": {"replicas": replicas}}

            result = await self.patch_deployment(name, patch, namespace)
            if result:
                logger.info(f"成功扩缩容Deployment {name} 到 {replicas} 副本")

            return result

        except Exception as e:
            logger.error(f"扩缩容Deployment失败: {str(e)}")
            return False

    async def can_i(self, verb: str, resource: str, namespace: str = None) -> bool:
        """检查当前Kubernetes身份是否具备指定资源操作权限。"""
        if not self._ensure_initialized():
            raise RuntimeError("Kubernetes未初始化，无法检查RBAC权限")

        namespace = namespace or config.k8s.namespace
        resource_group = "apps" if resource in {"deployments", "replicasets", "statefulsets", "daemonsets"} else ""

        try:
            auth_v1 = client.AuthorizationV1Api()
            review = client.V1SelfSubjectAccessReview(
                spec=client.V1SelfSubjectAccessReviewSpec(
                    resource_attributes=client.V1ResourceAttributes(
                        group=resource_group,
                        namespace=namespace,
                        resource=resource,
                        verb=verb,
                    )
                )
            )
            result = auth_v1.create_self_subject_access_review(body=review)
            return bool(result.status and result.status.allowed)
        except ApiException as e:
            logger.error(f"RBAC权限检查失败: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"RBAC权限检查异常: {str(e)}")
            return False

    def is_healthy(self) -> bool:
        """检查Kubernetes连接是否健康"""
        if not self.initialized:
            logger.warning("Kubernetes未初始化")
            return False

        try:
            # 尝试获取API版本
            api = client.VersionApi()
            api.get_code()

            # 尝试列出命名空间
            self.core_v1.list_namespace(limit=1)

            return True
        except Exception as e:
            logger.error(f"Kubernetes健康检查失败: {str(e)}")
            self.initialized = False
            return False

    async def health_check(self) -> bool:
        """异步健康检查方法 - 为RCA模块提供兼容接口"""
        return self.is_healthy()

    async def get_deployment_status(
        self, name: str, namespace: str = None
    ) -> Optional[Dict[str, Any]]:
        """获取Deployment状态详情"""
        if not self._ensure_initialized():
            raise RuntimeError("Kubernetes未初始化，无法获取Deployment状态")

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

    async def get_pod(self, namespace: str, pod_name: str) -> Optional[Dict]:
        """
        获取单个Pod的详细信息

        Args:
            namespace: Kubernetes命名空间
            pod_name: Pod名称

        Returns:
            Optional[Dict]: Pod信息字典，如果不存在则返回None
        """
        if not self._ensure_initialized():
            raise RuntimeError("Kubernetes未初始化，无法获取Pod信息")

        try:
            pod = self.core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)

            pod_dict = pod.to_dict()
            # 清理不必要的字段
            if "metadata" in pod_dict:
                metadata = pod_dict["metadata"]
                for key in ["managed_fields", "resource_version", "uid"]:
                    metadata.pop(key, None)

            logger.debug(f"成功获取Pod {pod_name} 信息")
            return pod_dict

        except ApiException as e:
            if e.status == 404:
                logger.warning(f"Pod {pod_name} 不存在于命名空间 {namespace}")
            else:
                logger.error(f"获取Pod {pod_name} 失败: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"获取Pod {pod_name} 异常: {str(e)}")
            return None

    async def get_pod_logs(
        self,
        namespace: str,
        pod_name: str,
        container_name: str = None,
        since_time: datetime = None,
        tail_lines: int = None,
        follow: bool = False,
    ) -> Optional[str]:
        """
        获取Pod容器的日志

        Args:
            namespace: Kubernetes命名空间
            pod_name: Pod名称
            container_name: 容器名称，如果不指定则获取第一个容器的日志
            since_time: 开始时间，获取此时间之后的日志
            tail_lines: 获取最后N行日志
            follow: 是否持续跟踪日志

        Returns:
            Optional[str]: 日志内容，如果获取失败则返回None
        """
        if not self._ensure_initialized():
            raise RuntimeError("Kubernetes未初始化，无法获取Pod日志")

        try:
            # 先获取Pod状态，检查容器是否就绪
            pod = self.core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)

            # 确定要查询的容器名称
            if not container_name and pod.spec.containers:
                container_name = pod.spec.containers[0].name

            # 检查容器状态
            if pod.status.container_statuses:
                for container_status in pod.status.container_statuses:
                    if container_status.name == container_name:
                        # 如果容器处于waiting状态，返回状态信息而不是尝试获取日志
                        if container_status.state.waiting:
                            reason = container_status.state.waiting.reason
                            message = container_status.state.waiting.message or ""
                            logger.info(
                                f"容器 {container_name} 处于waiting状态: {reason} - {message}"
                            )
                            # 返回状态信息而不是None，以便调用者知道发生了什么
                            return f"[容器等待中] {reason}: {message}"
                        # 如果容器从未运行过（没有lastState.terminated），跳过
                        elif (
                            not container_status.ready
                            and not container_status.state.running
                        ):
                            if not (
                                container_status.last_state
                                and container_status.last_state.terminated
                            ):
                                logger.info(
                                    f"容器 {container_name} 尚未运行，无法获取日志"
                                )
                                return "[容器未运行]"

            # 构建日志查询参数
            kwargs = {
                "name": pod_name,
                "namespace": namespace,
                "follow": follow,
                "timestamps": True,
            }

            if container_name:
                kwargs["container"] = container_name

            if since_time:
                # 确保两个时间都有时区信息
                current_time = datetime.now(timezone.utc)
                if since_time.tzinfo is None:
                    since_time = since_time.replace(tzinfo=timezone.utc)
                kwargs["since_seconds"] = int(
                    (current_time - since_time).total_seconds()
                )

            if tail_lines:
                kwargs["tail_lines"] = tail_lines

            # 获取日志
            logs = self.core_v1.read_namespaced_pod_log(**kwargs)

            logger.debug(
                f"成功获取Pod {pod_name} 容器 {container_name or 'default'} 的日志"
            )
            return logs

        except ApiException as e:
            if e.status == 404:
                logger.warning(f"Pod {pod_name} 或容器 {container_name} 不存在")
            else:
                logger.error(f"获取Pod {pod_name} 日志失败: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"获取Pod {pod_name} 日志异常: {str(e)}")
            return None
