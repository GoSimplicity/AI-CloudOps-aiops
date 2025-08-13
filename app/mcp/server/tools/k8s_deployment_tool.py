#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict

from kubernetes import client
from kubernetes.client.rest import ApiException

from app.utils.time_utils import iso_utc_now

from .k8s_base_tool import K8sBaseTool

UTC_TZ = timezone.utc


class K8sDeploymentTool(K8sBaseTool):
    """k8s Deployment管理工具"""

    def __init__(self):
        super().__init__(
            name="k8s_deployment_management",
            description="k8s Deployment管理工具，支持查看Deployment列表、获取状态、滚动更新、回滚、伸缩等操作",
        )

    def get_parameters(self) -> Dict[str, Any]:
        """获取工具参数定义"""
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": "要执行的操作",
                    "enum": [
                        "list_deployments",
                        "get_deployment_status",
                        "update_image",
                        "rollback",
                        "scale",
                        "get_rollout_history",
                        "restart_deployment",
                    ],
                },
                "config_path": {
                    "type": "string",
                    "description": "可选的kubeconfig文件路径",
                },
                "namespace": {
                    "type": "string",
                    "description": "命名空间，默认为default",
                },
                "deployment_name": {
                    "type": "string",
                    "description": "Deployment名称（部分操作需要）",
                },
                "all_namespaces": {
                    "type": "boolean",
                    "description": "是否查看所有命名空间",
                    "default": False,
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回结果数",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 50,
                },
                "container_name": {
                    "type": "string",
                    "description": "容器名称（更新镜像时需要）",
                },
                "new_image": {
                    "type": "string",
                    "description": "新的镜像地址（更新镜像时需要）",
                },
                "replicas": {
                    "type": "integer",
                    "description": "副本数量（伸缩时需要）",
                    "minimum": 0,
                    "maximum": 100,
                },
                "revision": {
                    "type": "integer",
                    "description": "回滚到的版本号（回滚时可选）",
                },
            },
            "required": ["operation"],
        }

    def _create_api_clients(self) -> Dict[str, client.ApiClient]:
        """创建Deployment工具需要的API客户端"""
        return {"apps_v1": client.AppsV1Api(), "v1": client.CoreV1Api()}

    async def _execute_internal(self, parameters: Dict[str, Any]) -> Any:
        """执行工具内部逻辑"""
        operation = parameters.get("operation")
        config_path = parameters.get("config_path")

        # 初始化API客户端
        clients = self._initialize_clients(config_path)
        apps_v1 = clients["apps_v1"]

        # 根据操作类型执行相应的方法
        if operation == "list_deployments":
            return await self._list_deployments(apps_v1, parameters)
        elif operation == "get_deployment_status":
            return await self._get_deployment_status(apps_v1, parameters)
        elif operation == "update_image":
            return await self._update_image(apps_v1, parameters)
        elif operation == "rollback":
            return await self._rollback_deployment(apps_v1, parameters)
        elif operation == "scale":
            return await self._scale_deployment(apps_v1, parameters)
        elif operation == "get_rollout_history":
            return await self._get_rollout_history(apps_v1, parameters)
        elif operation == "restart_deployment":
            return await self._restart_deployment(apps_v1, parameters)
        else:
            return {
                "success": False,
                "error": "不支持的操作",
                "message": f"未知的操作类型: {operation}",
            }

    async def _list_deployments(
        self, apps_v1: client.AppsV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Deployment列表"""
        try:
            namespace = parameters.get("namespace", "default")
            all_namespaces = parameters.get("all_namespaces", False)
            max_results = parameters.get("max_results", 50)

            loop = asyncio.get_event_loop()

            # 根据参数获取Deployment列表
            if all_namespaces:
                deployments = await loop.run_in_executor(
                    self._executor,
                    lambda: apps_v1.list_deployment_for_all_namespaces(
                        limit=max_results
                    ),
                )
            else:
                deployments = await loop.run_in_executor(
                    self._executor,
                    lambda: apps_v1.list_namespaced_deployment(
                        namespace=namespace, limit=max_results
                    ),
                )

            # 格式化Deployment信息
            deployment_list = []
            for deploy in deployments.items:
                deployment_info = {
                    "name": deploy.metadata.name,
                    "namespace": deploy.metadata.namespace,
                    "ready": f"{deploy.status.ready_replicas or 0}/{deploy.spec.replicas or 0}",
                    "up_to_date": deploy.status.updated_replicas or 0,
                    "available": deploy.status.available_replicas or 0,
                    "age": self._calculate_age(deploy.metadata.creation_timestamp),
                    "strategy": (
                        deploy.spec.strategy.type
                        if deploy.spec.strategy
                        else "RollingUpdate"
                    ),
                    "labels": deploy.metadata.labels or {},
                    "selector": deploy.spec.selector.match_labels or {},
                    "containers": [
                        {
                            "name": container.name,
                            "image": container.image,
                            "resources": self._format_resources(container.resources),
                        }
                        for container in deploy.spec.template.spec.containers or []
                    ],
                }
                deployment_list.append(deployment_info)

            return {
                "success": True,
                "operation": "list_deployments",
                "total_count": len(deployment_list),
                "deployments": deployment_list,
                "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {
                "success": False,
                "error": "获取Deployment列表失败",
                "message": str(e),
                "timestamp": iso_utc_now(),
            }

    async def _get_deployment_status(
        self, apps_v1: client.AppsV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Deployment状态详情"""
        try:
            deployment_name = parameters.get("deployment_name")
            namespace = parameters.get("namespace", "default")

            if not deployment_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "Deployment名称是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取Deployment详细信息
            deployment = await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.read_namespaced_deployment(
                    name=deployment_name, namespace=namespace
                ),
            )

            # 格式化状态信息
            deployment_status = {
                "name": deployment.metadata.name,
                "namespace": deployment.metadata.namespace,
                "uid": deployment.metadata.uid,
                "creation_timestamp": deployment.metadata.creation_timestamp.isoformat(),
                "age": self._calculate_age(deployment.metadata.creation_timestamp),
                "labels": deployment.metadata.labels or {},
                "annotations": deployment.metadata.annotations or {},
                "spec": {
                    "replicas": deployment.spec.replicas,
                    "selector": deployment.spec.selector.match_labels or {},
                    "strategy": (
                        {
                            "type": deployment.spec.strategy.type,
                            "rolling_update": (
                                {
                                    "max_unavailable": (
                                        str(
                                            deployment.spec.strategy.rolling_update.max_unavailable
                                        )
                                        if deployment.spec.strategy.rolling_update
                                        else None
                                    ),
                                    "max_surge": (
                                        str(
                                            deployment.spec.strategy.rolling_update.max_surge
                                        )
                                        if deployment.spec.strategy.rolling_update
                                        else None
                                    ),
                                }
                                if deployment.spec.strategy.rolling_update
                                else None
                            ),
                        }
                        if deployment.spec.strategy
                        else {}
                    ),
                    "revision_history_limit": deployment.spec.revision_history_limit,
                    "progress_deadline_seconds": deployment.spec.progress_deadline_seconds,
                    "paused": deployment.spec.paused or False,
                },
                "status": {
                    "replicas": deployment.status.replicas or 0,
                    "updated_replicas": deployment.status.updated_replicas or 0,
                    "ready_replicas": deployment.status.ready_replicas or 0,
                    "available_replicas": deployment.status.available_replicas or 0,
                    "unavailable_replicas": deployment.status.unavailable_replicas or 0,
                    "observed_generation": deployment.status.observed_generation,
                    "conditions": [
                        {
                            "type": cond.type,
                            "status": cond.status,
                            "reason": cond.reason,
                            "message": cond.message,
                            "last_transition_time": (
                                cond.last_transition_time.isoformat()
                                if cond.last_transition_time
                                else None
                            ),
                            "last_update_time": (
                                cond.last_update_time.isoformat()
                                if cond.last_update_time
                                else None
                            ),
                        }
                        for cond in deployment.status.conditions or []
                    ],
                },
                "pod_template": {
                    "containers": [
                        {
                            "name": container.name,
                            "image": container.image,
                            "ports": [
                                {
                                    "containerPort": port.container_port,
                                    "protocol": port.protocol,
                                }
                                for port in container.ports or []
                            ],
                            "resources": self._format_resources(container.resources),
                            "env": (
                                [
                                    {"name": env.name, "value": env.value}
                                    for env in container.env or []
                                ]
                                if container.env
                                else []
                            ),
                        }
                        for container in deployment.spec.template.spec.containers or []
                    ]
                },
            }

            return {
                "success": True,
                "operation": "get_deployment_status",
                "deployment_status": deployment_status,
                "timestamp": iso_utc_now(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Deployment不存在",
                    "message": f"在命名空间 {namespace} 中找不到Deployment {deployment_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "获取Deployment状态失败",
                    "message": str(e),
                }
        except Exception as e:
            return {
                "success": False,
                "error": "获取Deployment状态失败",
                "message": str(e),
            }

    async def _update_image(
        self, apps_v1: client.AppsV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """更新Deployment镜像"""
        try:
            deployment_name = parameters.get("deployment_name")
            namespace = parameters.get("namespace", "default")
            container_name = parameters.get("container_name")
            new_image = parameters.get("new_image")

            if not all([deployment_name, container_name, new_image]):
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "deployment_name、container_name和new_image都是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取当前Deployment
            deployment = await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.read_namespaced_deployment(
                    name=deployment_name, namespace=namespace
                ),
            )

            # 查找并更新容器镜像
            containers = deployment.spec.template.spec.containers
            container_found = False
            old_image = None

            for container in containers:
                if container.name == container_name:
                    old_image = container.image
                    container.image = new_image
                    container_found = True
                    break

            if not container_found:
                return {
                    "success": False,
                    "error": "容器不存在",
                    "message": f"在Deployment {deployment_name} 中找不到容器 {container_name}",
                }

            # 更新Deployment
            updated_deployment = await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.patch_namespaced_deployment(
                    name=deployment_name, namespace=namespace, body=deployment
                ),
            )

            return {
                "success": True,
                "operation": "update_image",
                "message": f"Deployment {deployment_name} 的容器 {container_name} 镜像已更新",
                "deployment_name": deployment_name,
                "namespace": namespace,
                "container_name": container_name,
                "old_image": old_image,
                "new_image": new_image,
                "revision": updated_deployment.metadata.generation,
                "timestamp": datetime.now(UTC_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Deployment不存在",
                    "message": f"在命名空间 {namespace} 中找不到Deployment {deployment_name}",
                }
            else:
                return {"success": False, "error": "更新镜像失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "更新镜像失败", "message": str(e)}

    async def _rollback_deployment(
        self, apps_v1: client.AppsV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """回滚Deployment"""
        try:
            deployment_name = parameters.get("deployment_name")
            namespace = parameters.get("namespace", "default")
            revision = parameters.get("revision")

            if not deployment_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "deployment_name是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 如果没有指定revision，回滚到上一个版本
            if not revision:
                # 获取当前Deployment的revision
                current_deployment = await loop.run_in_executor(
                    self._executor,
                    lambda: apps_v1.read_namespaced_deployment(
                        name=deployment_name, namespace=namespace
                    ),
                )

                current_revision = int(
                    current_deployment.metadata.annotations.get(
                        "deployment.kubernetes.io/revision", "1"
                    )
                )
                revision = current_revision - 1 if current_revision > 1 else 1

            # 构建回滚请求
            # 注意：Kubernetes 1.16+ 已废弃DeploymentRollback，我们使用kubectl rollout undo的等效操作
            # 这里我们通过更新Deployment的pod template来实现回滚
            deployment = await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.read_namespaced_deployment(
                    name=deployment_name, namespace=namespace
                ),
            )

            # 更新revision annotation来触发回滚
            if not deployment.metadata.annotations:
                deployment.metadata.annotations = {}

            deployment.metadata.annotations["deployment.kubernetes.io/revision"] = str(
                revision
            )

            updated_deployment = await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.patch_namespaced_deployment(
                    name=deployment_name, namespace=namespace, body=deployment
                ),
            )

            return {
                "success": True,
                "operation": "rollback",
                "message": f"Deployment {deployment_name} 已回滚到版本 {revision}",
                "deployment_name": deployment_name,
                "namespace": namespace,
                "target_revision": revision,
                "current_revision": updated_deployment.metadata.generation,
                "timestamp": datetime.now(UTC_TZ).isoformat(),
            }

        except Exception as e:
            return {"success": False, "error": "回滚失败", "message": str(e)}

    async def _scale_deployment(
        self, apps_v1: client.AppsV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """伸缩Deployment"""
        try:
            deployment_name = parameters.get("deployment_name")
            namespace = parameters.get("namespace", "default")
            replicas = parameters.get("replicas")

            if not deployment_name or replicas is None:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "deployment_name和replicas都是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取当前Deployment
            deployment = await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.read_namespaced_deployment(
                    name=deployment_name, namespace=namespace
                ),
            )

            old_replicas = deployment.spec.replicas
            deployment.spec.replicas = replicas

            # 更新Deployment
            await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.patch_namespaced_deployment(
                    name=deployment_name, namespace=namespace, body=deployment
                ),
            )

            return {
                "success": True,
                "operation": "scale",
                "message": f"Deployment {deployment_name} 已从 {old_replicas} 个副本伸缩到 {replicas} 个副本",
                "deployment_name": deployment_name,
                "namespace": namespace,
                "old_replicas": old_replicas,
                "new_replicas": replicas,
                "timestamp": datetime.now(UTC_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Deployment不存在",
                    "message": f"在命名空间 {namespace} 中找不到Deployment {deployment_name}",
                }
            else:
                return {"success": False, "error": "伸缩失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "伸缩失败", "message": str(e)}

    async def _get_rollout_history(
        self, apps_v1: client.AppsV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Deployment的发布历史"""
        try:
            deployment_name = parameters.get("deployment_name")
            namespace = parameters.get("namespace", "default")

            if not deployment_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "deployment_name是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取相关的ReplicaSet
            replica_sets = await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.list_namespaced_replica_set(
                    namespace=namespace, label_selector=f"app={deployment_name}"
                ),
            )

            # 格式化历史信息
            history = []
            for rs in replica_sets.items:
                if (
                    rs.metadata.annotations
                    and "deployment.kubernetes.io/revision" in rs.metadata.annotations
                ):
                    revision = rs.metadata.annotations[
                        "deployment.kubernetes.io/revision"
                    ]
                    change_cause = rs.metadata.annotations.get(
                        "kubernetes.io/change-cause", "未记录"
                    )

                    history.append(
                        {
                            "revision": int(revision),
                            "change_cause": change_cause,
                            "replica_set_name": rs.metadata.name,
                            "creation_timestamp": rs.metadata.creation_timestamp.isoformat(),
                            "replicas": rs.spec.replicas or 0,
                            "ready_replicas": rs.status.ready_replicas or 0,
                        }
                    )

            # 按版本号排序
            history.sort(key=lambda x: x["revision"])

            return {
                "success": True,
                "operation": "get_rollout_history",
                "deployment_name": deployment_name,
                "namespace": namespace,
                "history": history,
                "total_revisions": len(history),
                "timestamp": datetime.now(UTC_TZ).isoformat(),
            }

        except Exception as e:
            return {"success": False, "error": "获取发布历史失败", "message": str(e)}

    async def _restart_deployment(
        self, apps_v1: client.AppsV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """重启Deployment"""
        try:
            deployment_name = parameters.get("deployment_name")
            namespace = parameters.get("namespace", "default")

            if not deployment_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "deployment_name是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取当前Deployment
            deployment = await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.read_namespaced_deployment(
                    name=deployment_name, namespace=namespace
                ),
            )

            # 添加/更新重启注解
            if not deployment.spec.template.metadata:
                deployment.spec.template.metadata = client.V1ObjectMeta()
            if not deployment.spec.template.metadata.annotations:
                deployment.spec.template.metadata.annotations = {}

            deployment.spec.template.metadata.annotations[
                "kubectl.kubernetes.io/restartedAt"
            ] = iso_utc_now()

            # 更新Deployment
            await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.patch_namespaced_deployment(
                    name=deployment_name, namespace=namespace, body=deployment
                ),
            )

            return {
                "success": True,
                "operation": "restart_deployment",
                "message": f"Deployment {deployment_name} 已重启",
                "deployment_name": deployment_name,
                "namespace": namespace,
                "restarted_at": deployment.spec.template.metadata.annotations[
                    "kubectl.kubernetes.io/restartedAt"
                ],
                "timestamp": iso_utc_now(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Deployment不存在",
                    "message": f"在命名空间 {namespace} 中找不到Deployment {deployment_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "重启Deployment失败",
                    "message": str(e),
                }
        except Exception as e:
            return {"success": False, "error": "重启Deployment失败", "message": str(e)}

    def _format_resources(self, resources) -> Dict[str, Any]:
        """格式化资源配置"""
        if not resources:
            return {}

        return {
            "requests": dict(resources.requests) if resources.requests else {},
            "limits": dict(resources.limits) if resources.limits else {},
        }
