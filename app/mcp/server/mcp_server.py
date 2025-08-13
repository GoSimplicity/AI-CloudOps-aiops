#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List

logger = logging.getLogger("aiops.mcp.core")


class BaseTool(ABC):
    """工具基类"""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.parameters = self.get_parameters()

    @abstractmethod
    def get_parameters(self) -> Dict[str, Any]:
        """获取工具参数定义"""

    @abstractmethod
    async def execute(self, parameters: Dict[str, Any]) -> Any:
        """执行工具"""


class MCPServer:
    """MCP服务器核心类"""

    def __init__(self):
        self.tools: Dict[str, BaseTool] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}
        logger.info("MCP服务器核心初始化完成")

    async def register_tool(self, tool: BaseTool) -> None:
        """注册工具"""
        self.tools[tool.name] = tool
        logger.info(f"工具已注册: {tool.name}")

    async def execute_tool(self, tool_name: str, parameters: Dict[str, Any]) -> Any:
        """执行工具"""
        if tool_name not in self.tools:
            raise ValueError(f"未知的工具: {tool_name}")

        tool = self.tools[tool_name]
        logger.info(f"开始执行工具: {tool_name}, 参数: {parameters}")

        try:
            result = await tool.execute(parameters)
            logger.info(f"工具执行成功: {tool_name}")
            return result
        except Exception as e:
            logger.error(f"工具执行失败: {tool_name}, 错误: {str(e)}")
            raise

    async def get_tools_list(self) -> List[Dict[str, Any]]:
        """获取工具列表"""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self.tools.values()
        ]

    async def shutdown(self) -> None:
        """关闭服务器"""
        logger.info("正在关闭MCP服务器...")
        self.tools.clear()
        self.sessions.clear()
        logger.info("MCP服务器已关闭")
