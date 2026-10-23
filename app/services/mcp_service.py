#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps MCP协议服务
"""

import asyncio
from datetime import datetime
import logging
from typing import Any, Dict, Optional

from ..common.exceptions import AssistantError, ValidationError
from ..mcp.mcp_client import MCPAssistant
from .base import BaseService

logger = logging.getLogger("aiops.services.mcp")


class MCPService(BaseService):
    """AI-CloudOps MCP服务 - 封装MCP助手功能"""

    def __init__(self) -> None:
        super().__init__("mcp")
        self._mcp_assistant = None
        self._health_cache = {"healthy": False, "last_check": None}
        from app.config.settings import config

        self._cache_timeout = config.mcp.health_check_interval

    async def _do_initialize(self) -> None:
        """初始化服务"""
        self._mcp_assistant = MCPAssistant()
        logger.info("MCP服务初始化完成")

    async def _do_health_check(self) -> bool:
        """健康检查"""
        try:
            # 检查缓存
            now = datetime.now()
            if (
                self._health_cache["last_check"]
                and (now - self._health_cache["last_check"]).seconds
                < self._cache_timeout
            ):
                return self._health_cache["healthy"]

            # 如果assistant为None，尝试获取但不强制失败
            if not self._mcp_assistant:
                try:
                    self._mcp_assistant = MCPAssistant()
                except Exception as e:
                    logger.debug("获取MCP助手实例失败，异常类型: %s", type(e).__name__)
                    self._health_cache = {"healthy": False, "last_check": now}
                    return False

            # 检查MCP服务可用性
            if self._mcp_assistant:
                is_available = await self._mcp_assistant.is_available()
                self._health_cache = {"healthy": is_available, "last_check": now}
                return is_available
            else:
                self._health_cache = {"healthy": False, "last_check": now}
                return False

        except Exception as e:
            logger.warning("MCP健康检查失败，异常类型: %s", type(e).__name__)
            self._health_cache = {"healthy": False, "last_check": datetime.now()}
            return False

    async def get_answer(
        self,
        question: str,
        session_id: Optional[str] = None,
        **kwargs,  # 兼容其他参数但忽略
    ) -> Dict[str, Any]:
        """
        获取MCP回答
        """
        # 参数验证
        self._validate_question(question)

        # 确保服务就绪
        await self.initialize()
        await self._ensure_ready()

        try:
            # 导入配置
            from app.config.settings import config

            # 设置超时
            timeout = config.mcp.timeout

            # 调用MCP助手
            start_time = datetime.now()
            result = await asyncio.wait_for(
                self._mcp_assistant.process_query(question, session_id=session_id),
                timeout=timeout,
            )
            processing_time = (datetime.now() - start_time).total_seconds()

            # 构建返回格式，保持与RAG模式一致
            return {
                "answer": result,
                "confidence_score": 0.9,  # MCP工具调用置信度较高
                "source_documents": [],  # MCP模式不使用文档
                "cache_hit": False,  # MCP模式不使用缓存
                "processing_time": processing_time,
                "session_id": session_id,
                "success": True,
                "mode": "mcp",
                "timestamp": datetime.now().isoformat(),
            }

        except asyncio.TimeoutError as e:
            raise AssistantError("MCP请求超时") from e
        except AssistantError:
            raise
        except Exception as e:
            logger.error("MCP获取答案失败，异常类型: %s", type(e).__name__)
            raise AssistantError("MCP调用失败") from e

    async def get_service_health_info(self) -> Dict[str, Any]:
        """获取详细健康信息"""
        try:
            # 检查MCP服务可用性
            is_available = await self._do_health_check()

            # 获取可用工具信息
            tools_info = {}
            if self._mcp_assistant and is_available:
                try:
                    tools_data = await self._mcp_assistant.client.get_available_tools()
                    tools_info = {
                        "available_tools": len(tools_data.get("tools", [])),
                        "tools": [
                            tool.get("name") for tool in tools_data.get("tools", [])
                        ],
                    }
                except Exception as e:
                    logger.warning("获取工具信息失败，异常类型: %s", type(e).__name__)
                    tools_info = {"error": str(e)}

            return {
                "service": "mcp",
                "status": "healthy" if is_available else "unhealthy",
                "components": {
                    "mcp_assistant": bool(self._mcp_assistant),
                    "mcp_server": is_available,
                },
                "tools": tools_info,
                "health_cache": self._health_cache,
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as e:
            return {
                "service": "mcp",
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }

    async def get_mcp_config(self) -> Dict[str, Any]:
        """获取MCP配置信息"""
        from ..config.settings import config

        return {
            "service_type": "mcp",
            "mode": "tool_calling",
            "server_url": config.mcp.server_url,
            "timeout": config.mcp.timeout,
            "max_retries": config.mcp.max_retries,
            "health_check_interval": config.mcp.health_check_interval,
            "features": [
                "工具调用",
                "实时计算",
                "Kubernetes操作",
                "系统信息查询",
                "文件操作",
                "自动工具选择",
            ],
            "capabilities": [
                "时间查询",
                "数学计算",
                "K8s集群管理",
                "Pod操作",
                "服务管理",
                "配置查询",
                "日志获取",
                "资源监控",
            ],
        }

    def _validate_question(self, question: str) -> None:
        """验证问题"""
        if not question or not isinstance(question, str):
            raise ValidationError("question", "问题不能为空")

        question = question.strip()
        if not question:
            raise ValidationError("question", "问题内容不能为空")

        if len(question) > 1000:
            raise ValidationError("question", "问题长度不能超过1000字符")

    async def _ensure_ready(self) -> None:
        """确保服务就绪"""
        if not self._mcp_assistant:
            try:
                self._mcp_assistant = MCPAssistant()
                logger.info("MCP助手在运行时成功初始化")
            except Exception as e:
                logger.error("无法初始化MCP助手，异常类型: %s", type(e).__name__)
                raise AssistantError(f"MCP服务暂未就绪: {str(e)}")

    async def cleanup(self) -> None:
        """清理MCP服务资源"""
        try:
            self.logger.info("开始清理MCP服务资源...")

            # 清理MCP助手实例
            if self._mcp_assistant:
                try:
                    if hasattr(self._mcp_assistant, "cleanup"):
                        if asyncio.iscoroutinefunction(self._mcp_assistant.cleanup):
                            await self._mcp_assistant.cleanup()
                        else:
                            self._mcp_assistant.cleanup()
                    elif hasattr(self._mcp_assistant, "close"):
                        if asyncio.iscoroutinefunction(self._mcp_assistant.close):
                            await self._mcp_assistant.close()
                        else:
                            self._mcp_assistant.close()
                except Exception as e:
                    self.logger.warning("清理MCP助手实例失败，异常类型: %s", type(e).__name__)
                self._mcp_assistant = None

            # 重置健康缓存
            self._health_cache = {"healthy": False, "last_check": None}

            # 调用父类清理方法
            await super().cleanup()

            self.logger.info("MCP服务资源清理完成")

        except Exception as e:
            self.logger.error("MCP服务资源清理失败，异常类型: %s", type(e).__name__)
            raise


__all__ = ["MCPService"]
