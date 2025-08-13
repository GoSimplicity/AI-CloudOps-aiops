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
from typing import Any, Dict, Optional

from kubernetes import client
from kubernetes.client.rest import ApiException

from app.utils.time_utils import iso_utc_now

from .k8s_base_tool import K8sBaseTool

UTC_TZ = timezone.utc


class K8sLogsTool(K8sBaseTool):
    """k8s日志查看工具"""

    def __init__(self):
        super().__init__(
            name="k8s_logs_viewer",
            description="k8s日志查看工具，支持查看Pod和容器日志、历史日志、实时日志等功能",
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
                        "get_pod_logs",
                        "get_container_logs",
                        "get_previous_logs",
                        "tail_logs",
                        "search_logs",
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
                "pod_name": {"type": "string", "description": "Pod名称"},
                "container_name": {
                    "type": "string",
                    "description": "容器名称（可选，未指定则获取所有容器日志）",
                },
                "follow": {
                    "type": "boolean",
                    "description": "是否跟踪日志（实时日志）",
                    "default": False,
                },
                "previous": {
                    "type": "boolean",
                    "description": "是否获取重启前的日志",
                    "default": False,
                },
                "since_seconds": {
                    "type": "integer",
                    "description": "获取多少秒内的日志",
                    "minimum": 1,
                    "maximum": 86400,
                },
                "since_time": {
                    "type": "string",
                    "description": "获取指定时间之后的日志，RFC3339格式",
                },
                "tail_lines": {
                    "type": "integer",
                    "description": "获取最后多少行日志",
                    "minimum": 1,
                    "maximum": 1000,
                    "default": 100,
                },
                "timestamps": {
                    "type": "boolean",
                    "description": "是否包含时间戳",
                    "default": True,
                },
                "limit_bytes": {
                    "type": "integer",
                    "description": "限制日志大小（字节）",
                    "minimum": 1024,
                    "maximum": 10485760,
                    "default": 1048576,
                },
                "search_pattern": {
                    "type": "string",
                    "description": "搜索模式（用于search_logs操作）",
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "搜索时是否忽略大小写",
                    "default": True,
                },
            },
            "required": ["operation", "pod_name"],
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
            "get_pod_logs": lambda: self._get_pod_logs(v1, parameters),
            "get_container_logs": lambda: self._get_container_logs(v1, parameters),
            "get_previous_logs": lambda: self._get_previous_logs(v1, parameters),
            "tail_logs": lambda: self._tail_logs(v1, parameters),
            "search_logs": lambda: self._search_logs(v1, parameters),
        }

        if operation in operation_map:
            return await operation_map[operation]()
        else:
            return {
                "success": False,
                "error": "不支持的操作",
                "message": f"未知的操作类型: {operation}",
            }

    async def _get_pod_logs(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取Pod所有容器的日志"""
        try:
            pod_name = parameters.get("pod_name")
            namespace = parameters.get("namespace", "default")
            tail_lines = parameters.get("tail_lines", 100)
            timestamps = parameters.get("timestamps", True)
            since_seconds = parameters.get("since_seconds")
            since_time = parameters.get("since_time")
            limit_bytes = parameters.get("limit_bytes", 1048576)

            loop = asyncio.get_event_loop()

            # 首先获取Pod信息，确定有哪些容器
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

            containers = pod.spec.containers or []
            init_containers = pod.spec.init_containers or []

            # 获取所有容器的日志
            container_logs = {}

            # 获取普通容器日志
            for container in containers:
                container_name = container.name
                try:
                    log_content = await self._fetch_container_logs(
                        v1,
                        namespace,
                        pod_name,
                        container_name,
                        tail_lines,
                        timestamps,
                        since_seconds,
                        since_time,
                        limit_bytes,
                    )
                    container_logs[container_name] = {
                        "type": "container",
                        "logs": log_content,
                        "lines": len(log_content.split("\n")) if log_content else 0,
                    }
                except Exception as e:
                    container_logs[container_name] = {
                        "type": "container",
                        "error": str(e),
                        "lines": 0,
                    }

            # 获取init容器日志
            for init_container in init_containers:
                container_name = init_container.name
                try:
                    log_content = await self._fetch_container_logs(
                        v1,
                        namespace,
                        pod_name,
                        container_name,
                        tail_lines,
                        timestamps,
                        since_seconds,
                        since_time,
                        limit_bytes,
                    )
                    container_logs[f"{container_name} (init)"] = {
                        "type": "init-container",
                        "logs": log_content,
                        "lines": len(log_content.split("\n")) if log_content else 0,
                    }
                except Exception as e:
                    container_logs[f"{container_name} (init)"] = {
                        "type": "init-container",
                        "error": str(e),
                        "lines": 0,
                    }

            return {
                "success": True,
                "operation": "get_pod_logs",
                "pod_name": pod_name,
                "namespace": namespace,
                "container_count": len(containers) + len(init_containers),
                "container_logs": container_logs,
                "parameters": {
                    "tail_lines": tail_lines,
                    "timestamps": timestamps,
                    "since_seconds": since_seconds,
                    "since_time": since_time,
                    "limit_bytes": limit_bytes,
                },
                "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "获取Pod日志失败", "message": str(e)}

    async def _get_container_logs(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取指定容器的日志"""
        try:
            pod_name = parameters.get("pod_name")
            namespace = parameters.get("namespace", "default")
            container_name = parameters.get("container_name")
            tail_lines = parameters.get("tail_lines", 100)
            timestamps = parameters.get("timestamps", True)
            since_seconds = parameters.get("since_seconds")
            since_time = parameters.get("since_time")
            limit_bytes = parameters.get("limit_bytes", 1048576)

            if not container_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "container_name是必需的参数",
                }

            # 获取容器日志
            log_content = await self._fetch_container_logs(
                v1,
                namespace,
                pod_name,
                container_name,
                tail_lines,
                timestamps,
                since_seconds,
                since_time,
                limit_bytes,
            )

            # 分析日志
            log_lines = log_content.split("\n") if log_content else []
            non_empty_lines = [line for line in log_lines if line.strip()]

            return {
                "success": True,
                "operation": "get_container_logs",
                "pod_name": pod_name,
                "namespace": namespace,
                "container_name": container_name,
                "logs": log_content,
                "total_lines": len(log_lines),
                "non_empty_lines": len(non_empty_lines),
                "log_size_bytes": len(log_content.encode()) if log_content else 0,
                "parameters": {
                    "tail_lines": tail_lines,
                    "timestamps": timestamps,
                    "since_seconds": since_seconds,
                    "since_time": since_time,
                    "limit_bytes": limit_bytes,
                },
                "timestamp": iso_utc_now(),
            }

        except ApiException as e:
            if e.status == 404:
                return {
                    "success": False,
                    "error": "Pod或容器不存在",
                    "message": f"在命名空间 {namespace} 中找不到Pod {pod_name} 或容器 {container_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "获取容器日志失败",
                    "message": str(e),
                }
        except Exception as e:
            return {"success": False, "error": "获取容器日志失败", "message": str(e)}

    async def _get_previous_logs(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取重启前的日志"""
        try:
            pod_name = parameters.get("pod_name")
            namespace = parameters.get("namespace", "default")
            container_name = parameters.get("container_name")
            tail_lines = parameters.get("tail_lines", 100)
            timestamps = parameters.get("timestamps", True)
            limit_bytes = parameters.get("limit_bytes", 1048576)

            if not container_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "container_name是必需的参数",
                }

            # 获取重启前的日志
            log_content = await self._fetch_container_logs(
                v1,
                namespace,
                pod_name,
                container_name,
                tail_lines,
                timestamps,
                None,
                None,
                limit_bytes,
                previous=True,
            )

            # 分析日志
            log_lines = log_content.split("\n") if log_content else []
            non_empty_lines = [line for line in log_lines if line.strip()]

            return {
                "success": True,
                "operation": "get_previous_logs",
                "pod_name": pod_name,
                "namespace": namespace,
                "container_name": container_name,
                "logs": log_content,
                "total_lines": len(log_lines),
                "non_empty_lines": len(non_empty_lines),
                "log_size_bytes": len(log_content.encode()) if log_content else 0,
                "note": "这是容器重启前的日志",
                "timestamp": iso_utc_now(),
            }

        except ApiException as e:
            if e.status == 400:
                return {
                    "success": False,
                    "error": "没有重启前的日志",
                    "message": f"容器 {container_name} 没有重启前的日志可用",
                }
            elif e.status == 404:
                return {
                    "success": False,
                    "error": "Pod或容器不存在",
                    "message": f"在命名空间 {namespace} 中找不到Pod {pod_name} 或容器 {container_name}",
                }
            else:
                return {
                    "success": False,
                    "error": "获取重启前日志失败",
                    "message": str(e),
                }
        except Exception as e:
            return {"success": False, "error": "获取重启前日志失败", "message": str(e)}

    async def _tail_logs(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取最新日志（tail功能）"""
        try:
            pod_name = parameters.get("pod_name")
            namespace = parameters.get("namespace", "default")
            container_name = parameters.get("container_name")
            tail_lines = parameters.get("tail_lines", 50)
            timestamps = parameters.get("timestamps", True)

            if not container_name:
                # 如果没有指定容器，获取所有容器的tail日志
                return await self._get_pod_logs(v1, parameters)

            # 获取最新的日志
            log_content = await self._fetch_container_logs(
                v1,
                namespace,
                pod_name,
                container_name,
                tail_lines,
                timestamps,
                300,
                None,
                524288,  # 最近5分钟，限制512KB
            )

            # 分析日志
            log_lines = log_content.split("\n") if log_content else []
            recent_lines = [line for line in log_lines if line.strip()][-tail_lines:]

            return {
                "success": True,
                "operation": "tail_logs",
                "pod_name": pod_name,
                "namespace": namespace,
                "container_name": container_name,
                "recent_logs": "\n".join(recent_lines),
                "lines_returned": len(recent_lines),
                "requested_lines": tail_lines,
                "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {"success": False, "error": "获取tail日志失败", "message": str(e)}

    async def _search_logs(
        self, v1: client.CoreV1Api, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """在日志中搜索指定模式"""
        try:
            pod_name = parameters.get("pod_name")
            namespace = parameters.get("namespace", "default")
            container_name = parameters.get("container_name")
            search_pattern = parameters.get("search_pattern")
            ignore_case = parameters.get("ignore_case", True)
            tail_lines = parameters.get("tail_lines", 500)
            timestamps = parameters.get("timestamps", True)

            if not search_pattern:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "search_pattern是必需的参数",
                }

            if not container_name:
                return {
                    "success": False,
                    "error": "缺少参数",
                    "message": "container_name是必需的参数",
                }

            # 获取日志内容
            log_content = await self._fetch_container_logs(
                v1,
                namespace,
                pod_name,
                container_name,
                tail_lines,
                timestamps,
                None,
                None,
                2097152,  # 2MB限制
            )

            if not log_content:
                return {
                    "success": True,
                    "operation": "search_logs",
                    "pod_name": pod_name,
                    "namespace": namespace,
                    "container_name": container_name,
                    "search_pattern": search_pattern,
                    "matching_lines": [],
                    "total_matches": 0,
                    "message": "没有找到日志内容",
                    "timestamp": iso_utc_now(),
                }

            # 搜索匹配的行
            log_lines = log_content.split("\n")
            matching_lines = []

            search_text = search_pattern.lower() if ignore_case else search_pattern

            for line_num, line in enumerate(log_lines, 1):
                search_line = line.lower() if ignore_case else line
                if search_text in search_line:
                    matching_lines.append(
                        {
                            "line_number": line_num,
                            "content": line.strip(),
                            "matches": search_line.count(search_text),
                        }
                    )

            # 限制返回的匹配行数
            limited_matches = matching_lines[:100]

            return {
                "success": True,
                "operation": "search_logs",
                "pod_name": pod_name,
                "namespace": namespace,
                "container_name": container_name,
                "search_pattern": search_pattern,
                "ignore_case": ignore_case,
                "matching_lines": limited_matches,
                "total_matches": len(matching_lines),
                "returned_matches": len(limited_matches),
                "total_lines_searched": len(log_lines),
                "timestamp": datetime.now(UTC_TZ).isoformat(),
            }

        except Exception as e:
            return {"success": False, "error": "搜索日志失败", "message": str(e)}

    async def _fetch_container_logs(
        self,
        v1: client.CoreV1Api,
        namespace: str,
        pod_name: str,
        container_name: str,
        tail_lines: Optional[int] = None,
        timestamps: bool = True,
        since_seconds: Optional[int] = None,
        since_time: Optional[str] = None,
        limit_bytes: Optional[int] = None,
        previous: bool = False,
    ) -> str:
        """获取容器日志的通用方法"""
        loop = asyncio.get_event_loop()

        # 构建参数
        log_params = {
            "name": pod_name,
            "namespace": namespace,
            "container": container_name,
            "timestamps": timestamps,
            "previous": previous,
        }

        if tail_lines:
            log_params["tail_lines"] = tail_lines
        if since_seconds:
            log_params["since_seconds"] = since_seconds
        if since_time:
            log_params["since_time"] = since_time
        if limit_bytes:
            log_params["limit_bytes"] = limit_bytes

        # 获取日志
        log_content = await loop.run_in_executor(
            self._executor, lambda: v1.read_namespaced_pod_log(**log_params)
        )

        return log_content or ""
