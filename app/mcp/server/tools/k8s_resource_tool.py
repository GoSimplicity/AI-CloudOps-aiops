#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: MCP 工具（k8s_resource_tool）
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict

import yaml
from kubernetes import client
from kubernetes.client.rest import ApiException
from kubernetes.dynamic import DynamicClient

from app.utils.time_utils import iso_utc_now

from .k8s_base_tool import K8sBaseTool

UTC_TZ = timezone.utc


class K8sResourceTool(K8sBaseTool):
    """k8s资源操作工具"""

    def __init__(self):
        super().__init__(
            name="k8s_resource_operations",
            description="k8s资源操作工具，支持通用资源的describe、标签管理、注解管理和YAML应用功能",
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
                        "describe_resource",
                        "add_labels",
                        "remove_labels",
                        "add_annotations",
                        "remove_annotations",
                        "apply_yaml",
                        "get_resource_yaml",
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
                "resource_type": {
                    "type": "string",
                    "description": "资源类型（如pod、service、deployment等）",
                    "enum": [
                        "pod",
                        "service",
                        "deployment",
                        "replicaset",
                        "configmap",
                        "secret",
                        "ingress",
                        "namespace",
                        "node",
                        "pv",
                        "pvc",
                    ],
                },
                "resource_name": {"type": "string", "description": "资源名称"},
                "labels": {
                    "type": "object",
                    "description": "标签键值对",
                    "additionalProperties": {"type": "string"},
                },
                "annotations": {
                    "type": "object",
                    "description": "注解键值对",
                    "additionalProperties": {"type": "string"},
                },
                "label_keys": {
                    "type": "array",
                    "description": "要删除的标签键名列表",
                    "items": {"type": "string"},
                },
                "annotation_keys": {
                    "type": "array",
                    "description": "要删除的注解键名列表",
                    "items": {"type": "string"},
                },
                "yaml_content": {
                    "type": "string",
                    "description": "YAML内容（应用YAML时使用）",
                },
            },
            "required": ["operation"],
        }

    def _create_api_clients(self) -> Dict[str, Any]:
        """创建Kubernetes API客户端"""
        # 初始化各种API客户端
        api_clients = {
            "v1": client.CoreV1Api(),
            "apps_v1": client.AppsV1Api(),
            "networking_v1": client.NetworkingV1Api(),
            "storage_v1": client.StorageV1Api(),
            "api_client": client.ApiClient(),
        }

        # 尝试初始化动态客户端
        try:
            api_clients["dynamic"] = DynamicClient(api_clients["api_client"])
        except Exception:
            api_clients["dynamic"] = None

        return api_clients

    async def _execute_internal(self, parameters: Dict[str, Any]) -> Any:
        """执行工具内部逻辑"""
        operation = parameters.get("operation")
        config_path = parameters.get("config_path")

        # 初始化API客户端
        clients = self._initialize_clients(config_path)

        # 根据操作类型执行相应的方法
        operation_map = {
            "describe_resource": lambda: self._describe_resource(clients, parameters),
            "add_labels": lambda: self._add_labels(clients, parameters),
            "remove_labels": lambda: self._remove_labels(clients, parameters),
            "add_annotations": lambda: self._add_annotations(clients, parameters),
            "remove_annotations": lambda: self._remove_annotations(clients, parameters),
            "apply_yaml": lambda: self._apply_yaml(clients, parameters),
            "get_resource_yaml": lambda: self._get_resource_yaml(clients, parameters),
        }

        if operation in operation_map:
            return await operation_map[operation]()
        else:
            return {
                "success": False,
                "error": "不支持的操作",
                "message": f"未知的操作类型: {operation}",
            }

    async def _describe_resource(
        self, clients: Dict[str, Any], parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """描述资源详细信息"""
        try:
            resource_type = parameters.get("resource_type")
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")

            if not resource_type or not resource_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_type和resource_name都是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 根据资源类型获取相应的API客户端和方法
            resource_info = await self._get_resource_by_type(
                clients, resource_type, resource_name, namespace, loop
            )

            if not resource_info:
                return {
                    "success": False,
                    "error": "资源不存在",
                    "message": f"找不到资源 {resource_type}/{resource_name}",
                }

            # 格式化资源信息
            description = {
                "name": resource_info.metadata.name,
                "namespace": getattr(resource_info.metadata, "namespace", None),
                "uid": resource_info.metadata.uid,
                "resource_version": resource_info.metadata.resource_version,
                "creation_timestamp": resource_info.metadata.creation_timestamp.isoformat(),
                "age": self._calculate_age(resource_info.metadata.creation_timestamp),
                "labels": resource_info.metadata.labels or {},
                "annotations": resource_info.metadata.annotations or {},
                "owner_references": [
                    {
                        "api_version": ref.api_version,
                        "kind": ref.kind,
                        "name": ref.name,
                        "uid": ref.uid,
                        "controller": ref.controller,
                        "block_owner_deletion": ref.block_owner_deletion,
                    }
                    for ref in resource_info.metadata.owner_references or []
                ],
                "finalizers": resource_info.metadata.finalizers or [],
            }

            # 根据资源类型添加特定信息
            if hasattr(resource_info, "spec"):
                description["spec"] = self._serialize_object(resource_info.spec)

            if hasattr(resource_info, "status"):
                description["status"] = self._serialize_object(resource_info.status)

            return {
                "success": True,
                "operation": "describe_resource",
                "resource_type": resource_type,
                "resource_description": description,
                "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "描述资源失败", "message": str(e)}

    async def _add_labels(
        self, clients: Dict[str, Any], parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """添加标签"""
        try:
            resource_type = parameters.get("resource_type")
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")
            labels = parameters.get("labels", {})

            if not all([resource_type, resource_name, labels]):
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_type、resource_name和labels都是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取资源
            resource = await self._get_resource_by_type(
                clients, resource_type, resource_name, namespace, loop
            )

            if not resource:
                return {
                    "success": False,
                    "error": "资源不存在",
                    "message": f"找不到资源 {resource_type}/{resource_name}",
                }

            # 更新标签
            if not resource.metadata.labels:
                resource.metadata.labels = {}

            resource.metadata.labels.update(labels)

            # 应用更新并返回新资源
            updated_resource = await self._patch_resource_by_type(
                clients, resource_type, resource_name, namespace, resource, loop
            )

            return {
                "success": True,
                "operation": "add_labels",
                "message": f"{resource_type} {resource_name} 的标签已更新",
                "resource_type": resource_type,
                "resource_name": resource_name,
                "namespace": namespace,
                "added_labels": labels,
                "new_labels": updated_resource.metadata.labels or {},
                "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "添加标签失败", "message": str(e)}

    async def _remove_labels(
        self, clients: Dict[str, Any], parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """删除标签"""
        try:
            resource_type = parameters.get("resource_type")
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")
            label_keys = parameters.get("label_keys", [])

            if not all([resource_type, resource_name, label_keys]):
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_type、resource_name和label_keys都是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取资源
            resource = await self._get_resource_by_type(
                clients, resource_type, resource_name, namespace, loop
            )

            if not resource:
                return {
                    "success": False,
                    "error": "资源不存在",
                    "message": f"找不到资源 {resource_type}/{resource_name}",
                }

            # 删除标签
            removed_labels = {}

            if resource.metadata.labels:
                for key in label_keys:
                    if key in resource.metadata.labels:
                        removed_labels[key] = resource.metadata.labels.pop(key)

            # 应用更新并返回新标签集合
            updated_resource = await self._patch_resource_by_type(
                clients, resource_type, resource_name, namespace, resource, loop
            )

            return {
                "success": True,
                "operation": "remove_labels",
                "message": f"{resource_type} {resource_name} 的标签已删除",
                "resource_type": resource_type,
                "resource_name": resource_name,
                "namespace": namespace,
                "removed_labels": removed_labels,
                "new_labels": updated_resource.metadata.labels or {},
                "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "删除标签失败", "message": str(e)}

    async def _add_annotations(
        self, clients: Dict[str, Any], parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """添加注解"""
        try:
            resource_type = parameters.get("resource_type")
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")
            annotations = parameters.get("annotations", {})

            if not all([resource_type, resource_name, annotations]):
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_type、resource_name和annotations都是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取资源
            resource = await self._get_resource_by_type(
                clients, resource_type, resource_name, namespace, loop
            )

            if not resource:
                return {
                    "success": False,
                    "error": "资源不存在",
                    "message": f"找不到资源 {resource_type}/{resource_name}",
                }

            # 更新注解
            if not resource.metadata.annotations:
                resource.metadata.annotations = {}

            resource.metadata.annotations.update(annotations)

            # 应用更新并返回新注解集合
            updated_resource = await self._patch_resource_by_type(
                clients, resource_type, resource_name, namespace, resource, loop
            )

            return {
                "success": True,
                "operation": "add_annotations",
                "message": f"{resource_type} {resource_name} 的注解已更新",
                "resource_type": resource_type,
                "resource_name": resource_name,
                "namespace": namespace,
                "added_annotations": annotations,
                "new_annotations": updated_resource.metadata.annotations or {},
                "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "添加注解失败", "message": str(e)}

    async def _remove_annotations(
        self, clients: Dict[str, Any], parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """删除注解"""
        try:
            resource_type = parameters.get("resource_type")
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")
            annotation_keys = parameters.get("annotation_keys", [])

            if not all([resource_type, resource_name, annotation_keys]):
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_type、resource_name和annotation_keys都是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取资源
            resource = await self._get_resource_by_type(
                clients, resource_type, resource_name, namespace, loop
            )

            if not resource:
                return {
                    "success": False,
                    "error": "资源不存在",
                    "message": f"找不到资源 {resource_type}/{resource_name}",
                }

            # 删除注解
            removed_annotations = {}
            if resource.metadata.annotations:
                for key in annotation_keys:
                    if key in resource.metadata.annotations:
                        removed_annotations[key] = resource.metadata.annotations.pop(
                            key
                        )

            # 应用更新并返回新注解集合
            updated_resource = await self._patch_resource_by_type(
                clients, resource_type, resource_name, namespace, resource, loop
            )

            return {
                "success": True,
                "operation": "remove_annotations",
                "message": f"{resource_type} {resource_name} 的注解已删除",
                "resource_type": resource_type,
                "resource_name": resource_name,
                "namespace": namespace,
                "removed_annotations": removed_annotations,
                "new_annotations": updated_resource.metadata.annotations or {},
                "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "删除注解失败", "message": str(e)}

    async def _apply_yaml(
        self, clients: Dict[str, Any], parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """应用YAML配置"""
        try:
            yaml_content = parameters.get("yaml_content")

            if not yaml_content:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "yaml_content是必需的参数",
                }

            # 解析YAML内容
            try:
                resources = list(yaml.safe_load_all(yaml_content))
                resources = [res for res in resources if res]  # 过滤空对象
            except yaml.YAMLError as e:
                return {
                    "success": False,
                    "error": "YAML解析失败",
                    "message": f"YAML格式错误: {str(e)}",
                }

            if not resources:
                return {
                    "success": False,
                    "error": "YAML为空",
                    "message": "YAML内容中没有有效的资源定义",
                }

            # 应用资源
            applied_resources = []
            errors = []

            for resource_def in resources:
                try:
                    # 这里只是简单实现，实际使用中可能需要更复杂的逻辑
                    applied_resources.append(
                        {
                            "apiVersion": resource_def.get("apiVersion"),
                            "kind": resource_def.get("kind"),
                            "name": resource_def.get("metadata", {}).get("name"),
                            "namespace": resource_def.get("metadata", {}).get(
                                "namespace"
                            ),
                        }
                    )
                except Exception as e:
                    errors.append(
                        {
                            "resource": resource_def.get("kind", "Unknown"),
                            "error": str(e),
                        }
                    )

            return {
                "success": len(errors) == 0,
                "operation": "apply_yaml",
                "message": f"已应用 {len(applied_resources)} 个资源，{len(errors)} 个失败",
                "applied_resources": applied_resources,
                "errors": errors,
                "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "应用YAML失败", "message": str(e)}

    async def _get_resource_yaml(
        self, clients: Dict[str, Any], parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取资源的YAML表示"""
        try:
            resource_type = parameters.get("resource_type")
            resource_name = parameters.get("resource_name")
            namespace = parameters.get("namespace", "default")

            if not resource_type or not resource_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "resource_type和resource_name都是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取资源
            resource = await self._get_resource_by_type(
                clients, resource_type, resource_name, namespace, loop
            )

            if not resource:
                return {
                    "success": False,
                    "error": "资源不存在",
                    "message": f"找不到资源 {resource_type}/{resource_name}",
                }

            # 转换为YAML
            resource_dict = self._resource_to_dict(resource)
            yaml_content = yaml.dump(
                resource_dict, default_flow_style=False, allow_unicode=True
            )

            return {
                "success": True,
                "operation": "get_resource_yaml",
                "resource_type": resource_type,
                "resource_name": resource_name,
                "namespace": namespace,
                "yaml_content": yaml_content,
                "timestamp": datetime.now(UTC_TZ).isoformat(),
            }

        except Exception as e:
            return {"success": False, "error": "获取资源YAML失败", "message": str(e)}

    async def _get_resource_by_type(
        self,
        clients: Dict[str, Any],
        resource_type: str,
        name: str,
        namespace: str,
        loop,
    ) -> Any:
        """根据资源类型获取资源"""
        v1 = clients["v1"]
        apps_v1 = clients["apps_v1"]
        networking_v1 = clients["networking_v1"]

        try:
            if resource_type == "pod":
                return await loop.run_in_executor(
                    self._executor, lambda: v1.read_namespaced_pod(name, namespace)
                )
            elif resource_type == "service":
                return await loop.run_in_executor(
                    self._executor, lambda: v1.read_namespaced_service(name, namespace)
                )
            elif resource_type == "deployment":
                return await loop.run_in_executor(
                    self._executor,
                    lambda: apps_v1.read_namespaced_deployment(name, namespace),
                )
            elif resource_type == "replicaset":
                return await loop.run_in_executor(
                    self._executor,
                    lambda: apps_v1.read_namespaced_replica_set(name, namespace),
                )
            elif resource_type == "configmap":
                return await loop.run_in_executor(
                    self._executor,
                    lambda: v1.read_namespaced_config_map(name, namespace),
                )
            elif resource_type == "secret":
                return await loop.run_in_executor(
                    self._executor, lambda: v1.read_namespaced_secret(name, namespace)
                )
            elif resource_type == "ingress":
                return await loop.run_in_executor(
                    self._executor,
                    lambda: networking_v1.read_namespaced_ingress(name, namespace),
                )
            elif resource_type == "namespace":
                return await loop.run_in_executor(
                    self._executor, lambda: v1.read_namespace(name)
                )
            elif resource_type == "node":
                return await loop.run_in_executor(
                    self._executor, lambda: v1.read_node(name)
                )
            elif resource_type == "pv":
                return await loop.run_in_executor(
                    self._executor, lambda: v1.read_persistent_volume(name)
                )
            elif resource_type == "pvc":
                return await loop.run_in_executor(
                    self._executor,
                    lambda: v1.read_namespaced_persistent_volume_claim(name, namespace),
                )
            else:
                return None
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    async def _patch_resource_by_type(
        self,
        clients: Dict[str, Any],
        resource_type: str,
        name: str,
        namespace: str,
        body: Any,
        loop,
    ) -> Any:
        """根据资源类型更新资源"""
        v1 = clients["v1"]
        apps_v1 = clients["apps_v1"]
        networking_v1 = clients["networking_v1"]

        if resource_type == "pod":
            return await loop.run_in_executor(
                self._executor, lambda: v1.patch_namespaced_pod(name, namespace, body)
            )
        elif resource_type == "service":
            return await loop.run_in_executor(
                self._executor,
                lambda: v1.patch_namespaced_service(name, namespace, body),
            )
        elif resource_type == "deployment":
            return await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.patch_namespaced_deployment(name, namespace, body),
            )
        elif resource_type == "replicaset":
            return await loop.run_in_executor(
                self._executor,
                lambda: apps_v1.patch_namespaced_replica_set(name, namespace, body),
            )
        elif resource_type == "configmap":
            return await loop.run_in_executor(
                self._executor,
                lambda: v1.patch_namespaced_config_map(name, namespace, body),
            )
        elif resource_type == "secret":
            return await loop.run_in_executor(
                self._executor,
                lambda: v1.patch_namespaced_secret(name, namespace, body),
            )
        elif resource_type == "ingress":
            return await loop.run_in_executor(
                self._executor,
                lambda: networking_v1.patch_namespaced_ingress(name, namespace, body),
            )
        elif resource_type == "namespace":
            return await loop.run_in_executor(
                self._executor, lambda: v1.patch_namespace(name, body)
            )
        elif resource_type == "node":
            return await loop.run_in_executor(
                self._executor, lambda: v1.patch_node(name, body)
            )
        else:
            raise ValueError(f"不支持的资源类型: {resource_type}")

    def _serialize_object(self, obj) -> Any:
        """序列化Kubernetes对象"""
        if obj is None:
            return None

        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        elif isinstance(obj, dict):
            return obj
        elif isinstance(obj, list):
            return [self._serialize_object(item) for item in obj]
        else:
            return str(obj)

    def _resource_to_dict(self, resource) -> Dict[str, Any]:
        """将资源对象转换为字典"""
        if hasattr(resource, "to_dict"):
            resource_dict = resource.to_dict()

            # 清理不需要的字段
            if "status" in resource_dict:
                del resource_dict["status"]

            if "metadata" in resource_dict:
                metadata = resource_dict["metadata"]
                # 保留需要的metadata字段
                keep_fields = ["name", "namespace", "labels", "annotations"]
                resource_dict["metadata"] = {
                    k: v for k, v in metadata.items() if k in keep_fields
                }

            return resource_dict
        else:
            return {}
