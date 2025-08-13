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


class K8sServiceTool(K8sBaseTool):
    """k8s Service管理工具"""

    def __init__(self):
        super().__init__(
            name="k8s_service_management",
            description="k8s Service管理工具，支持查看Service列表、获取详情、创建和删除Service等操作",
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
                        "list_services",
                        "get_service_details",
                        "create_service",
                        "delete_service",
                        "get_endpoints",
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
                "service_name": {
                    "type": "string",
                    "description": "Service名称（部分操作需要）",
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
                "service_config": {
                    "type": "object",
                    "description": "创建Service时的配置",
                    "properties": {
                        "name": {"type": "string", "description": "Service名称"},
                        "type": {
                            "type": "string",
                            "enum": [
                                "ClusterIP",
                                "NodePort",
                                "LoadBalancer",
                                "ExternalName",
                            ],
                            "description": "Service类型",
                            "default": "ClusterIP",
                        },
                        "selector": {
                            "type": "object",
                            "description": "Pod选择器标签",
                            "additionalProperties": {"type": "string"},
                        },
                        "ports": {
                            "type": "array",
                            "description": "端口配置",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {
                                        "type": "string",
                                        "description": "端口名称",
                                    },
                                    "port": {
                                        "type": "integer",
                                        "description": "Service端口",
                                    },
                                    "target_port": {
                                        "type": "integer",
                                        "description": "目标端口",
                                    },
                                    "protocol": {
                                        "type": "string",
                                        "enum": ["TCP", "UDP"],
                                        "default": "TCP",
                                    },
                                },
                                "required": ["port"],
                            },
                        },
                        "external_name": {
                            "type": "string",
                            "description": "ExternalName类型的外部名称",
                        },
                    },
                },
            },
            "required": ["operation"],
        }

    async def _execute_internal(self, parameters: Dict[str, Any]) -> Any:
        """执行工具内部逻辑"""
        operation = parameters.get("operation")
        config_path = parameters.get("config_path")

        # 初始化API客户端
        clients = self._initialize_clients(config_path)
        v1 = clients["v1"]

        # 根据操作类型执行相应的方法
        if operation == "list_services":
            return await self._list_services(v1, parameters)
        elif operation == "get_service_details":
            return await self._get_service_details(v1, parameters)
        elif operation == "create_service":
            return await self._create_service(v1, parameters)
        elif operation == "delete_service":
            return await self._delete_service(v1, parameters)
        elif operation == "get_endpoints":
            return await self._get_endpoints(v1, parameters)
        else:
            return {
                "success": False,
                "error": "不支持的操作",
                "message": f"未知的操作类型: {operation}",
            }

    async def _list_services(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Service列表"""
        try:
            namespace = parameters.get("namespace", "default")
            all_namespaces = parameters.get("all_namespaces", False)
            max_results = parameters.get("max_results", 50)

            loop = asyncio.get_event_loop()

            # 根据参数获取Service列表
            if all_namespaces:
                services = await loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_service_for_all_namespaces(limit=max_results),
                )
            else:
                services = await loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_namespaced_service(
                        namespace=namespace, limit=max_results
                    ),
                )

            # 格式化Service信息
            service_list = []
            for svc in services.items:
                service_info = {
                    "name": svc.metadata.name,
                    "namespace": svc.metadata.namespace,
                    "type": svc.spec.type,
                    "cluster_ip": svc.spec.cluster_ip,
                    "external_ips": svc.spec.external_i_ps or [],
                    "ports": [
                        {
                            "name": port.name,
                            "port": port.port,
                            "target_port": port.target_port,
                            "protocol": port.protocol,
                            "node_port": port.node_port,
                        }
                        for port in svc.spec.ports or []
                    ],
                    "selector": svc.spec.selector or {},
                    "session_affinity": svc.spec.session_affinity,
                    "age": self._calculate_age(svc.metadata.creation_timestamp),
                    "labels": svc.metadata.labels or {},
                }

                # 添加LoadBalancer特有信息
                if svc.spec.type == "LoadBalancer" and svc.status.load_balancer:
                    ingress_list = svc.status.load_balancer.ingress or []
                    service_info["load_balancer_ingress"] = [
                        {"ip": ing.ip, "hostname": ing.hostname} for ing in ingress_list
                    ]

                service_list.append(service_info)

            return {
                "success": True,
                "operation": "list_services",
                "total_count": len(service_list),
                "services": service_list,
            "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {
                "success": False,
                "error": "获取Service列表失败",
                "message": str(e),
            "timestamp": iso_utc_now(),
            }

    async def _get_service_details(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Service详细信息"""
        try:
            service_name = parameters.get("service_name")
            namespace = parameters.get("namespace", "default")

            if not service_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "Service名称是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取Service详细信息
            service = await loop.run_in_executor(
                self._executor,
                lambda: v1.read_namespaced_service(
                    name=service_name, namespace=namespace
                ),
            )

            # 格式化详细信息
            service_details = {
                "name": service.metadata.name,
                "namespace": service.metadata.namespace,
                "uid": service.metadata.uid,
                "creation_timestamp": service.metadata.creation_timestamp.isoformat(),
                "age": self._calculate_age(service.metadata.creation_timestamp),
                "labels": service.metadata.labels or {},
                "annotations": service.metadata.annotations or {},
                "spec": {
                    "type": service.spec.type,
                    "cluster_ip": service.spec.cluster_ip,
                    "cluster_ips": service.spec.cluster_i_ps or [],
                    "external_ips": service.spec.external_i_ps or [],
                    "session_affinity": service.spec.session_affinity,
                    "external_name": service.spec.external_name,
                    "external_traffic_policy": service.spec.external_traffic_policy,
                    "ip_families": service.spec.ip_families or [],
                    "ip_family_policy": service.spec.ip_family_policy,
                    "selector": service.spec.selector or {},
                    "ports": [
                        {
                            "name": port.name,
                            "port": port.port,
                            "target_port": str(port.target_port),
                            "protocol": port.protocol,
                            "node_port": port.node_port,
                            "app_protocol": port.app_protocol,
                        }
                        for port in service.spec.ports or []
                    ],
                },
                "status": {},
            }

            # 添加LoadBalancer状态信息
            if service.status.load_balancer:
                ingress_list = service.status.load_balancer.ingress or []
                service_details["status"]["load_balancer"] = {
                    "ingress": [
                        {
                            "ip": ing.ip,
                            "hostname": ing.hostname,
                            "ports": (
                                [
                                    {
                                        "port": port.port,
                                        "protocol": port.protocol,
                                        "error": port.error,
                                    }
                                    for port in ing.ports or []
                                ]
                                if ing.ports
                                else []
                            ),
                        }
                        for ing in ingress_list
                    ]
                }

            return {
                "success": True,
                "operation": "get_service_details",
                "service_details": service_details,
            "timestamp": iso_utc_now(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Service不存在",
                    "message": f"在命名空间 {namespace} 中找不到Service {service_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "获取Service详情失败",
                    "message": str(e),
                }
        except Exception as e:
            return {"success": False, "error": "获取Service详情失败", "message": str(e)}

    async def _create_service(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """创建Service"""
        try:
            namespace = parameters.get("namespace", "default")
            service_config = parameters.get("service_config")

            if not service_config:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "service_config参数是必需的",
                }

            service_name = service_config.get("name")
            if not service_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "service_config中的name是必需的",
                }

            # 构建Service对象
            service_body = client.V1Service(
                api_version="v1",
                kind="Service",
                metadata=client.V1ObjectMeta(
                    name=service_name,
                    namespace=namespace,
                    labels=service_config.get("labels", {}),
                ),
                spec=client.V1ServiceSpec(
                    type=service_config.get("type", "ClusterIP"),
                    selector=service_config.get("selector", {}),
                    ports=[
                        client.V1ServicePort(
                            name=port.get("name"),
                            port=port["port"],
                            target_port=port.get("target_port", port["port"]),
                            protocol=port.get("protocol", "TCP"),
                            node_port=port.get("node_port"),
                        )
                        for port in service_config.get("ports", [])
                    ],
                    external_name=service_config.get("external_name"),
                ),
            )

            loop = asyncio.get_event_loop()

            # 创建Service
            created_service = await loop.run_in_executor(
                self._executor,
                lambda: v1.create_namespaced_service(
                    namespace=namespace, body=service_body
                ),
            )

            return {
                "success": True,
                "operation": "create_service",
                "message": f"Service {service_name} 在命名空间 {namespace} 中创建成功",
                "service_name": created_service.metadata.name,
                "namespace": created_service.metadata.namespace,
                "cluster_ip": created_service.spec.cluster_ip,
                "type": created_service.spec.type,
            "timestamp": iso_utc_now(),
            }

        except ApiException as e:
            if e.status == 409:
                return {
                    "success": False,
                    "error": "Service已存在",
                    "message": f"命名空间 {namespace} 中已存在同名Service",
                }
            else:
                return {"success": False, "error": "创建Service失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "创建Service失败", "message": str(e)}

    async def _delete_service(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """删除Service"""
        try:
            service_name = parameters.get("service_name")
            namespace = parameters.get("namespace", "default")

            if not service_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "Service名称是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 删除Service
            await loop.run_in_executor(
                self._executor,
                lambda: v1.delete_namespaced_service(
                    name=service_name, namespace=namespace
                ),
            )

            return {
                "success": True,
                "operation": "delete_service",
                "message": f"Service {service_name} 在命名空间 {namespace} 中已成功删除",
                "service_name": service_name,
                "namespace": namespace,
            "timestamp": iso_utc_now(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Service不存在",
                    "message": f"在命名空间 {namespace} 中找不到Service {service_name}",
                }
            else:
                return {"success": False, "error": "删除Service失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "删除Service失败", "message": str(e)}

    async def _get_endpoints(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Service的端点信息"""
        try:
            service_name = parameters.get("service_name")
            namespace = parameters.get("namespace", "default")

            if not service_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "Service名称是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取Endpoints
            try:
                endpoints = await loop.run_in_executor(
                    self._executor,
                    lambda: v1.read_namespaced_endpoints(
                        name=service_name, namespace=namespace
                    ),
                )
            except ApiException as e:
                if e.status == 404:
                    return {
                        "success": True,
                        "operation": "get_endpoints",
                        "service_name": service_name,
                        "namespace": namespace,
                        "message": "Service存在但没有可用的端点",
                        "subsets": [],
            "timestamp": iso_utc_now(),
                    }
                raise

            # 格式化端点信息
            subsets = []
            for subset in endpoints.subsets or []:
                subset_info = {
                    "addresses": [
                        {
                            "ip": addr.ip,
                            "hostname": addr.hostname,
                            "target_ref": (
                                {
                                    "kind": addr.target_ref.kind,
                                    "name": addr.target_ref.name,
                                    "namespace": addr.target_ref.namespace,
                                }
                                if addr.target_ref
                                else None
                            ),
                        }
                        for addr in subset.addresses or []
                    ],
                    "not_ready_addresses": [
                        {
                            "ip": addr.ip,
                            "hostname": addr.hostname,
                            "target_ref": (
                                {
                                    "kind": addr.target_ref.kind,
                                    "name": addr.target_ref.name,
                                    "namespace": addr.target_ref.namespace,
                                }
                                if addr.target_ref
                                else None
                            ),
                        }
                        for addr in subset.not_ready_addresses or []
                    ],
                    "ports": [
                        {
                            "name": port.name,
                            "port": port.port,
                            "protocol": port.protocol,
                            "app_protocol": port.app_protocol,
                        }
                        for port in subset.ports or []
                    ],
                }
                subsets.append(subset_info)

            return {
                "success": True,
                "operation": "get_endpoints",
                "service_name": service_name,
                "namespace": namespace,
                "subsets": subsets,
                "ready_endpoints": sum(len(s["addresses"]) for s in subsets),
                "not_ready_endpoints": sum(
                    len(s["not_ready_addresses"]) for s in subsets
                ),
                "timestamp": datetime.now(UTC_TZ).isoformat(),
            }

        except Exception as e:
            return {"success": False, "error": "获取端点信息失败", "message": str(e)}
