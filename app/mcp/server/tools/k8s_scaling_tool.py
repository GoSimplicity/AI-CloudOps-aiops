#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops MCP k8s应用伸缩工具
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: k8s应用伸缩的MCP工具，提供Deployment、ReplicaSet等的伸缩功能
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List

from kubernetes import client
from kubernetes.client.rest import ApiException

from .k8s_base_tool import K8sBaseTool


class K8sScalingTool(K8sBaseTool):
    """k8s应用伸缩工具"""

    def __init__(self):
        super().__init__(
            name="k8s_application_scaling",
            description="k8s应用伸缩工具，支持Deployment、ReplicaSet等资源的手动伸缩和自动伸缩查看",
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
                        "scale_deployment",
                        "scale_replicaset",
                        "get_hpa_status",
                        "list_hpa",
                        "create_hpa",
                        "delete_hpa",
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
                "resource_name": {
                    "type": "string",
                    "description": "资源名称（Deployment、ReplicaSet、HPA名称）",
                },
                "replicas": {
                    "type": "integer",
                    "description": "目标副本数量",
                    "minimum": 0,
                    "maximum": 100,
                },
                "all_namespaces": {
                    "type": "boolean",
                    "description": "是否查看所有命名空间",
                    "default": False,
                },
                "hpa_config": {
                    "type": "object",
                    "description": "HPA配置（创建HPA时使用）",
                    "properties": {
                        "min_replicas": {"type": "integer", "minimum": 1, "default": 1},
                        "max_replicas": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 10,
                        },
                        "target_cpu_percent": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 80,
                        },
                        "target_memory_percent": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                },
            },
            "required": ["operation"],
        }

    def _create_api_clients(self) -> Dict[str, client.ApiClient]:
        """创建Kubernetes API客户端"""
        api_clients = {
            "apps_v1": client.AppsV1Api(),
            "autoscaling_v1": client.AutoscalingV1Api(),
            "autoscaling_v2": None,  # 会在需要时初始化
        }

        # 尝试初始化v2 API
        try:
            api_clients["autoscaling_v2"] = client.AutoscalingV2Api()
        except Exception:
            pass  # v2 API可能不可用

        return api_clients

    async def _execute_internal(self, parameters: Dict[str, Any]) -> Any:
        """执行工具内部逻辑"""
        operation = parameters.get("operation")
        config_path = parameters.get("config_path")

        # 初始化API客户端
        clients = self._initialize_clients(config_path)
        apps_v1 = clients["apps_v1"]
        autoscaling_v1 = clients["autoscaling_v1"]
        autoscaling_v2 = clients["autoscaling_v2"]

        # 根据操作类型执行相应的方法
        operation_map = {
            "scale_deployment": lambda: self._scale_deployment(apps_v1, parameters),
            "scale_replicaset": lambda: self._scale_replicaset(apps_v1, parameters),
            "get_hpa_status": lambda: self._get_hpa_status(
                autoscaling_v1, autoscaling_v2, parameters
            ),
            "list_hpa": lambda: self._list_hpa(
                autoscaling_v1, autoscaling_v2, parameters
            ),
            "create_hpa": lambda: self._create_hpa(autoscaling_v1, parameters),
            "delete_hpa": lambda: self._delete_hpa(autoscaling_v1, parameters),
        }

        if operation in operation_map:
            return await operation_map[operation]()
        else:
            return {
                "success": False,
                "error": "不支持的操作",
                "message": f"未知的操作类型: {operation}",
            }

    async def _scale_deployment(
        self, apps_v1: client.AppsV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """伸缩Deployment"""
        try:
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")
            replicas = parameters.get("replicas")

            if not resource_name or replicas is None:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_name和replicas都是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取当前Deployment
            deployment = await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.read_namespaced_deployment(
                    name=resource_name, namespace=namespace
                ),
            )

            old_replicas = deployment.spec.replicas

            # 更新副本数
            scale_body = client.V1Scale(
                metadata=client.V1ObjectMeta(name=resource_name, namespace=namespace),
                spec=client.V1ScaleSpec(replicas=replicas),
            )

            # 执行伸缩
            scaled_deployment = await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.patch_namespaced_deployment_scale(
                    name=resource_name, namespace=namespace, body=scale_body
                ),
            )

            return {
                "success": True,
                "operation": "scale_deployment",
                "message": f"Deployment {resource_name} 已从 {old_replicas} 个副本伸缩到 {replicas} 个副本",
                "deployment_name": resource_name,
                "namespace": namespace,
                "old_replicas": old_replicas,
                "new_replicas": replicas,
                "current_replicas": scaled_deployment.spec.replicas,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Deployment不存在",
                    "message": f"在命名空间 {namespace} 中找不到Deployment {resource_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "伸缩Deployment失败",
                    "message": str(e),
                }
        except Exception as e:
            return {"success": False, "error": "伸缩Deployment失败", "message": str(e)}

    async def _scale_replicaset(
        self, apps_v1: client.AppsV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """伸缩ReplicaSet"""
        try:
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")
            replicas = parameters.get("replicas")

            if not resource_name or replicas is None:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_name和replicas都是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取当前ReplicaSet
            replicaset = await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.read_namespaced_replica_set(
                    name=resource_name, namespace=namespace
                ),
            )

            old_replicas = replicaset.spec.replicas

            # 更新副本数
            scale_body = client.V1Scale(
                metadata=client.V1ObjectMeta(name=resource_name, namespace=namespace),
                spec=client.V1ScaleSpec(replicas=replicas),
            )

            # 执行伸缩
            scaled_replicaset = await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.patch_namespaced_replica_set_scale(
                    name=resource_name, namespace=namespace, body=scale_body
                ),
            )

            return {
                "success": True,
                "operation": "scale_replicaset",
                "message": f"ReplicaSet {resource_name} 已从 {old_replicas} 个副本伸缩到 {replicas} 个副本",
                "replicaset_name": resource_name,
                "namespace": namespace,
                "old_replicas": old_replicas,
                "new_replicas": replicas,
                "current_replicas": scaled_replicaset.spec.replicas,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "ReplicaSet不存在",
                    "message": f"在命名空间 {namespace} 中找不到ReplicaSet {resource_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "伸缩ReplicaSet失败",
                    "message": str(e),
                }
        except Exception as e:
            return {"success": False, "error": "伸缩ReplicaSet失败", "message": str(e)}

    async def _list_hpa(
        self, autoscaling_v1, autoscaling_v2, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取HPA列表"""
        try:
            namespace = parameters.get("namespace", "default")
            all_namespaces = parameters.get("all_namespaces", False)
            loop = asyncio.get_event_loop()

            # 优先使用v2 API
            if autoscaling_v2:
                if all_namespaces:
                    hpas = await loop.run_in_executor(
                        self._executor,
                        lambda: autoscaling_v2.list_horizontal_pod_autoscaler_for_all_namespaces(),
                    )
                else:
                    hpas = await loop.run_in_executor(
                        self._executor,
                        lambda: autoscaling_v2.list_namespaced_horizontal_pod_autoscaler(
                            namespace=namespace
                        ),
                    )
                is_v2 = True
            else:
                # 使用v1 API
                if all_namespaces:
                    hpas = await loop.run_in_executor(
                        self._executor,
                        lambda: autoscaling_v1.list_horizontal_pod_autoscaler_for_all_namespaces(),
                    )
                else:
                    hpas = await loop.run_in_executor(
                        self._executor,
                        lambda: autoscaling_v1.list_namespaced_horizontal_pod_autoscaler(
                            namespace=namespace
                        ),
                    )
                is_v2 = False

            # 格式化HPA信息
            hpa_list = []
            for hpa in hpas.items:
                hpa_info = {
                    "name": hpa.metadata.name,
                    "namespace": hpa.metadata.namespace,
                    "age": self._calculate_age(hpa.metadata.creation_timestamp),
                    "reference": {
                        "api_version": hpa.spec.scale_target_ref.api_version,
                        "kind": hpa.spec.scale_target_ref.kind,
                        "name": hpa.spec.scale_target_ref.name,
                    },
                    "min_replicas": hpa.spec.min_replicas,
                    "max_replicas": hpa.spec.max_replicas,
                    "current_replicas": hpa.status.current_replicas,
                    "desired_replicas": hpa.status.desired_replicas,
                }

                # 根据API版本添加不同信息
                if is_v2:
                    hpa_info["metrics"] = self._format_v2_metrics(
                        hpa.spec.metrics or []
                    )
                    hpa_info["current_metrics"] = self._format_v2_current_metrics(
                        hpa.status.current_metrics or []
                    )
                else:
                    hpa_info["target_cpu_percent"] = (
                        hpa.spec.target_cpu_utilization_percentage
                    )
                    hpa_info["current_cpu_percent"] = (
                        hpa.status.current_cpu_utilization_percentage
                    )

                hpa_list.append(hpa_info)

            return {
                "success": True,
                "operation": "list_hpa",
                "total_count": len(hpa_list),
                "hpas": hpa_list,
                "api_version": "v2" if is_v2 else "v1",
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

        except Exception as e:
            return {"success": False, "error": "获取HPA列表失败", "message": str(e)}

    async def _get_hpa_status(
        self, autoscaling_v1, autoscaling_v2, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取指定HPA的状态"""
        try:
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")

            if not resource_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_name是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 优先使用v2 API
            if autoscaling_v2:
                hpa = await loop.run_in_executor(
                    self._executor,
                    lambda: autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(
                        name=resource_name, namespace=namespace
                    ),
                )
                is_v2 = True
            else:
                hpa = await loop.run_in_executor(
                    self._executor,
                    lambda: autoscaling_v1.read_namespaced_horizontal_pod_autoscaler(
                        name=resource_name, namespace=namespace
                    ),
                )
                is_v2 = False

            # 格式化HPA详细信息
            hpa_status = {
                "name": hpa.metadata.name,
                "namespace": hpa.metadata.namespace,
                "uid": hpa.metadata.uid,
                "creation_timestamp": hpa.metadata.creation_timestamp.isoformat(),
                "age": self._calculate_age(hpa.metadata.creation_timestamp),
                "labels": hpa.metadata.labels or {},
                "spec": {
                    "scale_target_ref": {
                        "api_version": hpa.spec.scale_target_ref.api_version,
                        "kind": hpa.spec.scale_target_ref.kind,
                        "name": hpa.spec.scale_target_ref.name,
                    },
                    "min_replicas": hpa.spec.min_replicas,
                    "max_replicas": hpa.spec.max_replicas,
                },
                "status": {
                    "current_replicas": hpa.status.current_replicas,
                    "desired_replicas": hpa.status.desired_replicas,
                    "last_scale_time": (
                        hpa.status.last_scale_time.isoformat()
                        if hpa.status.last_scale_time
                        else None
                    ),
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
                        for cond in hpa.status.conditions or []
                    ],
                },
            }

            # 根据API版本添加不同信息
            if is_v2:
                hpa_status["spec"]["metrics"] = self._format_v2_metrics(
                    hpa.spec.metrics or []
                )
                hpa_status["status"]["current_metrics"] = (
                    self._format_v2_current_metrics(hpa.status.current_metrics or [])
                )
            else:
                hpa_status["spec"][
                    "target_cpu_percent"
                ] = hpa.spec.target_cpu_utilization_percentage
                hpa_status["status"][
                    "current_cpu_percent"
                ] = hpa.status.current_cpu_utilization_percentage

            return {
                "success": True,
                "operation": "get_hpa_status",
                "hpa_status": hpa_status,
                "api_version": "v2" if is_v2 else "v1",
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "HPA不存在",
                    "message": f"在命名空间 {namespace} 中找不到HPA {resource_name}",
                }
            else:
                return {"success": False, "error": "获取HPA状态失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "获取HPA状态失败", "message": str(e)}

    async def _create_hpa(
        self, autoscaling_v1, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """创建HPA"""
        try:
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")
            hpa_config = parameters.get("hpa_config", {})

            if not resource_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_name是必需的参数",
                }

            min_replicas = hpa_config.get("min_replicas", 1)
            max_replicas = hpa_config.get("max_replicas", 10)
            target_cpu_percent = hpa_config.get("target_cpu_percent", 80)

            # 构建HPA对象
            hpa_body = client.V1HorizontalPodAutoscaler(
                api_version="autoscaling/v1",
                kind="HorizontalPodAutoscaler",
                metadata=client.V1ObjectMeta(
                    name=f"{resource_name}-hpa", namespace=namespace
                ),
                spec=client.V1HorizontalPodAutoscalerSpec(
                    scale_target_ref=client.V1CrossVersionObjectReference(
                        api_version="apps/v1", kind="Deployment", name=resource_name
                    ),
                    min_replicas=min_replicas,
                    max_replicas=max_replicas,
                    target_cpu_utilization_percentage=target_cpu_percent,
                ),
            )

            loop = asyncio.get_event_loop()

            # 创建HPA
            created_hpa = await loop.run_in_executor(
                self._executor,
                lambda: autoscaling_v1.create_namespaced_horizontal_pod_autoscaler(
                    namespace=namespace, body=hpa_body
                ),
            )

            return {
                "success": True,
                "operation": "create_hpa",
                "message": f"HPA {created_hpa.metadata.name} 在命名空间 {namespace} 中创建成功",
                "hpa_name": created_hpa.metadata.name,
                "namespace": namespace,
                "target_deployment": resource_name,
                "min_replicas": min_replicas,
                "max_replicas": max_replicas,
                "target_cpu_percent": target_cpu_percent,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

        except ApiException as e:
            if e.status == 409:
                return {
                    "success": False,
                    "error": "HPA已存在",
                    "message": f"命名空间 {namespace} 中已存在同名HPA",
                }
            else:
                return {"success": False, "error": "创建HPA失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "创建HPA失败", "message": str(e)}

    async def _delete_hpa(
        self, autoscaling_v1, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """删除HPA"""
        try:
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")

            if not resource_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_name是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 删除HPA
            await loop.run_in_executor(
                self._executor,
                lambda: autoscaling_v1.delete_namespaced_horizontal_pod_autoscaler(
                    name=resource_name, namespace=namespace
                ),
            )

            return {
                "success": True,
                "operation": "delete_hpa",
                "message": f"HPA {resource_name} 在命名空间 {namespace} 中已成功删除",
                "hpa_name": resource_name,
                "namespace": namespace,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "HPA不存在",
                    "message": f"在命名空间 {namespace} 中找不到HPA {resource_name}",
                }
            else:
                return {"success": False, "error": "删除HPA失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "删除HPA失败", "message": str(e)}

    def _format_v2_metrics(self, metrics: List) -> List[Dict[str, Any]]:
        """格式化v2 API的目标指标"""
        formatted_metrics = []
        for metric in metrics:
            if hasattr(metric, "resource"):
                formatted_metrics.append(
                    {
                        "type": "Resource",
                        "resource": {
                            "name": metric.resource.name,
                            "target": (
                                metric.resource.target.to_dict()
                                if metric.resource.target
                                else {}
                            ),
                        },
                    }
                )
            elif hasattr(metric, "pods"):
                formatted_metrics.append(
                    {
                        "type": "Pods",
                        "pods": metric.pods.to_dict() if metric.pods else {},
                    }
                )
        return formatted_metrics

    def _format_v2_current_metrics(self, metrics: List) -> List[Dict[str, Any]]:
        """格式化v2 API的当前指标"""
        formatted_metrics = []
        for metric in metrics:
            if hasattr(metric, "resource"):
                formatted_metrics.append(
                    {
                        "type": "Resource",
                        "resource": {
                            "name": metric.resource.name,
                            "current": (
                                metric.resource.current.to_dict()
                                if metric.resource.current
                                else {}
                            ),
                        },
                    }
                )
        return formatted_metrics
