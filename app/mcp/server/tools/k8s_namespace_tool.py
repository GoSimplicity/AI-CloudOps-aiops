#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops MCP k8s命名空间管理工具
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: k8s命名空间管理的MCP工具，提供命名空间的创建、删除、查看等操作
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))

from kubernetes import client
from kubernetes.client.rest import ApiException

from .k8s_base_tool import K8sBaseTool


class K8sNamespaceTool(K8sBaseTool):
    """k8s命名空间管理工具"""

    def __init__(self):
        super().__init__(
            name="k8s_namespace_management",
            description="k8s命名空间管理工具，支持命名空间的创建、删除、查看和资源管理",
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
                        "list_namespaces",
                        "get_namespace",
                        "create_namespace",
                        "delete_namespace",
                        "get_namespace_resources",
                    ],
                },
                "config_path": {
                    "type": "string",
                    "description": "可选的kubeconfig文件路径",
                },
                "namespace_name": {"type": "string", "description": "命名空间名称"},
                "labels": {
                    "type": "object",
                    "description": "标签",
                    "additionalProperties": {"type": "string"},
                },
                "annotations": {
                    "type": "object",
                    "description": "注解",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["operation"],
        }

    def _create_api_clients(self) -> Dict[str, client.ApiClient]:
        """创建Kubernetes API客户端"""
        return {"v1": client.CoreV1Api(), "apps_v1": client.AppsV1Api()}

    async def _execute_internal(self, parameters: Dict[str, Any]) -> Any:
        """执行工具内部逻辑"""
        operation = parameters.get("operation")
        config_path = parameters.get("config_path")

        # 初始化API客户端
        clients = self._initialize_clients(config_path)
        v1 = clients["v1"]
        apps_v1 = clients["apps_v1"]

        # 根据操作类型执行相应的方法
        operation_map = {
            "list_namespaces": lambda: self._list_namespaces(v1, parameters),
            "get_namespace": lambda: self._get_namespace(v1, parameters),
            "create_namespace": lambda: self._create_namespace(v1, parameters),
            "delete_namespace": lambda: self._delete_namespace(v1, parameters),
            "get_namespace_resources": lambda: self._get_namespace_resources(
                v1, apps_v1, parameters
            ),
        }

        if operation in operation_map:
            return await operation_map[operation]()
        else:
            return {
                "success": False,
                "error": "不支持的操作",
                "message": f"未知的操作类型: {operation}",
            }

    async def _list_namespaces(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取命名空间列表"""
        try:
            loop = asyncio.get_event_loop()

            # 获取命名空间列表
            namespaces = await loop.run_in_executor(
                self._executor, lambda: v1.list_namespace()
            )

            # 格式化命名空间信息
            namespace_list = []
            for ns in namespaces.items:
                namespace_info = {
                    "name": ns.metadata.name,
                    "status": ns.status.phase,
                    "age": self._calculate_age(ns.metadata.creation_timestamp),
                    "labels": ns.metadata.labels or {},
                    "annotations": ns.metadata.annotations or {},
                    "finalizers": ns.spec.finalizers or [] if ns.spec else [],
                }
                namespace_list.append(namespace_info)

            return {
                "success": True,
                "operation": "list_namespaces",
                "total_count": len(namespace_list),
                "namespaces": namespace_list,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except Exception as e:
            return {
                "success": False,
                "error": "获取命名空间列表失败",
                "message": str(e),
            }

    async def _get_namespace(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取命名空间详细信息"""
        try:
            namespace_name = parameters.get("namespace_name")

            if not namespace_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "namespace_name是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取命名空间详细信息
            namespace = await loop.run_in_executor(
                self._executor, lambda: v1.read_namespace(name=namespace_name)
            )

            # 格式化详细信息
            namespace_details = {
                "name": namespace.metadata.name,
                "uid": namespace.metadata.uid,
                "creation_timestamp": namespace.metadata.creation_timestamp.isoformat(),
                "age": self._calculate_age(namespace.metadata.creation_timestamp),
                "labels": namespace.metadata.labels or {},
                "annotations": namespace.metadata.annotations or {},
                "status": {
                    "phase": namespace.status.phase,
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
                        }
                        for cond in namespace.status.conditions or []
                    ],
                },
                "spec": {
                    "finalizers": (
                        namespace.spec.finalizers or [] if namespace.spec else []
                    )
                },
            }

            return {
                "success": True,
                "operation": "get_namespace",
                "namespace": namespace_details,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "命名空间不存在",
                    "message": f"找不到命名空间 {namespace_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "获取命名空间详情失败",
                    "message": str(e),
                }
        except Exception as e:
            return {
                "success": False,
                "error": "获取命名空间详情失败",
                "message": str(e),
            }

    async def _create_namespace(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """创建命名空间"""
        try:
            namespace_name = parameters.get("namespace_name")
            labels = parameters.get("labels", {})
            annotations = parameters.get("annotations", {})

            if not namespace_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "namespace_name是必需的参数",
                }

            # 构建Namespace对象
            namespace_body = client.V1Namespace(
                api_version="v1",
                kind="Namespace",
                metadata=client.V1ObjectMeta(
                    name=namespace_name, labels=labels, annotations=annotations
                ),
            )

            loop = asyncio.get_event_loop()

            # 创建命名空间
            created_namespace = await loop.run_in_executor(
                self._executor, lambda: v1.create_namespace(body=namespace_body)
            )

            return {
                "success": True,
                "operation": "create_namespace",
                "message": f"命名空间 {namespace_name} 创建成功",
                "namespace_name": created_namespace.metadata.name,
                "uid": created_namespace.metadata.uid,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 409:
                return {
                    "success": False,
                    "error": "命名空间已存在",
                    "message": f"命名空间 {namespace_name} 已经存在",
                }
            else:
                return {
                    "success": False,
                    "error": "创建命名空间失败",
                    "message": str(e),
                }
        except Exception as e:
            return {"success": False, "error": "创建命名空间失败", "message": str(e)}

    async def _delete_namespace(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """删除命名空间"""
        try:
            namespace_name = parameters.get("namespace_name")

            if not namespace_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "namespace_name是必需的参数",
                }

            # 禁止删除系统命名空间
            protected_namespaces = [
                "default",
                "kube-system",
                "kube-public",
                "kube-node-lease",
            ]
            if namespace_name in protected_namespaces:
                return {
                    "success": False,
                    "error": "禁止操作",
                    "message": f"不允许删除系统命名空间 {namespace_name}",
                }

            loop = asyncio.get_event_loop()

            # 删除命名空间
            await loop.run_in_executor(
                self._executor, lambda: v1.delete_namespace(name=namespace_name)
            )

            return {
                "success": True,
                "operation": "delete_namespace",
                "message": f"命名空间 {namespace_name} 已开始删除过程",
                "namespace_name": namespace_name,
                "note": "命名空间删除是异步过程，可能需要一些时间才能完全删除",
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "命名空间不存在",
                    "message": f"找不到命名空间 {namespace_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "删除命名空间失败",
                    "message": str(e),
                }
        except Exception as e:
            return {"success": False, "error": "删除命名空间失败", "message": str(e)}

    async def _get_namespace_resources(
        self,
        v1: client.CoreV1Api,
        apps_v1: client.AppsV1Api,
        parameters: Dict[str, Any],
    ) -> Dict[str, Any]:
        """获取命名空间中的资源统计"""
        try:
            namespace_name = parameters.get("namespace_name")

            if not namespace_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "namespace_name是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 并行获取各种资源
            tasks = [
                loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_namespaced_pod(namespace=namespace_name),
                ),
                loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_namespaced_service(namespace=namespace_name),
                ),
                loop.run_in_executor(
                    self._executor,
                    lambda: apps_v1.list_namespaced_deployment(
                        namespace=namespace_name
                    ),
                ),
                loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_namespaced_config_map(namespace=namespace_name),
                ),
                loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_namespaced_secret(namespace=namespace_name),
                ),
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)
            pods, services, deployments, configmaps, secrets = results

            # 统计资源数量
            resource_summary = {
                "pods": {
                    "total": len(pods.items) if not isinstance(pods, Exception) else 0,
                    "running": 0,
                    "pending": 0,
                    "failed": 0,
                },
                "services": {
                    "total": (
                        len(services.items)
                        if not isinstance(services, Exception)
                        else 0
                    )
                },
                "deployments": {
                    "total": (
                        len(deployments.items)
                        if not isinstance(deployments, Exception)
                        else 0
                    ),
                    "available": 0,
                    "unavailable": 0,
                },
                "configmaps": {
                    "total": (
                        len(configmaps.items)
                        if not isinstance(configmaps, Exception)
                        else 0
                    )
                },
                "secrets": {
                    "total": (
                        len(secrets.items) if not isinstance(secrets, Exception) else 0
                    )
                },
            }

            # 统计Pod状态
            if not isinstance(pods, Exception):
                for pod in pods.items:
                    phase = pod.status.phase
                    if phase == "Running":
                        resource_summary["pods"]["running"] += 1
                    elif phase == "Pending":
                        resource_summary["pods"]["pending"] += 1
                    elif phase == "Failed":
                        resource_summary["pods"]["failed"] += 1

            # 统计Deployment状态
            if not isinstance(deployments, Exception):
                for deploy in deployments.items:
                    if (
                        deploy.status.available_replicas
                        and deploy.status.available_replicas > 0
                    ):
                        resource_summary["deployments"]["available"] += 1
                    else:
                        resource_summary["deployments"]["unavailable"] += 1

            return {
                "success": True,
                "operation": "get_namespace_resources",
                "namespace_name": namespace_name,
                "resource_summary": resource_summary,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "命名空间不存在",
                    "message": f"找不到命名空间 {namespace_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "获取命名空间资源失败",
                    "message": str(e),
                }
        except Exception as e:
            return {
                "success": False,
                "error": "获取命名空间资源失败",
                "message": str(e),
            }
