"""
MCP集成模块
将增强版MCP客户端集成到助手系统中
"""

import logging
from typing import Dict, Any, List, Optional

from app.mcp.market.client_v2 import EnhancedMCPClient, MCPServiceManager
from app.mcp.market.models import MCPService, ToolResponse

logger = logging.getLogger("aiops.assistant.mcp")


class MCPIntegration:
    """MCP集成类 - 为助手提供MCP服务访问能力"""

    def __init__(self):
        self.mcp_client: Optional[EnhancedMCPClient] = None
        self._initialized = False

    async def initialize(self):
        """初始化MCP集成"""
        if self._initialized:
            return

        try:
            self.mcp_client = await MCPServiceManager.get_instance()
            self._initialized = True
            logger.info("MCP集成已初始化")
        except Exception as e:
            logger.error(f"MCP集成初始化失败: {e}")
            self._initialized = False

    async def shutdown(self):
        """关闭MCP集成"""
        if self.mcp_client:
            await MCPServiceManager.shutdown()
            self.mcp_client = None
            self._initialized = False
            logger.info("MCP集成已关闭")

    async def is_available(self) -> bool:
        """检查MCP是否可用"""
        return self._initialized and self.mcp_client is not None

    async def get_market_status(self) -> Dict[str, Any]:
        """获取MCP市场状态"""
        if not self.mcp_client:
            return {"available": False, "error": "MCP未初始化"}

        try:
            status = await self.mcp_client.get_market_status()
            return {"available": True, **status}
        except Exception as e:
            return {"available": False, "error": str(e)}

    async def list_services(self) -> List[MCPService]:
        """获取所有MCP服务"""
        if not self.mcp_client:
            return []

        try:
            return await self.mcp_client.list_services()
        except Exception as e:
            logger.error(f"获取MCP服务列表失败: {e}")
            return []

    async def add_service(self, service_config: Dict[str, Any]) -> bool:
        """添加MCP服务"""
        if not self.mcp_client:
            return False

        try:
            from app.mcp.market.models import MCPService
            service = MCPService(**service_config)
            return await self.mcp_client.add_service(service)
        except Exception as e:
            logger.error(f"添加MCP服务失败: {e}")
            return False

    async def remove_service(self, service_name: str) -> bool:
        """移除MCP服务"""
        if not self.mcp_client:
            return False

        try:
            return await self.mcp_client.remove_service(service_name)
        except Exception as e:
            logger.error(f"移除MCP服务失败: {e}")
            return False

    async def execute_mcp_tool(
        self,
        tool_name: str,
        parameters: Dict[str, Any] = None,
        preferred_services: Optional[List[str]] = None
    ) -> ToolResponse:
        """执行MCP工具调用"""
        if not self.mcp_client:
            return ToolResponse(
                success=False,
                result=None,
                service_name="",
                response_time=0,
                error="MCP未初始化"
            )

        if parameters is None:
            parameters = {}

        try:
            return await self.mcp_client.execute_tool(
                tool_name=tool_name,
                parameters=parameters,
                preferred_services=preferred_services
            )
        except Exception as e:
            logger.error(f"执行MCP工具失败: {e}")
            return ToolResponse(
                success=False,
                result=None,
                service_name="",
                response_time=0,
                error=str(e)
            )

    async def process_with_mcp(self, question: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """使用MCP工具处理查询"""
        if not self.mcp_client:
            return {"used_mcp": False, "response": "MCP服务不可用"}

        try:
            # 使用增强版MCP客户端处理查询
            response = await self.mcp_client.process_query(
                question=question,
                preferred_services=context.get("preferred_services") if context else None
            )

            return {
                "used_mcp": True,
                "response": response,
                "mcp_status": await self.get_market_status()
            }
        except Exception as e:
            logger.error(f"MCP处理查询失败: {e}")
            return {"used_mcp": False, "response": f"MCP处理失败: {e}"}

    async def get_available_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        """获取所有可用工具"""
        if not self.mcp_client:
            return {}

        try:
            return await self.mcp_client.get_available_tools()
        except Exception as e:
            logger.error(f"获取可用工具失败: {e}")
            return {}

    async def get_service_tools(self, service_name: str) -> List[Dict[str, Any]]:
        """获取指定服务的工具"""
        if not self.mcp_client:
            return []

        try:
            return await self.mcp_client.registry.get_service_tools(service_name)
        except Exception as e:
            logger.error(f"获取服务工具失败: {e}")
            return []

    async def discover_new_services(self, service_urls: List[str]) -> Dict[str, bool]:
        """发现新的MCP服务"""
        if not self.mcp_client:
            return {}

        try:
            return await self.mcp_client.discover_services(service_urls)
        except Exception as e:
            logger.error(f"发现新服务失败: {e}")
            return {}

    async def wait_for_service(self, service_name: str, timeout: float = 30) -> bool:
        """等待服务变为可用"""
        if not self.mcp_client:
            return False

        try:
            return await self.mcp_client.wait_for_service(service_name, timeout)
        except Exception as e:
            logger.error(f"等待服务失败: {e}")
            return False

    async def get_services_by_tags(self, tags: List[str]) -> List[MCPService]:
        """按标签获取服务"""
        if not self.mcp_client:
            return []

        try:
            return await self.mcp_client.service_discovery.find_services_by_tags(tags)
        except Exception as e:
            logger.error(f"按标签获取服务失败: {e}")
            return []

    async def batch_execute_tools(
        self,
        requests: List[Dict[str, Any]],
        parallel: bool = True,
        max_concurrent: int = 5
    ) -> List[ToolResponse]:
        """批量执行工具调用"""
        if not self.mcp_client:
            return []

        try:
            return await self.mcp_client.batch_execute_tools(
                requests=requests,
                parallel=parallel,
                max_concurrent=max_concurrent
            )
        except Exception as e:
            logger.error(f"批量执行工具失败: {e}")
            return []


# 全局单例
_mcp_integration: Optional[MCPIntegration] = None


async def get_mcp_integration() -> MCPIntegration:
    """获取MCP集成实例"""
    global _mcp_integration
    if _mcp_integration is None:
        _mcp_integration = MCPIntegration()
        await _mcp_integration.initialize()
    return _mcp_integration


async def shutdown_mcp_integration():
    """关闭MCP集成"""
    global _mcp_integration
    if _mcp_integration is not None:
        await _mcp_integration.shutdown()
        _mcp_integration = None
