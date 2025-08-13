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


class K8sMonitorTool(K8sBaseTool):
    """k8s资源监控工具"""

    def __init__(self):
        super().__init__(
            name="k8s_resource_monitor",
            description="k8s资源监控工具，支持查看节点和Pod的资源使用情况、资源配额等",
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
                        "get_node_metrics",
                        "get_pod_metrics",
                        "get_resource_quotas",
                        "get_limit_ranges",
                        "get_top_pods",
                        "get_top_nodes",
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
                "all_namespaces": {
                    "type": "boolean",
                    "description": "是否查看所有命名空间",
                    "default": False,
                },
                "node_name": {"type": "string", "description": "节点名称（可选）"},
                "pod_name": {"type": "string", "description": "Pod名称（可选）"},
                "max_results": {
                    "type": "integer",
                    "description": "最大返回结果数",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
            },
            "required": ["operation"],
        }

    def _create_api_clients(self) -> Dict[str, client.ApiClient]:
        """创建Kubernetes API客户端"""
        return {"v1": client.CoreV1Api(), "metrics_v1beta1": client.MetricsV1beta1Api()}

    async def _execute_internal(self, parameters: Dict[str, Any]) -> Any:
        """执行工具内部逻辑"""
        operation = parameters.get("operation")
        config_path = parameters.get("config_path")

        # 初始化API客户端
        clients = self._initialize_clients(config_path)
        v1 = clients["v1"]

        # 尝试初始化metrics API
        try:
            metrics_api = clients["metrics_v1beta1"]
        except Exception:
            metrics_api = None

        # 根据操作类型执行相应的方法
        operation_map = {
            "get_node_metrics": lambda: self._get_node_metrics(metrics_api, parameters),
            "get_pod_metrics": lambda: self._get_pod_metrics(metrics_api, parameters),
            "get_resource_quotas": lambda: self._get_resource_quotas(v1, parameters),
            "get_limit_ranges": lambda: self._get_limit_ranges(v1, parameters),
            "get_top_pods": lambda: self._get_top_pods(metrics_api, v1, parameters),
            "get_top_nodes": lambda: self._get_top_nodes(metrics_api, v1, parameters),
        }

        if operation in operation_map:
            return await operation_map[operation]()
        else:
            return {
                "success": False,
                "error": "不支持的操作",
                "message": f"未知的操作类型: {operation}",
            }

    async def _get_node_metrics(
        self, metrics_api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取节点资源使用指标"""
        try:
            if not metrics_api:
                return {
                    "success": False,
                    "error": "Metrics API不可用",
                    "message": "集群中没有安装Metrics Server，无法获取资源使用指标",
                }

            node_name = parameters.get("node_name")
            loop = asyncio.get_event_loop()

            if node_name:
                # 获取指定节点的指标
                try:
                    node_metrics = await loop.run_in_executor(
                        self._executor,
                        lambda: metrics_api.read_node_metrics(name=node_name),
                    )
                    node_metrics_list = [node_metrics]
                except ApiException as e:
                    if e.status == 404:
                        return {
                            "success": False,
                            "error": "节点不存在",
                            "message": f"找不到节点 {node_name}",
                        }
                    raise
            else:
                # 获取所有节点的指标
                node_metrics_response = await loop.run_in_executor(
                    self._executor, lambda: metrics_api.list_node_metrics()
                )
                node_metrics_list = node_metrics_response.items

            # 格式化节点指标
            nodes_data = []
            for node_metric in node_metrics_list:
                node_data = {
                    "name": node_metric.metadata.name,
                    "timestamp": node_metric.timestamp.isoformat(),
                    "window": node_metric.window,
                    "usage": {
                        "cpu": node_metric.usage.get("cpu", "0"),
                        "memory": node_metric.usage.get("memory", "0"),
                    },
                    "cpu_cores": self._parse_cpu_resource(
                        node_metric.usage.get("cpu", "0")
                    ),
                    "memory_bytes": self._parse_memory_resource(
                        node_metric.usage.get("memory", "0")
                    ),
                }
                nodes_data.append(node_data)

            return {
                "success": True,
                "operation": "get_node_metrics",
                "node_count": len(nodes_data),
                "nodes": nodes_data,
            "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "获取节点指标失败", "message": str(e)}

    async def _get_pod_metrics(
        self, metrics_api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Pod资源使用指标"""
        try:
            if not metrics_api:
                return {
                    "success": False,
                    "error": "Metrics API不可用",
                    "message": "集群中没有安装Metrics Server，无法获取资源使用指标",
                }

            namespace = parameters.get("namespace", "default")
            pod_name = parameters.get("pod_name")
            all_namespaces = parameters.get("all_namespaces", False)
            max_results = parameters.get("max_results", 20)
            loop = asyncio.get_event_loop()

            if pod_name and not all_namespaces:
                # 获取指定Pod的指标
                try:
                    pod_metrics = await loop.run_in_executor(
                        self._executor,
                        lambda: metrics_api.read_namespaced_pod_metrics(
                            name=pod_name, namespace=namespace
                        ),
                    )
                    pod_metrics_list = [pod_metrics]
                except ApiException as e:
                    if e.status == 404:
                        return {
                            "success": False,
                            "error": "Pod不存在",
                            "message": f"在命名空间 {namespace} 中找不到Pod {pod_name}",
                        }
                    raise
            elif all_namespaces:
                # 获取所有命名空间的Pod指标
                pod_metrics_response = await loop.run_in_executor(
                    self._executor,
                    lambda: metrics_api.list_pod_metrics_for_all_namespaces(
                        limit=max_results
                    ),
                )
                pod_metrics_list = pod_metrics_response.items
            else:
                # 获取指定命名空间的Pod指标
                pod_metrics_response = await loop.run_in_executor(
                    self._executor,
                    lambda: metrics_api.list_namespaced_pod_metrics(
                        namespace=namespace, limit=max_results
                    ),
                )
                pod_metrics_list = pod_metrics_response.items

            # 格式化Pod指标
            pods_data = []
            for pod_metric in pod_metrics_list:
                containers_usage = []
                total_cpu = 0
                total_memory = 0

                for container in pod_metric.containers:
                    cpu_cores = self._parse_cpu_resource(
                        container.usage.get("cpu", "0")
                    )
                    memory_bytes = self._parse_memory_resource(
                        container.usage.get("memory", "0")
                    )

                    containers_usage.append(
                        {
                            "name": container.name,
                            "usage": {
                                "cpu": container.usage.get("cpu", "0"),
                                "memory": container.usage.get("memory", "0"),
                            },
                            "cpu_cores": cpu_cores,
                            "memory_bytes": memory_bytes,
                        }
                    )

                    total_cpu += cpu_cores
                    total_memory += memory_bytes

                pod_data = {
                    "name": pod_metric.metadata.name,
                    "namespace": pod_metric.metadata.namespace,
                    "timestamp": pod_metric.timestamp.isoformat(),
                    "window": pod_metric.window,
                    "containers": containers_usage,
                    "total_usage": {
                        "cpu_cores": total_cpu,
                        "memory_bytes": total_memory,
                        "memory_mb": round(total_memory / 1024 / 1024, 2),
                    },
                }
                pods_data.append(pod_data)

            return {
                "success": True,
                "operation": "get_pod_metrics",
                "pod_count": len(pods_data),
                "pods": pods_data,
            "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "获取Pod指标失败", "message": str(e)}

    async def _get_resource_quotas(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取资源配额信息"""
        try:
            namespace = parameters.get("namespace", "default")
            all_namespaces = parameters.get("all_namespaces", False)
            loop = asyncio.get_event_loop()

            if all_namespaces:
                quotas = await loop.run_in_executor(
                    self._executor, lambda: v1.list_resource_quota_for_all_namespaces()
                )
            else:
                quotas = await loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_namespaced_resource_quota(namespace=namespace),
                )

            # 格式化资源配额信息
            quotas_data = []
            for quota in quotas.items:
                quota_data = {
                    "name": quota.metadata.name,
                    "namespace": quota.metadata.namespace,
                    "age": self._calculate_age(quota.metadata.creation_timestamp),
                    "scopes": quota.spec.scopes or [],
                    "hard": dict(quota.spec.hard) if quota.spec.hard else {},
                    "used": dict(quota.status.used) if quota.status.used else {},
                    "scope_selector": (
                        quota.spec.scope_selector.match_expressions
                        if quota.spec.scope_selector
                        else []
                    ),
                }
                quotas_data.append(quota_data)

            return {
                "success": True,
                "operation": "get_resource_quotas",
                "quota_count": len(quotas_data),
                "quotas": quotas_data,
            "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "获取资源配额失败", "message": str(e)}

    async def _get_limit_ranges(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取资源限制范围信息"""
        try:
            namespace = parameters.get("namespace", "default")
            all_namespaces = parameters.get("all_namespaces", False)
            loop = asyncio.get_event_loop()

            if all_namespaces:
                limit_ranges = await loop.run_in_executor(
                    self._executor, lambda: v1.list_limit_range_for_all_namespaces()
                )
            else:
                limit_ranges = await loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_namespaced_limit_range(namespace=namespace),
                )

            # 格式化限制范围信息
            limit_ranges_data = []
            for lr in limit_ranges.items:
                limits = []
                for limit in lr.spec.limits:
                    limit_data = {
                        "type": limit.type,
                        "max": dict(limit.max) if limit.max else {},
                        "min": dict(limit.min) if limit.min else {},
                        "default": dict(limit.default) if limit.default else {},
                        "default_request": (
                            dict(limit.default_request) if limit.default_request else {}
                        ),
                        "max_limit_request_ratio": (
                            dict(limit.max_limit_request_ratio)
                            if limit.max_limit_request_ratio
                            else {}
                        ),
                    }
                    limits.append(limit_data)

                lr_data = {
                    "name": lr.metadata.name,
                    "namespace": lr.metadata.namespace,
                    "age": self._calculate_age(lr.metadata.creation_timestamp),
                    "limits": limits,
                }
                limit_ranges_data.append(lr_data)

            return {
                "success": True,
                "operation": "get_limit_ranges",
                "limit_range_count": len(limit_ranges_data),
                "limit_ranges": limit_ranges_data,
            "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {
                "success": False,
                "error": "获取资源限制范围失败",
                "message": str(e),
            }

    async def _get_top_pods(
        self, metrics_api, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取资源使用Top Pod列表"""
        try:
            if not metrics_api:
                return {
                    "success": False,
                    "error": "Metrics API不可用",
                    "message": "集群中没有安装Metrics Server，无法获取资源使用指标",
                }

            namespace = parameters.get("namespace", "default")
            all_namespaces = parameters.get("all_namespaces", False)
            max_results = parameters.get("max_results", 20)
            loop = asyncio.get_event_loop()

            # 获取Pod指标
            if all_namespaces:
                pod_metrics_response = await loop.run_in_executor(
                    self._executor,
                    lambda: metrics_api.list_pod_metrics_for_all_namespaces(),
                )
            else:
                pod_metrics_response = await loop.run_in_executor(
                    self._executor,
                    lambda: metrics_api.list_namespaced_pod_metrics(
                        namespace=namespace
                    ),
                )

            # 计算Pod总使用量并排序
            pods_usage = []
            for pod_metric in pod_metrics_response.items:
                total_cpu = 0
                total_memory = 0

                for container in pod_metric.containers:
                    total_cpu += self._parse_cpu_resource(
                        container.usage.get("cpu", "0")
                    )
                    total_memory += self._parse_memory_resource(
                        container.usage.get("memory", "0")
                    )

                pods_usage.append(
                    {
                        "name": pod_metric.metadata.name,
                        "namespace": pod_metric.metadata.namespace,
                        "cpu_cores": total_cpu,
                        "memory_bytes": total_memory,
                        "memory_mb": round(total_memory / 1024 / 1024, 2),
                    }
                )

            # 按CPU使用量排序
            top_cpu_pods = sorted(
                pods_usage, key=lambda x: x["cpu_cores"], reverse=True
            )[:max_results]

            # 按内存使用量排序
            top_memory_pods = sorted(
                pods_usage, key=lambda x: x["memory_bytes"], reverse=True
            )[:max_results]

            return {
                "success": True,
                "operation": "get_top_pods",
                "total_pods": len(pods_usage),
                "top_cpu_pods": top_cpu_pods,
                "top_memory_pods": top_memory_pods,
            "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "获取Top Pod失败", "message": str(e)}

    async def _get_top_nodes(
        self, metrics_api, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取资源使用Top Node列表"""
        try:
            if not metrics_api:
                return {
                    "success": False,
                    "error": "Metrics API不可用",
                    "message": "集群中没有安装Metrics Server，无法获取资源使用指标",
                }

            max_results = parameters.get("max_results", 20)
            loop = asyncio.get_event_loop()

            # 获取节点指标
            node_metrics_response = await loop.run_in_executor(
                self._executor, lambda: metrics_api.list_node_metrics()
            )

            # 计算节点使用量
            nodes_usage = []
            for node_metric in node_metrics_response.items:
                cpu_cores = self._parse_cpu_resource(node_metric.usage.get("cpu", "0"))
                memory_bytes = self._parse_memory_resource(
                    node_metric.usage.get("memory", "0")
                )

                nodes_usage.append(
                    {
                        "name": node_metric.metadata.name,
                        "cpu_cores": cpu_cores,
                        "memory_bytes": memory_bytes,
                        "memory_gb": round(memory_bytes / 1024 / 1024 / 1024, 2),
                    }
                )

            # 按CPU使用量排序
            top_cpu_nodes = sorted(
                nodes_usage, key=lambda x: x["cpu_cores"], reverse=True
            )[:max_results]

            # 按内存使用量排序
            top_memory_nodes = sorted(
                nodes_usage, key=lambda x: x["memory_bytes"], reverse=True
            )[:max_results]

            return {
                "success": True,
                "operation": "get_top_nodes",
                "total_nodes": len(nodes_usage),
                "top_cpu_nodes": top_cpu_nodes,
                "top_memory_nodes": top_memory_nodes,
                "timestamp": datetime.now(UTC_TZ).isoformat(),
            }

        except Exception as e:
            return {"success": False, "error": "获取Top Node失败", "message": str(e)}

    def _parse_cpu_resource(self, cpu_str: str) -> float:
        """解析CPU资源字符串为核心数"""
        if not cpu_str or cpu_str == "0":
            return 0.0

        cpu_str = str(cpu_str).strip()

        # 处理不同的CPU单位
        if cpu_str.endswith("n"):  # 纳核 (nanocores)
            return float(cpu_str[:-1]) / 1000000000
        elif cpu_str.endswith("u"):  # 微核 (microcores)
            return float(cpu_str[:-1]) / 1000000
        elif cpu_str.endswith("m"):  # 毫核 (millicores)
            return float(cpu_str[:-1]) / 1000
        else:
            # 假设是核心数
            return float(cpu_str)

    def _parse_memory_resource(self, memory_str: str) -> int:
        """解析内存资源字符串为字节数"""
        if not memory_str or memory_str == "0":
            return 0

        memory_str = str(memory_str).strip()

        # 处理不同的内存单位
        multipliers = {
            "Ki": 1024,
            "Mi": 1024 * 1024,
            "Gi": 1024 * 1024 * 1024,
            "Ti": 1024 * 1024 * 1024 * 1024,
            "k": 1000,
            "M": 1000 * 1000,
            "G": 1000 * 1000 * 1000,
            "T": 1000 * 1000 * 1000 * 1000,
        }

        for suffix, multiplier in multipliers.items():
            if memory_str.endswith(suffix):
                return int(float(memory_str[: -len(suffix)]) * multiplier)

        # 假设是字节数
        return int(memory_str)
