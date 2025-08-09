#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops MCP k8sIngress管理工具
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: k8sIngress管理的MCP工具，提供Ingress路由的查看、创建、管理功能
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from kubernetes import client
from kubernetes.client.rest import ApiException

from .k8s_base_tool import K8sBaseTool

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))


class K8sIngressTool(K8sBaseTool):
    """k8sIngress管理工具"""

    def __init__(self):
        super().__init__(
            name="k8s_ingress_management",
            description="k8sIngress管理工具，支持Ingress资源的查看、创建、更新和删除",
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
                        "list_ingresses",
                        "get_ingress",
                        "create_ingress",
                        "delete_ingress",
                        "get_ingress_classes",
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
                "ingress_name": {"type": "string", "description": "Ingress名称"},
                "all_namespaces": {
                    "type": "boolean",
                    "description": "是否查看所有命名空间",
                    "default": False,
                },
                "ingress_config": {
                    "type": "object",
                    "description": "Ingress配置（创建Ingress时使用）",
                    "properties": {
                        "name": {"type": "string", "description": "Ingress名称"},
                        "ingress_class": {
                            "type": "string",
                            "description": "Ingress类名称",
                        },
                        "rules": {
                            "type": "array",
                            "description": "路由规则",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "host": {"type": "string", "description": "主机名"},
                                    "paths": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "path": {
                                                    "type": "string",
                                                    "description": "路径",
                                                },
                                                "path_type": {
                                                    "type": "string",
                                                    "enum": [
                                                        "Exact",
                                                        "Prefix",
                                                        "ImplementationSpecific",
                                                    ],
                                                    "default": "Prefix",
                                                },
                                                "service_name": {
                                                    "type": "string",
                                                    "description": "服务名称",
                                                },
                                                "service_port": {
                                                    "type": "integer",
                                                    "description": "服务端口",
                                                },
                                            },
                                            "required": [
                                                "path",
                                                "service_name",
                                                "service_port",
                                            ],
                                        },
                                    },
                                },
                                "required": ["paths"],
                            },
                        },
                        "tls": {
                            "type": "array",
                            "description": "TLS配置",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "hosts": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "secret_name": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            "required": ["operation"],
        }

    def _create_api_clients(self) -> Dict[str, client.ApiClient]:
        """创建Kubernetes API客户端"""
        return {"networking_v1": client.NetworkingV1Api()}

    async def _execute_internal(self, parameters: Dict[str, Any]) -> Any:
        """执行工具内部逻辑"""
        operation = parameters.get("operation")
        config_path = parameters.get("config_path")

        # 初始化API客户端
        clients = self._initialize_clients(config_path)
        networking_v1 = clients["networking_v1"]

        # 根据操作类型执行相应的方法
        operation_map = {
            "list_ingresses": lambda: self._list_ingresses(networking_v1, parameters),
            "get_ingress": lambda: self._get_ingress(networking_v1, parameters),
            "create_ingress": lambda: self._create_ingress(networking_v1, parameters),
            "delete_ingress": lambda: self._delete_ingress(networking_v1, parameters),
            "get_ingress_classes": lambda: self._get_ingress_classes(
                networking_v1, parameters
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

    async def _list_ingresses(
        self, networking_v1: client.NetworkingV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Ingress列表"""
        try:
            namespace = parameters.get("namespace", "default")
            all_namespaces = parameters.get("all_namespaces", False)
            loop = asyncio.get_event_loop()

            # 根据参数获取Ingress列表
            if all_namespaces:
                ingresses = await loop.run_in_executor(
                    self._executor,
                    lambda: networking_v1.list_ingress_for_all_namespaces(),
                )
            else:
                ingresses = await loop.run_in_executor(
                    self._executor,
                    lambda: networking_v1.list_namespaced_ingress(namespace=namespace),
                )

            # 格式化Ingress信息
            ingress_list = []
            for ingress in ingresses.items:
                ingress_info = {
                    "name": ingress.metadata.name,
                    "namespace": ingress.metadata.namespace,
                    "age": self._calculate_age(ingress.metadata.creation_timestamp),
                    "ingress_class": ingress.spec.ingress_class_name,
                    "hosts": [],
                    "addresses": [],
                    "ports": [],
                }

                # 提取主机名
                if ingress.spec.rules:
                    for rule in ingress.spec.rules:
                        if rule.host:
                            ingress_info["hosts"].append(rule.host)

                # 提取地址信息
                if (
                    ingress.status.load_balancer
                    and ingress.status.load_balancer.ingress
                ):
                    for lb_ingress in ingress.status.load_balancer.ingress:
                        if lb_ingress.ip:
                            ingress_info["addresses"].append(lb_ingress.ip)
                        elif lb_ingress.hostname:
                            ingress_info["addresses"].append(lb_ingress.hostname)

                # TLS配置
                if ingress.spec.tls:
                    ingress_info["ports"].append("443")
                if ingress.spec.rules:
                    ingress_info["ports"].append("80")

                ingress_list.append(ingress_info)

            return {
                "success": True,
                "operation": "list_ingresses",
                "total_count": len(ingress_list),
                "ingresses": ingress_list,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except Exception as e:
            return {"success": False, "error": "获取Ingress列表失败", "message": str(e)}

    async def _get_ingress(
        self, networking_v1: client.NetworkingV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Ingress详细信息"""
        try:
            ingress_name = parameters.get("ingress_name")
            namespace = parameters.get("namespace", "default")

            if not ingress_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "ingress_name是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取Ingress详细信息
            ingress = await loop.run_in_executor(
                self._executor,
                lambda: networking_v1.read_namespaced_ingress(
                    name=ingress_name, namespace=namespace
                ),
            )

            # 格式化详细信息
            ingress_details = {
                "name": ingress.metadata.name,
                "namespace": ingress.metadata.namespace,
                "uid": ingress.metadata.uid,
                "creation_timestamp": ingress.metadata.creation_timestamp.isoformat(),
                "age": self._calculate_age(ingress.metadata.creation_timestamp),
                "labels": ingress.metadata.labels or {},
                "annotations": ingress.metadata.annotations or {},
                "spec": {
                    "ingress_class_name": ingress.spec.ingress_class_name,
                    "default_backend": None,
                    "rules": [],
                    "tls": [],
                },
                "status": {"load_balancer": {"ingress": []}},
            }

            # 默认后端
            if ingress.spec.default_backend:
                default_backend = ingress.spec.default_backend
                if default_backend.service:
                    ingress_details["spec"]["default_backend"] = {
                        "service": {
                            "name": default_backend.service.name,
                            "port": (
                                default_backend.service.port.number
                                if default_backend.service.port
                                else None
                            ),
                        }
                    }

            # 路由规则
            if ingress.spec.rules:
                for rule in ingress.spec.rules:
                    rule_info = {"host": rule.host, "http": {"paths": []}}

                    if rule.http and rule.http.paths:
                        for path in rule.http.paths:
                            path_info = {
                                "path": path.path,
                                "path_type": path.path_type,
                                "backend": (
                                    {
                                        "service": {
                                            "name": path.backend.service.name,
                                            "port": (
                                                path.backend.service.port.number
                                                if path.backend.service.port
                                                else None
                                            ),
                                        }
                                    }
                                    if path.backend.service
                                    else None
                                ),
                            }
                            rule_info["http"]["paths"].append(path_info)

                    ingress_details["spec"]["rules"].append(rule_info)

            # TLS配置
            if ingress.spec.tls:
                for tls in ingress.spec.tls:
                    tls_info = {
                        "hosts": tls.hosts or [],
                        "secret_name": tls.secret_name,
                    }
                    ingress_details["spec"]["tls"].append(tls_info)

            # 负载均衡器状态
            if ingress.status.load_balancer and ingress.status.load_balancer.ingress:
                for lb_ingress in ingress.status.load_balancer.ingress:
                    lb_info = {"ip": lb_ingress.ip, "hostname": lb_ingress.hostname}
                    ingress_details["status"]["load_balancer"]["ingress"].append(
                        lb_info
                    )

            return {
                "success": True,
                "operation": "get_ingress",
                "ingress_details": ingress_details,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Ingress不存在",
                    "message": f"在命名空间 {namespace} 中找不到Ingress {ingress_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "获取Ingress详情失败",
                    "message": str(e),
                }
        except Exception as e:
            return {"success": False, "error": "获取Ingress详情失败", "message": str(e)}

    async def _create_ingress(
        self, networking_v1: client.NetworkingV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """创建Ingress"""
        try:
            namespace = parameters.get("namespace", "default")
            ingress_config = parameters.get("ingress_config")

            if not ingress_config or not ingress_config.get("name"):
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "ingress_config及name字段是必需的",
                }

            ingress_name = ingress_config["name"]
            rules = ingress_config.get("rules", [])
            tls_config = ingress_config.get("tls", [])
            ingress_class = ingress_config.get("ingress_class")

            # 构建Ingress规则
            ingress_rules = []
            for rule_config in rules:
                paths = []
                for path_config in rule_config.get("paths", []):
                    path = client.V1HTTPIngressPath(
                        path=path_config["path"],
                        path_type=path_config.get("path_type", "Prefix"),
                        backend=client.V1IngressBackend(
                            service=client.V1IngressServiceBackend(
                                name=path_config["service_name"],
                                port=client.V1ServiceBackendPort(
                                    number=path_config["service_port"]
                                ),
                            )
                        ),
                    )
                    paths.append(path)

                rule = client.V1IngressRule(
                    host=rule_config.get("host"),
                    http=client.V1HTTPIngressRuleValue(paths=paths),
                )
                ingress_rules.append(rule)

            # 构建TLS配置
            ingress_tls = []
            for tls in tls_config:
                ingress_tls.append(
                    client.V1IngressTLS(
                        hosts=tls.get("hosts", []), secret_name=tls.get("secret_name")
                    )
                )

            # 构建Ingress对象
            ingress_body = client.V1Ingress(
                api_version="networking.k8s.io/v1",
                kind="Ingress",
                metadata=client.V1ObjectMeta(name=ingress_name, namespace=namespace),
                spec=client.V1IngressSpec(
                    ingress_class_name=ingress_class,
                    rules=ingress_rules,
                    tls=ingress_tls if ingress_tls else None,
                ),
            )

            loop = asyncio.get_event_loop()

            # 创建Ingress
            created_ingress = await loop.run_in_executor(
                self._executor,
                lambda: networking_v1.create_namespaced_ingress(
                    namespace=namespace, body=ingress_body
                ),
            )

            return {
                "success": True,
                "operation": "create_ingress",
                "message": f"Ingress {ingress_name} 在命名空间 {namespace} 中创建成功",
                "ingress_name": created_ingress.metadata.name,
                "namespace": created_ingress.metadata.namespace,
                "ingress_class": ingress_class,
                "rules_count": len(ingress_rules),
                "tls_count": len(ingress_tls),
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 409:
                return {
                    "success": False,
                    "error": "Ingress已存在",
                    "message": f"命名空间 {namespace} 中已存在同名Ingress",
                }
            else:
                return {"success": False, "error": "创建Ingress失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "创建Ingress失败", "message": str(e)}

    async def _delete_ingress(
        self, networking_v1: client.NetworkingV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """删除Ingress"""
        try:
            ingress_name = parameters.get("ingress_name")
            namespace = parameters.get("namespace", "default")

            if not ingress_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "ingress_name是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 删除Ingress
            await loop.run_in_executor(
                self._executor,
                lambda: networking_v1.delete_namespaced_ingress(
                    name=ingress_name, namespace=namespace
                ),
            )

            return {
                "success": True,
                "operation": "delete_ingress",
                "message": f"Ingress {ingress_name} 在命名空间 {namespace} 中已成功删除",
                "ingress_name": ingress_name,
                "namespace": namespace,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Ingress不存在",
                    "message": f"在命名空间 {namespace} 中找不到Ingress {ingress_name}",
                }
            else:
                return {"success": False, "error": "删除Ingress失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "删除Ingress失败", "message": str(e)}

    async def _get_ingress_classes(
        self, networking_v1: client.NetworkingV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取IngressClass列表"""
        try:
            loop = asyncio.get_event_loop()

            # 获取IngressClass列表
            ingress_classes = await loop.run_in_executor(
                self._executor, lambda: networking_v1.list_ingress_class()
            )

            # 格式化IngressClass信息
            classes_list = []
            for ic in ingress_classes.items:
                class_info = {
                    "name": ic.metadata.name,
                    "controller": ic.spec.controller,
                    "parameters": None,
                    "is_default": False,
                    "age": self._calculate_age(ic.metadata.creation_timestamp),
                }

                # 检查是否为默认IngressClass
                if ic.metadata.annotations:
                    is_default = ic.metadata.annotations.get(
                        "ingressclass.kubernetes.io/is-default-class", "false"
                    )
                    class_info["is_default"] = is_default.lower() == "true"

                # 参数信息
                if ic.spec.parameters:
                    class_info["parameters"] = {
                        "api_group": ic.spec.parameters.api_group,
                        "kind": ic.spec.parameters.kind,
                        "name": ic.spec.parameters.name,
                        "namespace": ic.spec.parameters.namespace,
                        "scope": ic.spec.parameters.scope,
                    }

                classes_list.append(class_info)

            return {
                "success": True,
                "operation": "get_ingress_classes",
                "total_count": len(classes_list),
                "ingress_classes": classes_list,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except Exception as e:
            return {
                "success": False,
                "error": "获取IngressClass列表失败",
                "message": str(e),
            }
