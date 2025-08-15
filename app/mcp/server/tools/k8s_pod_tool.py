#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: MCP 工具（k8s_pod_tool）
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from kubernetes import client
from kubernetes.client.rest import ApiException

from app.utils.time_utils import iso_utc_now

from .k8s_base_tool import K8sBaseTool

UTC_TZ = timezone.utc


class K8sPodTool(K8sBaseTool):
    """k8s Pod管理工具"""

    def __init__(self):
        super().__init__(
            name="k8s_pod_management",
            description="k8s Pod管理工具，支持查看Pod列表、获取Pod详情、删除Pod、重启Pod等操作",
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
                        "list_pods",
                        "get_pod_details",
                        "delete_pod",
                        "restart_pod",
                        "get_pod_events",
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
                "pod_name": {
                    "type": "string",
                    "description": "Pod名称（部分操作需要）",
                },
                "label_selector": {
                    "type": "string",
                    "description": "标签选择器，格式如：app=nginx,version=v1",
                },
                "field_selector": {
                    "type": "string",
                    "description": "字段选择器，格式如：status.phase=Running",
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
                "time_window_hours": {
                    "type": "integer",
                    "description": "事件查询时间窗口（小时），仅适用于get_pod_events",
                    "minimum": 1,
                    "maximum": 24,
                    "default": 2,
                },
            },
            "required": ["operation"],
        }

    def _create_api_clients(self) -> Dict[str, client.ApiClient]:
        """创建Pod工具需要的API客户端"""
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
        if operation == "list_pods":
            return await self._list_pods(v1, parameters)
        elif operation == "get_pod_details":
            return await self._get_pod_details(v1, parameters)
        elif operation == "delete_pod":
            return await self._delete_pod(v1, parameters)
        elif operation == "restart_pod":
            return await self._restart_pod(v1, apps_v1, parameters)
        elif operation == "get_pod_events":
            return await self._get_pod_events(v1, parameters)
        else:
            return {
                "success": False,
                "error": "不支持的操作",
                "message": f"未知的操作类型: {operation}",
            }

    async def _list_pods(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Pod列表"""
        try:
            namespace = parameters.get("namespace", "default")
            all_namespaces = parameters.get("all_namespaces", False)
            label_selector = parameters.get("label_selector")
            field_selector = parameters.get("field_selector")
            max_results = parameters.get("max_results", 50)

            loop = asyncio.get_event_loop()

            # 根据参数获取Pod列表
            if all_namespaces:
                pods = await loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_pod_for_all_namespaces(
                        label_selector=label_selector,
                        field_selector=field_selector,
                        limit=max_results,
                    ),
                )
            else:
                pods = await loop.run_in_executor(
                    self._executor,
                    lambda: v1.list_namespaced_pod(
                        namespace=namespace,
                        label_selector=label_selector,
                        field_selector=field_selector,
                        limit=max_results,
                    ),
                )

            # 格式化Pod信息
            pod_list = []
            for pod in pods.items:
                pod_info = {
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "status": pod.status.phase,
                    "ready": self._get_pod_ready_status(pod),
                    "restart_count": sum(
                        cs.restart_count or 0
                        for cs in pod.status.container_statuses or []
                    ),
                    "age": self._calculate_age(pod.metadata.creation_timestamp),
                    "node": pod.spec.node_name or "未分配",
                    "ip": pod.status.pod_ip or "无",
                    "labels": pod.metadata.labels or {},
                    "containers": [cs.name for cs in pod.spec.containers or []],
                }
                pod_list.append(pod_info)

            return {
                "success": True,
                "operation": "list_pods",
                "total_count": len(pod_list),
                "pods": pod_list,
                "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {
                "success": False,
                "error": "获取Pod列表失败",
                "message": str(e),
                "timestamp": datetime.now(UTC_TZ).isoformat(),
            }

    async def _get_pod_details(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Pod详细信息"""
        try:
            pod_name = parameters.get("pod_name")
            namespace = parameters.get("namespace", "default")

            if not pod_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "Pod名称是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 获取Pod详细信息
            pod = await loop.run_in_executor(
                self._executor,
                lambda: v1.read_namespaced_pod(name=pod_name, namespace=namespace),
            )

            # 格式化详细信息
            pod_details = {
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "uid": pod.metadata.uid,
                "creation_timestamp": pod.metadata.creation_timestamp.isoformat(),
                "age": self._calculate_age(pod.metadata.creation_timestamp),
                "labels": pod.metadata.labels or {},
                "annotations": pod.metadata.annotations or {},
                "status": {
                    "phase": pod.status.phase,
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
                        for cond in pod.status.conditions or []
                    ],
                    "pod_ip": pod.status.pod_ip,
                    "host_ip": pod.status.host_ip,
                    "start_time": (
                        pod.status.start_time.isoformat()
                        if pod.status.start_time
                        else None
                    ),
                    "qos_class": pod.status.qos_class,
                },
                "spec": {
                    "node_name": pod.spec.node_name,
                    "service_account": pod.spec.service_account_name,
                    "restart_policy": pod.spec.restart_policy,
                    "dns_policy": pod.spec.dns_policy,
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
                            "resources": (
                                {
                                    "requests": container.resources.requests or {},
                                    "limits": container.resources.limits or {},
                                }
                                if container.resources
                                else {}
                            ),
                        }
                        for container in pod.spec.containers or []
                    ],
                },
                "container_statuses": [
                    {
                        "name": cs.name,
                        "ready": cs.ready,
                        "restart_count": cs.restart_count or 0,
                        "image": cs.image,
                        "image_id": cs.image_id,
                        "state": self._format_container_state(cs.state),
                    }
                    for cs in pod.status.container_statuses or []
                ],
            }

            return {
                "success": True,
                "operation": "get_pod_details",
                "pod_details": pod_details,
                "timestamp": iso_utc_now(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Pod不存在",
                    "message": f"在命名空间 {namespace} 中找不到Pod {pod_name}",
                }
            else:
                return {"success": False, "error": "获取Pod详情失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "获取Pod详情失败", "message": str(e)}

    async def _delete_pod(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """删除Pod"""
        try:
            pod_name = parameters.get("pod_name")
            namespace = parameters.get("namespace", "default")

            if not pod_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "Pod名称是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 删除Pod
            await loop.run_in_executor(
                self._executor,
                lambda: v1.delete_namespaced_pod(
                    name=pod_name,
                    namespace=namespace,
                    body=client.V1DeleteOptions(grace_period_seconds=30),
                ),
            )

            return {
                "success": True,
                "operation": "delete_pod",
                "message": f"Pod {pod_name} 在命名空间 {namespace} 中已成功删除",
                "pod_name": pod_name,
                "namespace": namespace,
                "timestamp": iso_utc_now(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Pod不存在",
                    "message": f"在命名空间 {namespace} 中找不到Pod {pod_name}",
                }
            else:
                return {"success": False, "error": "删除Pod失败", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "删除Pod失败", "message": str(e)}

    async def _restart_pod(
        self,
        v1: client.CoreV1Api,
        apps_v1: client.AppsV1Api,
        parameters: Dict[str, Any],
    ) -> Dict[str, Any]:
        """重启Pod（通过删除Pod让Deployment/ReplicaSet重新创建）"""
        try:
            pod_name = parameters.get("pod_name")
            namespace = parameters.get("namespace", "default")

            if not pod_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "Pod名称是必需的参数",
                }

            loop = asyncio.get_event_loop()

            # 首先获取Pod信息以检查所属的控制器
            try:
                pod = await loop.run_in_executor(
                    self._executor,
                    lambda: v1.read_namespaced_pod(name=pod_name, namespace=namespace),
                )
            except ApiException as e:
                if e.status == 404:
                    return {
                        "success": False,
                        "error": "Pod不存在",
                        "message": f"在命名空间 {namespace} 中找不到Pod {pod_name}",
                    }
                raise

            # 检查Pod的所有者引用
            owner_refs = pod.metadata.owner_references or []
            has_controller = any(ref.controller for ref in owner_refs)

            if not has_controller:
                return {
                    "success": False,
                    "error": "无法重启Pod",
                    "message": f"Pod {pod_name} 不受任何控制器管理，无法自动重启。请手动删除Pod。",
                }

            # 删除Pod让控制器重新创建
            await loop.run_in_executor(
                self._executor,
                lambda: v1.delete_namespaced_pod(
                    name=pod_name,
                    namespace=namespace,
                    body=client.V1DeleteOptions(grace_period_seconds=0),
                ),
            )

            return {
                "success": True,
                "operation": "restart_pod",
                "message": f"Pod {pod_name} 已删除，控制器将自动重新创建",
                "pod_name": pod_name,
                "namespace": namespace,
                "controller_info": [
                    {"kind": ref.kind, "name": ref.name, "controller": ref.controller}
                    for ref in owner_refs
                ],
                "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "重启Pod失败", "message": str(e)}

    async def _get_pod_events(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Pod相关事件"""
        try:
            pod_name = parameters.get("pod_name")
            namespace = parameters.get("namespace", "default")
            time_window_hours = parameters.get("time_window_hours", 2)

            if not pod_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "Pod名称是必需的参数",
                }

            loop = asyncio.get_event_loop()
            since_time = datetime.now(UTC_TZ) - timedelta(hours=time_window_hours)

            # 获取命名空间内的所有事件
            events = await loop.run_in_executor(
                self._executor,
                lambda: v1.list_namespaced_event(
                    namespace=namespace,
                    field_selector=f"involvedObject.name={pod_name}",
                    limit=50,
                ),
            )

            # 过滤和格式化事件
            pod_events = []
            for event in events.items:
                event_time = event.last_timestamp or event.first_timestamp
                if event_time and event_time >= since_time.replace(
                    tzinfo=event_time.tzinfo
                ):
                    pod_events.append(
                        {
                            "type": event.type,
                            "reason": event.reason,
                            "message": event.message,
                            "source": (
                                event.source.component if event.source else "Unknown"
                            ),
                            "count": event.count or 1,
                            "first_timestamp": (
                                event.first_timestamp.isoformat()
                                if event.first_timestamp
                                else None
                            ),
                            "last_timestamp": (
                                event.last_timestamp.isoformat()
                                if event.last_timestamp
                                else None
                            ),
                            "involved_object": {
                                "kind": event.involved_object.kind,
                                "name": event.involved_object.name,
                                "namespace": event.involved_object.namespace,
                            },
                        }
                    )

            # 按时间排序
            pod_events.sort(
                key=lambda x: x["last_timestamp"] or x["first_timestamp"], reverse=True
            )

            return {
                "success": True,
                "operation": "get_pod_events",
                "pod_name": pod_name,
                "namespace": namespace,
                "time_window_hours": time_window_hours,
                "total_events": len(pod_events),
                "events": pod_events,
                "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "获取Pod事件失败", "message": str(e)}

    def _get_pod_ready_status(self, pod) -> str:
        """获取Pod就绪状态"""
        if not pod.status.container_statuses:
            return "0/0"

        ready_count = sum(1 for cs in pod.status.container_statuses if cs.ready)
        total_count = len(pod.status.container_statuses)
        return f"{ready_count}/{total_count}"

    def _format_container_state(self, state) -> Dict[str, Any]:
        """格式化容器状态"""
        if not state:
            return {"state": "unknown"}

        if state.running:
            return {
                "state": "running",
                "started_at": (
                    state.running.started_at.isoformat()
                    if state.running.started_at
                    else None
                ),
            }
        elif state.waiting:
            return {
                "state": "waiting",
                "reason": state.waiting.reason,
                "message": state.waiting.message,
            }
        elif state.terminated:
            return {
                "state": "terminated",
                "reason": state.terminated.reason,
                "message": state.terminated.message,
                "exit_code": state.terminated.exit_code,
                "started_at": (
                    state.terminated.started_at.isoformat()
                    if state.terminated.started_at
                    else None
                ),
                "finished_at": (
                    state.terminated.finished_at.isoformat()
                    if state.terminated.finished_at
                    else None
                ),
            }
        else:
            return {"state": "unknown"}
