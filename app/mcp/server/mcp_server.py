#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: MCP服务器
"""

from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Any, Dict, List

from app.config.settings import config

logger = logging.getLogger("aiops.mcp.core")


class BaseTool(ABC):
    """工具基类"""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.parameters = self.get_parameters()

    @abstractmethod
    def get_parameters(self) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def execute(self, parameters: Dict[str, Any]) -> Any:
        pass

    async def cleanup(self) -> None:
        """释放工具资源，无状态工具无需额外处理"""


class MCPServer:
    """MCP服务器核心类"""

    def __init__(self):
        self.tools: Dict[str, BaseTool] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self._execution_tasks: set[asyncio.Task] = set()
        self._closing = False
        self.timeout = config.mcp.timeout
        logger.info("MCP服务器核心初始化完成")

    async def register_tool(self, tool: BaseTool) -> None:
        if self._closing:
            raise RuntimeError("MCP server is shutting down")
        self.tools[tool.name] = tool
        logger.info(f"工具已注册: {tool.name}")

    async def execute_tool(self, tool_name: str, parameters: Dict[str, Any]) -> Any:
        """执行工具"""
        if self._closing:
            raise RuntimeError("MCP server is shutting down")
        if tool_name not in self.tools:
            raise ValueError(f"未知的工具: {tool_name}")

        tool = self.tools[tool_name]
        logger.info(f"开始执行工具: {tool_name}, 参数: {parameters}")

        task = asyncio.current_task()
        self._execution_tasks.add(task)
        try:
            result = await asyncio.wait_for(tool.execute(parameters), timeout=self.timeout)
            logger.info(f"工具执行成功: {tool_name}")
            return result
        except Exception as e:
            logger.error(f"工具执行失败: {tool_name}, 错误: {str(e)}")
            raise
        finally:
            self._execution_tasks.discard(task)

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
        self._closing = True
        logger.info("正在关闭MCP服务器...")
        tasks = list(self._execution_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            for tool in self.tools.values():
                try:
                    await tool.cleanup()
                except Exception as e:
                    logger.error("工具 %s 清理失败，异常类型: %s", tool.name, type(e).__name__)
        finally:
            self._execution_tasks.clear()
            self.tools.clear()
            self.sessions.clear()
        logger.info("MCP服务器已关闭")
