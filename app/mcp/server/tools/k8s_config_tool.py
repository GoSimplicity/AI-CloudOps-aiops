#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops MCP k8s配置管理工具
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: k8s配置管理的MCP工具，提供ConfigMap和Secret的管理操作
"""

import asyncio
import base64
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))

from kubernetes import client
from kubernetes.client.rest import ApiException

from .k8s_base_tool import K8sBaseTool


class K8sConfigTool(K8sBaseTool):
    """k8s配置管理工具"""

    def __init__(self):
        super().__init__(
            name="k8s_config_management",
            description="k8s配置管理工具，支持ConfigMap和Secret的查看、创建、更新、删除等操作",
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
                        "list_configmaps",
                        "list_secrets",
                        "get_configmap",
                        "get_secret",
                        "create_configmap",
                        "create_secret",
                        "update_configmap",
                        "update_secret",
                        "delete_configmap",
                        "delete_secret",
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
                    "description": "资源名称（ConfigMap或Secret名称）",
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
                "data": {
                    "type": "object",
                    "description": "配置数据，键值对格式",
                    "additionalProperties": {"type": "string"},
                },
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
                "secret_type": {
                    "type": "string",
                    "description": "Secret类型",
                    "enum": [
                        "Opaque",
                        "kubernetes.io/service-account-token",
                        "kubernetes.io/dockercfg",
                        "kubernetes.io/dockerconfigjson",
                        "kubernetes.io/basic-auth",
                        "kubernetes.io/ssh-auth",
                        "kubernetes.io/tls",
                    ],
                    "default": "Opaque",
                },
                "immutable": {
                    "type": "boolean",
                    "description": "是否不可变",
                    "default": False,
                },
            },
            "required": ["operation"],
        }

    def _create_api_clients(self) -> Dict[str, client.ApiClient]:
        """创建Kubernetes API客户端"""
        return {"v1": client.CoreV1Api()}

    async def _execute_internal(self, parameters: Dict[str, Any]) -> Any:
        """执行工具内部逻辑"""
        operation = parameters.get("operation")
        config_path = parameters.get("config_path")

        # 初始化API客户端
        clients = self._initialize_clients(config_path)
        v1 = clients["v1"]

        # 根据操作类型执行相应的方法
        operation_map = {
            "list_configmaps": lambda: self._list_configmaps(v1, parameters),
            "list_secrets": lambda: self._list_secrets(v1, parameters),
            "get_configmap": lambda: self._get_configmap(v1, parameters),
            "get_secret": lambda: self._get_secret(v1, parameters),
            "create_configmap": lambda: self._create_configmap(v1, parameters),
            "create_secret": lambda: self._create_secret(v1, parameters),
            "update_configmap": lambda: self._update_configmap(v1, parameters),
            "update_secret": lambda: self._update_secret(v1, parameters),
            "delete_configmap": lambda: self._delete_configmap(v1, parameters),
            "delete_secret": lambda: self._delete_secret(v1, parameters),
        }

        if operation in operation_map:
            return await operation_map[operation]()
        else:
            return {
                "success": False,
                "error": "不支持的操作",
                "message": f"未知的操作类型: {operation}",
            }

    async def _list_configmaps(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取ConfigMap列表"""
        try:
            namespace = parameters.get("namespace", "default")
            all_namespaces = parameters.get("all_namespaces", False)
            max_results = parameters.get("max_results", 50)

            loop = asyncio.get_event_loop()

            # 根据参数获取ConfigMap列表
            if all_namespaces:
                configmaps = await loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_config_map_for_all_namespaces(limit=max_results),
                )
            else:
                configmaps = await loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_namespaced_config_map(
                        namespace=namespace, limit=max_results
                    ),
                )

            # 格式化ConfigMap信息
            configmap_list = []
            for cm in configmaps.items:
                configmap_info = {
                    "name": cm.metadata.name,
                    "namespace": cm.metadata.namespace,
                    "age": self._calculate_age(cm.metadata.creation_timestamp),
                    "labels": cm.metadata.labels or {},
                    "data_keys": list(cm.data.keys()) if cm.data else [],
                    "binary_data_keys": (
                        list(cm.binary_data.keys()) if cm.binary_data else []
                    ),
                    "immutable": cm.immutable or False,
                    "data_count": len(cm.data or {}) + len(cm.binary_data or {}),
                }
                configmap_list.append(configmap_info)

            return {
                "success": True,
                "operation": "list_configmaps",
                "total_count": len(configmap_list),
                "configmaps": configmap_list,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except Exception as e:
            return {
                "success": False,
                "error": "获取ConfigMap列表失败",
                "message": str(e),
            }

    async def _list_secrets(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Secret列表"""
        try:
            namespace = parameters.get("namespace", "default")
            all_namespaces = parameters.get("all_namespaces", False)
            max_results = parameters.get("max_results", 50)

            loop = asyncio.get_event_loop()

            # 根据参数获取Secret列表
            if all_namespaces:
                secrets = await loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_secret_for_all_namespaces(limit=max_results),
                )
            else:
                secrets = await loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_namespaced_secret(
                        namespace=namespace, limit=max_results
                    ),
                )

            # 格式化Secret信息
            secret_list = []
            for secret in secrets.items:
                secret_info = {
                    "name": secret.metadata.name,
                    "namespace": secret.metadata.namespace,
                    "type": secret.type,
                    "age": self._calculate_age(secret.metadata.creation_timestamp),
                    "labels": secret.metadata.labels or {},
                    "data_keys": list(secret.data.keys()) if secret.data else [],
                    "string_data_keys": (
                        list(secret.string_data.keys()) if secret.string_data else []
                    ),
                    "immutable": secret.immutable or False,
                    "data_count": len(secret.data or {})
                    + len(secret.string_data or {}),
                }
                secret_list.append(secret_info)

            return {
                "success": True,
                "operation": "list_secrets",
                "total_count": len(secret_list),
                "secrets": secret_list,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except Exception as e:
            return {"success": False, "error": "获取Secret列表失败", "message": str(e)}

    async def _get_configmap(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取ConfigMap详细信息"""
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

            # 获取ConfigMap详细信息
            configmap = await loop.run_in_executor(
                self._executor,
                lambda: v1.read_namespaced_config_map(
                    name=resource_name, namespace=namespace
                ),
            )

            # 格式化详细信息
            configmap_details = {
                "name": configmap.metadata.name,
                "namespace": configmap.metadata.namespace,
                "uid": configmap.metadata.uid,
                "creation_timestamp": configmap.metadata.creation_timestamp.isoformat(),
                "age": self._calculate_age(configmap.metadata.creation_timestamp),
                "labels": configmap.metadata.labels or {},
                "annotations": configmap.metadata.annotations or {},
                "immutable": configmap.immutable or False,
                "data": configmap.data or {},
                "binary_data": {
                    key: f"<binary data: {len(base64.b64decode(value))} bytes>"
                    for key, value in (configmap.binary_data or {}).items()
                },
                "data_count": len(configmap.data or {})
                + len(configmap.binary_data or {}),
            }

            return {
                "success": True,
                "operation": "get_configmap",
                "configmap": configmap_details,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "ConfigMap不存在",
                    "message": f"在命名空间 {namespace} 中找不到ConfigMap {resource_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "获取ConfigMap失败",
                    "message": str(e),
                }
        except Exception as e:
            return {"success": False, "error": "获取ConfigMap失败", "message": str(e)}

    async def _get_secret(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Secret详细信息"""
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

            # 获取Secret详细信息
            secret = await loop.run_in_executor(
                self._executor,
                lambda: v1.read_namespaced_secret(
                    name=resource_name, namespace=namespace
                ),
            )

            # 格式化详细信息（注意：Secret数据已加密，不直接返回明文）
            secret_details = {
                "name": secret.metadata.name,
                "namespace": secret.metadata.namespace,
                "uid": secret.metadata.uid,
                "type": secret.type,
                "creation_timestamp": secret.metadata.creation_timestamp.isoformat(),
                "age": self._calculate_age(secret.metadata.creation_timestamp),
                "labels": secret.metadata.labels or {},
                "annotations": secret.metadata.annotations or {},
                "immutable": secret.immutable or False,
                "data_keys": list(secret.data.keys()) if secret.data else [],
                "string_data_keys": (
                    list(secret.string_data.keys()) if secret.string_data else []
                ),
                "data_sizes": {
                    key: f"{len(base64.b64decode(value))} bytes"
                    for key, value in (secret.data or {}).items()
                },
                "data_count": len(secret.data or {}) + len(secret.string_data or {}),
            }

            return {
                "success": True,
                "operation": "get_secret",
                "secret": secret_details,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Secret不存在",
                    "message": f"在命名空间 {namespace} 中找不到Secret {resource_name}",
                }
            else:
                return {"success": False, "error": "获取Secret失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "获取Secret失败", "message": str(e)}

    async def _create_configmap(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """创建ConfigMap"""
        try:
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")
            data = parameters.get("data", {})
            labels = parameters.get("labels", {})
            annotations = parameters.get("annotations", {})
            immutable = parameters.get("immutable", False)

            if not resource_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_name是必需的参数",
                }

            # 构建ConfigMap对象
            configmap_body = client.V1ConfigMap(
                api_version="v1",
                kind="ConfigMap",
                metadata=client.V1ObjectMeta(
                    name=resource_name,
                    namespace=namespace,
                    labels=labels,
                    annotations=annotations,
                ),
                data=data,
                immutable=immutable,
            )

            loop = asyncio.get_event_loop()

            # 创建ConfigMap
            created_configmap = await loop.run_in_executor(
                self._executor,
                lambda: v1.create_namespaced_config_map(
                    namespace=namespace, body=configmap_body
                ),
            )

            return {
                "success": True,
                "operation": "create_configmap",
                "message": f"ConfigMap {resource_name} 在命名空间 {namespace} 中创建成功",
                "configmap_name": created_configmap.metadata.name,
                "namespace": created_configmap.metadata.namespace,
                "data_count": len(data),
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 409:
                return {
                    "success": False,
                    "error": "ConfigMap已存在",
                    "message": f"命名空间 {namespace} 中已存在同名ConfigMap",
                }
            else:
                return {
                    "success": False,
                    "error": "创建ConfigMap失败",
                    "message": str(e),
                }
        except Exception as e:
            return {"success": False, "error": "创建ConfigMap失败", "message": str(e)}

    async def _create_secret(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """创建Secret"""
        try:
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")
            data = parameters.get("data", {})
            labels = parameters.get("labels", {})
            annotations = parameters.get("annotations", {})
            secret_type = parameters.get("secret_type", "Opaque")
            immutable = parameters.get("immutable", False)

            if not resource_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_name是必需的参数",
                }

            # 编码Secret数据
            encoded_data = {}
            for key, value in data.items():
                if isinstance(value, str):
                    encoded_data[key] = base64.b64encode(value.encode()).decode()
                else:
                    encoded_data[key] = base64.b64encode(str(value).encode()).decode()

            # 构建Secret对象
            secret_body = client.V1Secret(
                api_version="v1",
                kind="Secret",
                metadata=client.V1ObjectMeta(
                    name=resource_name,
                    namespace=namespace,
                    labels=labels,
                    annotations=annotations,
                ),
                data=encoded_data,
                type=secret_type,
                immutable=immutable,
            )

            loop = asyncio.get_event_loop()

            # 创建Secret
            created_secret = await loop.run_in_executor(
                self._executor,
                lambda: v1.create_namespaced_secret(
                    namespace=namespace, body=secret_body
                ),
            )

            return {
                "success": True,
                "operation": "create_secret",
                "message": f"Secret {resource_name} 在命名空间 {namespace} 中创建成功",
                "secret_name": created_secret.metadata.name,
                "namespace": created_secret.metadata.namespace,
                "type": secret_type,
                "data_count": len(data),
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 409:
                return {
                    "success": False,
                    "error": "Secret已存在",
                    "message": f"命名空间 {namespace} 中已存在同名Secret",
                }
            else:
                return {"success": False, "error": "创建Secret失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "创建Secret失败", "message": str(e)}

    async def _update_configmap(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """更新ConfigMap"""
        try:
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")
            data = parameters.get("data", {})

            if not resource_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_name是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取现有ConfigMap
            existing_configmap = await loop.run_in_executor(
                self._executor,
                lambda: v1.read_namespaced_config_map(
                    name=resource_name, namespace=namespace
                ),
            )

            # 更新数据
            if existing_configmap.data:
                existing_configmap.data.update(data)
            else:
                existing_configmap.data = data

            # 应用更新
            await loop.run_in_executor(
                self._executor,
                lambda: v1.patch_namespaced_config_map(
                    name=resource_name, namespace=namespace, body=existing_configmap
                ),
            )

            return {
                "success": True,
                "operation": "update_configmap",
                "message": f"ConfigMap {resource_name} 在命名空间 {namespace} 中更新成功",
                "configmap_name": resource_name,
                "namespace": namespace,
                "updated_keys": list(data.keys()),
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "ConfigMap不存在",
                    "message": f"在命名空间 {namespace} 中找不到ConfigMap {resource_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "更新ConfigMap失败",
                    "message": str(e),
                }
        except Exception as e:
            return {"success": False, "error": "更新ConfigMap失败", "message": str(e)}

    async def _update_secret(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """更新Secret"""
        try:
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")
            data = parameters.get("data", {})

            if not resource_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_name是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取现有Secret
            existing_secret = await loop.run_in_executor(
                self._executor,
                lambda: v1.read_namespaced_secret(
                    name=resource_name, namespace=namespace
                ),
            )

            # 编码新数据
            encoded_data = {}
            for key, value in data.items():
                if isinstance(value, str):
                    encoded_data[key] = base64.b64encode(value.encode()).decode()
                else:
                    encoded_data[key] = base64.b64encode(str(value).encode()).decode()

            # 更新数据
            if existing_secret.data:
                existing_secret.data.update(encoded_data)
            else:
                existing_secret.data = encoded_data

            # 应用更新
            await loop.run_in_executor(
                self._executor,
                lambda: v1.patch_namespaced_secret(
                    name=resource_name, namespace=namespace, body=existing_secret
                ),
            )

            return {
                "success": True,
                "operation": "update_secret",
                "message": f"Secret {resource_name} 在命名空间 {namespace} 中更新成功",
                "secret_name": resource_name,
                "namespace": namespace,
                "updated_keys": list(data.keys()),
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Secret不存在",
                    "message": f"在命名空间 {namespace} 中找不到Secret {resource_name}",
                }
            else:
                return {"success": False, "error": "更新Secret失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "更新Secret失败", "message": str(e)}

    async def _delete_configmap(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """删除ConfigMap"""
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

            # 删除ConfigMap
            await loop.run_in_executor(
                self._executor,
                lambda: v1.delete_namespaced_config_map(
                    name=resource_name, namespace=namespace
                ),
            )

            return {
                "success": True,
                "operation": "delete_configmap",
                "message": f"ConfigMap {resource_name} 在命名空间 {namespace} 中已成功删除",
                "configmap_name": resource_name,
                "namespace": namespace,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "ConfigMap不存在",
                    "message": f"在命名空间 {namespace} 中找不到ConfigMap {resource_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "删除ConfigMap失败",
                    "message": str(e),
                }
        except Exception as e:
            return {"success": False, "error": "删除ConfigMap失败", "message": str(e)}

    async def _delete_secret(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """删除Secret"""
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

            # 删除Secret
            await loop.run_in_executor(
                self._executor,
                lambda: v1.delete_namespaced_secret(
                    name=resource_name, namespace=namespace
                ),
            )

            return {
                "success": True,
                "operation": "delete_secret",
                "message": f"Secret {resource_name} 在命名空间 {namespace} 中已成功删除",
                "secret_name": resource_name,
                "namespace": namespace,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Secret不存在",
                    "message": f"在命名空间 {namespace} 中找不到Secret {resource_name}",
                }
            else:
                return {"success": False, "error": "删除Secret失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "删除Secret失败", "message": str(e)}
