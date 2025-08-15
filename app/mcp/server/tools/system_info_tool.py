#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: MCP 工具（system_info_tool）
"""

import platform
from typing import Any, Dict

import psutil

from ..mcp_server import BaseTool


class SystemInfoTool(BaseTool):
    """获取系统信息的工具"""

    def __init__(self):
        super().__init__(
            name="get_system_info",
            description="获取系统基本信息，包括CPU、内存、磁盘使用情况",
        )

    def get_parameters(self) -> Dict[str, Any]:
        """获取工具参数定义"""
        return {
            "type": "object",
            "properties": {
                "info_type": {
                    "type": "string",
                    "description": "信息类型，可选值：'all'（默认，所有信息）、'cpu'、'memory'、'disk'、'network'",
                    "enum": ["all", "cpu", "memory", "disk", "network"],
                    "default": "all",
                }
            },
            "required": [],
        }

    async def execute(self, parameters: Dict[str, Any]) -> Any:
        """执行工具"""
        try:
            info_type = parameters.get("info_type", "all")

            result = {
                "platform": platform.system(),
                "platform_version": platform.version(),
                "hostname": platform.node(),
                "python_version": platform.python_version(),
            }

            if info_type in ["all", "cpu"]:
                cpu_percent = psutil.cpu_percent(interval=1)
                cpu_count = psutil.cpu_count()

                try:
                    cpu_freq = psutil.cpu_freq()
                    frequency = cpu_freq.current if cpu_freq else None
                except (OSError, AttributeError):
                    frequency = None

                result["cpu"] = {
                    "usage_percent": cpu_percent,
                    "core_count": cpu_count,
                    "frequency_mhz": frequency,
                }

            if info_type in ["all", "memory"]:
                memory = psutil.virtual_memory()
                swap = psutil.swap_memory()

                result["memory"] = {
                    "total_gb": round(memory.total / (1024**3), 2),
                    "available_gb": round(memory.available / (1024**3), 2),
                    "used_percent": memory.percent,
                    "swap_total_gb": round(swap.total / (1024**3), 2),
                    "swap_used_percent": swap.percent,
                }

            if info_type in ["all", "disk"]:
                disk = psutil.disk_usage("/")

                result["disk"] = {
                    "total_gb": round(disk.total / (1024**3), 2),
                    "used_gb": round(disk.used / (1024**3), 2),
                    "free_gb": round(disk.free / (1024**3), 2),
                    "used_percent": round((disk.used / disk.total) * 100, 2),
                }

            if info_type in ["all", "network"]:
                net_io = psutil.net_io_counters()

                result["network"] = {
                    "bytes_sent_gb": round(net_io.bytes_sent / (1024**3), 2),
                    "bytes_recv_gb": round(net_io.bytes_recv / (1024**3), 2),
                    "packets_sent": net_io.packets_sent,
                    "packets_recv": net_io.packets_recv,
                }

            return result

        except Exception as e:
            raise RuntimeError(f"获取系统信息失败: {str(e)}") from e
