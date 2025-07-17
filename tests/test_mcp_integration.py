"""
MCP集成测试
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch

from app.core.agents.assistant.mcp_integration import MCPIntegration, get_mcp_integration


class TestMCPIntegration:
    """MCP集成测试类"""
    
    @pytest.fixture
    def mcp_integration(self):
        """MCP集成实例"""
        return MCPIntegration()
    
    @pytest.mark.asyncio
    async def test_initialization(self, mcp_integration):
        """测试初始化"""
        # 模拟成功初始化
        with patch('app.mcp.market.client_v2.MCPServiceManager.get_instance') as mock_get_instance:
            mock_client = AsyncMock()
            mock_get_instance.return_value = mock_client
            
            await mcp_integration.initialize()
            
            assert mcp_integration._initialized is True
            assert mcp_integration.mcp_client is mock_client
    
    @pytest.mark.asyncio
    async def test_initialization_failure(self, mcp_integration):
        """测试初始化失败"""
        # 模拟初始化失败
        with patch('app.mcp.market.client_v2.MCPServiceManager.get_instance') as mock_get_instance:
            mock_get_instance.side_effect = Exception("初始化失败")
            
            await mcp_integration.initialize()
            
            assert mcp_integration._initialized is False
            assert mcp_integration.mcp_client is None
    
    @pytest.mark.asyncio
    async def test_is_available(self, mcp_integration):
        """测试可用性检查"""
        # 测试未初始化
        mcp_integration._initialized = False
        assert await mcp_integration.is_available() is False
        
        # 测试已初始化
        mcp_integration._initialized = True
        mcp_integration.mcp_client = AsyncMock()
        assert await mcp_integration.is_available() is True
    
    @pytest.mark.asyncio
    async def test_get_market_status(self, mcp_integration):
        """测试获取市场状态"""
        mcp_integration._initialized = True
        mcp_integration.mcp_client = AsyncMock()
        
        expected_status = {
            "total_services": 3,
            "healthy_services": 3,
            "total_tools": 8
        }
        
        mcp_integration.mcp_client.get_market_status.return_value = expected_status
        
        status = await mcp_integration.get_market_status()
        
        assert status["available"] is True
        assert status["total_services"] == 3
    
    @pytest.mark.asyncio
    async def test_get_market_status_not_initialized(self, mcp_integration):
        """测试未初始化时的市场状态"""
        mcp_integration._initialized = False
        
        status = await mcp_integration.get_market_status()
        
        assert status["available"] is False
        assert "error" in status
    
    @pytest.mark.asyncio
    async def test_list_services(self, mcp_integration):
        """测试获取服务列表"""
        mcp_integration._initialized = True
        mcp_integration.mcp_client = AsyncMock()
        
        mock_services = [
            AsyncMock(name="service1"),
            AsyncMock(name="service2")
        ]
        
        mcp_integration.mcp_client.list_services.return_value = mock_services
        
        services = await mcp_integration.list_services()
        
        assert len(services) == 2
        mcp_integration.mcp_client.list_services.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_add_service(self, mcp_integration):
        """测试添加服务"""
        mcp_integration._initialized = True
        mcp_integration.mcp_client = AsyncMock()
        mcp_integration.mcp_client.add_service.return_value = True
        
        service_config = {
            "name": "test_service",
            "display_name": "测试服务",
            "server_url": "http://localhost:9000"
        }
        
        result = await mcp_integration.add_service(service_config)
        
        assert result is True
        mcp_integration.mcp_client.add_service.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_remove_service(self, mcp_integration):
        """测试移除服务"""
        mcp_integration._initialized = True
        mcp_integration.mcp_client = AsyncMock()
        mcp_integration.mcp_client.remove_service.return_value = True
        
        result = await mcp_integration.remove_service("test_service")
        
        assert result is True
        mcp_integration.mcp_client.remove_service.assert_called_once_with("test_service")
    
    @pytest.mark.asyncio
    async def test_execute_mcp_tool(self, mcp_integration):
        """测试执行MCP工具"""
        mcp_integration._initialized = True
        mcp_integration.mcp_client = AsyncMock()
        
        expected_response = AsyncMock()
        expected_response.success = True
        expected_response.result = {"time": "2024-01-15 10:30:00"}
        expected_response.service_name = "time_service"
        expected_response.response_time = 0.05
        expected_response.error = None
        
        mcp_integration.mcp_client.execute_tool.return_value = expected_response
        
        response = await mcp_integration.execute_mcp_tool(
            tool_name="get_current_time",
            parameters={"timezone": "UTC"}
        )
        
        assert response.success is True
        assert response.result["time"] == "2024-01-15 10:30:00"
        mcp_integration.mcp_client.execute_tool.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_execute_mcp_tool_not_initialized(self, mcp_integration):
        """测试未初始化时的工具执行"""
        mcp_integration._initialized = False
        
        response = await mcp_integration.execute_mcp_tool("test_tool")
        
        assert response.success is False
        assert "MCP未初始化" in response.error
    
    @pytest.mark.asyncio
    async def test_process_with_mcp(self, mcp_integration):
        """测试使用MCP处理查询"""
        mcp_integration._initialized = True
        mcp_integration.mcp_client = AsyncMock()
        
        mcp_integration.mcp_client.process_query.return_value = "当前时间是2024-01-15 10:30:00"
        
        result = await mcp_integration.process_with_mcp("现在几点了？")
        
        assert result["used_mcp"] is True
        assert "当前时间" in result["response"]
        mcp_integration.mcp_client.process_query.assert_called_once_with("现在几点了？", None)
    
    @pytest.mark.asyncio
    async def test_get_available_tools(self, mcp_integration):
        """测试获取可用工具"""
        mcp_integration._initialized = True
        mcp_integration.mcp_client = AsyncMock()
        
        expected_tools = {
            "time_service": [
                {"name": "get_current_time", "description": "获取当前时间"}
            ]
        }
        
        mcp_integration.mcp_client.get_available_tools.return_value = expected_tools
        
        tools = await mcp_integration.get_available_tools()
        
        assert "time_service" in tools
        assert len(tools["time_service"]) == 1
    
    @pytest.mark.asyncio
    async def test_get_services_by_tags(self, mcp_integration):
        """测试按标签获取服务"""
        mcp_integration._initialized = True
        mcp_integration.mcp_client = AsyncMock()
        
        mock_services = [AsyncMock(), AsyncMock()]
        mcp_integration.mcp_client.service_discovery.find_services_by_tags.return_value = mock_services
        
        services = await mcp_integration.get_services_by_tags(["utility", "time"])
        
        assert len(services) == 2
        mcp_integration.mcp_client.service_discovery.find_services_by_tags.assert_called_once_with(["utility", "time"])
    
    @pytest.mark.asyncio
    async def test_discover_new_services(self, mcp_integration):
        """测试发现新服务"""
        mcp_integration._initialized = True
        mcp_integration.mcp_client = AsyncMock()
        
        expected_results = {
            "http://localhost:9006": True,
            "http://localhost:9007": False
        }
        
        mcp_integration.mcp_client.discover_services.return_value = expected_results
        
        results = await mcp_integration.discover_new_services([
            "http://localhost:9006",
            "http://localhost:9007"
        ])
        
        assert results["http://localhost:9006"] is True
        assert results["http://localhost:9007"] is False
    
    @pytest.mark.asyncio
    async def test_batch_execute_tools(self, mcp_integration):
        """测试批量执行工具"""
        mcp_integration._initialized = True
        mcp_integration.mcp_client = AsyncMock()
        
        requests = [
            {"tool_name": "get_current_time", "parameters": {}},
            {"tool_name": "calculate", "parameters": {"expression": "2+2"}}
        ]
        
        expected_responses = [
            AsyncMock(success=True, result={"time": "10:30"}),
            AsyncMock(success=True, result={"result": 4})
        ]
        
        mcp_integration.mcp_client.batch_execute_tools.return_value = expected_responses
        
        results = await mcp_integration.batch_execute_tools(requests)
        
        assert len(results) == 2
        assert results[0].success is True
        assert results[1].result["result"] == 4
    
    @pytest.mark.asyncio
    async def test_shutdown(self, mcp_integration):
        """测试关闭"""
        mcp_integration._initialized = True
        mcp_integration.mcp_client = AsyncMock()
        
        mcp_integration.mcp_client.stop = AsyncMock()
        
        await mcp_integration.shutdown()
        
        assert mcp_integration.mcp_client is None
        assert mcp_integration._initialized is False


class TestGlobalIntegration:
    """全局集成测试类"""
    
    @pytest.mark.asyncio
    async def test_get_mcp_integration_singleton(self):
        """测试获取MCP集成的单例"""
        with patch('app.core.agents.assistant.mcp_integration.MCPIntegration') as mock_class:
            mock_instance = AsyncMock()
            mock_class.return_value = mock_instance
            
            integration1 = await get_mcp_integration()
            integration2 = await get_mcp_integration()
            
            # 应该返回同一个实例
            assert integration1 is integration2
            mock_instance.initialize.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_shutdown_mcp_integration(self):
        """测试关闭MCP集成"""
        with patch('app.core.agents.assistant.mcp_integration._mcp_integration') as mock_integration:
            mock_integration.shutdown = AsyncMock()
            
            from app.core.agents.assistant.mcp_integration import shutdown_mcp_integration
            await shutdown_mcp_integration()
            
            mock_integration.shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_integration_with_assistant():
    """测试与助手的集成"""
    # 这个测试验证MCP集成能够正确与助手系统协同工作
    
    # 模拟助手环境
    from app.core.agents.assistant.core import AssistantAgent
    
    # 注意：这是一个集成测试，需要完整的MCP环境
    # 在实际测试中可能需要使用测试配置
    
    with patch('app.core.agents.assistant.core.get_mcp_integration') as mock_get_integration:
        mock_mcp = AsyncMock()
        mock_mcp.is_available.return_value = True
        mock_mcp.process_with_mcp.return_value = {
            "used_mcp": True,
            "response": "当前时间是2024-01-15 10:30:00"
        }
        
        mock_get_integration.return_value = mock_mcp
        
        # 验证助手能够使用MCP
        agent = AssistantAgent()
        
        # 这里可以测试助手集成，但需要完整环境
        # 在实际测试中可能需要更复杂的设置
        assert mock_mcp.is_available.called


if __name__ == "__main__":
    pytest.main([__file__, "-v"])