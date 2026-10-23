#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 时间处理工具
"""

from datetime import datetime, timezone
from typing import Any, Dict

from ..mcp_server import BaseTool


class TimeTool(BaseTool):
    """获取当前时间的工具"""

    def __init__(self):
        super().__init__(
            name="get_current_time",
            description="获取当前时间，返回ISO-8601格式的时间字符串",
        )

    def get_parameters(self) -> Dict[str, Any]:
        """获取工具参数定义"""
        return {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "description": "时间格式，可选值：'iso'（默认，ISO-8601格式）、'timestamp'（Unix时间戳）",
                    "enum": ["iso", "timestamp"],
                    "default": "iso",
                },
                "timezone": {
                    "type": "string",
                    "description": "时区，例如'UTC'、'Asia/Shanghai'，默认为UTC",
                    "default": "UTC",
                },
            },
            "required": [],
        }

    async def execute(self, parameters: Dict[str, Any]) -> Any:
        """执行工具"""
        try:
            # 获取参数
            time_format = parameters.get("format", "iso")
            timezone_str = parameters.get("timezone", "UTC")

            # 获取当前时间
            now = datetime.now(timezone.utc)

            if time_format == "timestamp":
                # 返回Unix时间戳
                return {"timestamp": int(now.timestamp()), "timezone": timezone_str}
            else:
                # 返回ISO-8601格式
                return {
                    "time": now.isoformat().replace("+00:00", "Z"),
                    "format": "ISO-8601",
                    "timezone": "UTC",
                }

        except Exception as e:
            raise RuntimeError(f"获取时间失败: {str(e)}")
