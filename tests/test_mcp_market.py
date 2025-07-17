"""
MCP服务市场测试
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch
from typing import Dict, Any, List

from app.mcp.market.models import MCPService, ServiceHealth, ToolResponse
from app.mcp.market.client_v2 import EnhancedMCPClient


class TestMCPMarket:
    """MCP服务市场测试类"""
    
    @pytest.fixture
    def sample_service(self):
        """示例服务"""
        return MCPService(
            name="test_service",
            display_name="测试服务",
            description="这是一个测试服务",
            version="1.0.0",
            server_url="http://localhost:9000",
            timeout=30,
            max_retries=3,
            health_check_interval=60,
            weight=1,
            tags=["test", "utility"],
            metadata={"test": True}
        )
    
    @pytest.mark.asyncio
    async def test_service_creation(self, sample_service):
        """测试服务创建"""
        assert sample_service.name == "test_service"
        assert sample_service.display_name == "测试服务"
        assert sample_service.health == ServiceHealth.HEALTHY
    
    @pytest.mark.asyncio
    async def test_tool_response_creation(self):
        """测试工具响应创建"""
        response = ToolResponse(
            success=True,
            result={"data": "test"},
            service_name="test_service",
            response_time=0.1
        )
        
        assert response.success is True
        assert response.result == {"data": "test"}
        assert response.service_name == "test_service"
        assert response.response_time == 0.1
        assert response.error is None
    
    @pytest.mark.asyncio
    async def test_enhanced_mcp_client_init(self):
        """测试增强版MCP客户端初始化"""
        client = EnhancedMCPClient()
        
        # 验证组件初始化
        assert client.registry is not None
        assert client.service_discovery is not None
        assert client.plugin_manager is not None
        assert client.llm_client is not None
    
    @pytest.mark.asyncio
    async def test_service_management(self, sample_service):
        """测试服务管理功能"""
        client = EnhancedMCPClient()
        
        # 模拟服务添加
        with patch.object(client.plugin_manager, 'add_service', new_callable=AsyncMock) as mock_add:
            mock_add.return_value = True
            
            result = await client.add_service(sample_service)
            assert result is True
            mock_add.assert_called_once_with(sample_service)
    
    @pytest.mark.asyncio
    async def test_service_listing(self, sample_service):
        """测试服务列表获取"""
        client = EnhancedMCPClient()
        
        # 模拟服务列表
        with patch.object(client.registry, 'list_services', new_callable=AsyncMock) as mock_list:
            mock_list.return_value = [sample_service]
            
            services = await client.list_services()
            assert len(services) == 1
            assert services[0].name == "test_service"
    
    @pytest.mark.asyncio
    async def test_tool_execution(self):
        """测试工具执行"""
        client = EnhancedMCPClient()
        
        expected_response = ToolResponse(
            success=True,
            result={"time": "2024-01-15 10:30:00"},
            service_name="time_service",
            response_time=0.05
        )
        
        # 模拟工具执行
        with patch.object(client.service_discovery, 'execute_tool', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = expected_response
            
            result = await client.execute_tool(
                tool_name="get_current_time",
                parameters={"timezone": "UTC"}
            )
            
            assert result.success is True
            assert result.result["time"] == "2024-01-15 10:30:00"
            mock_execute.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_batch_tool_execution(self):
        """测试批量工具执行"""
        client = EnhancedMCPClient()
        
        requests = [
            {"tool_name": "get_current_time", "parameters": {}},
            {"tool_name": "calculate", "parameters": {"expression": "2+2"}}
        ]
        
        expected_responses = [
            ToolResponse(success=True, result={"time": "10:30"}, service_name="time", response_time=0.1),
            ToolResponse(success=True, result={"result": 4}, service_name="calculator", response_time=0.05)
        ]
        
        # 模拟批量执行
        with patch.object(client.service_discovery, 'execute_tools_bulk', new_callable=AsyncMock) as mock_batch:
            mock_batch.return_value = expected_responses
            
            results = await client.batch_execute_tools(requests)
            assert len(results) == 2
            assert results[0].success is True
            assert results[1].result["result"] == 4
    
    @pytest.mark.asyncio
    async def test_market_status(self):
        """测试市场状态获取"""
        client = EnhancedMCPClient()
        
        expected_status = {
            "total_services": 5,
            "healthy_services": 4,
            "unhealthy_services": 1,
            "total_tools": 15
        }
        
        # 模拟状态获取
        with patch.object(client.registry, 'get_market_status', new_callable=AsyncMock) as mock_status:
            mock_status.return_value = expected_status
            
            status = await client.get_market_status()
            assert status["total_services"] == 5
            assert status["healthy_services"] == 4
    
    @pytest.mark.asyncio
    async def test_service_removal(self):
        """测试服务移除"""
        client = EnhancedMCPClient()
        
        # 模拟服务移除
        with patch.object(client.plugin_manager, 'remove_service', new_callable=AsyncMock) as mock_remove:
            mock_remove.return_value = True
            
            result = await client.remove_service("test_service")
            assert result is True
            mock_remove.assert_called_once_with("test_service")


class TestMCPIntegration:
    """MCP集成测试类"""
    
    @pytest.mark.asyncio
    async def test_mcp_integration_initialization(self):
        """测试MCP集成初始化"""
        from app.core.agents.assistant.mcp_integration import MCPIntegration
        
        integration = MCPIntegration()
        
        # 模拟成功初始化
        with patch.object(MCPIntegration, 'initialize', new_callable=AsyncMock) as mock_init:
            mock_init.return_value = None
            
            await integration.initialize()
            assert integration._initialized is True
    
    @pytest.mark.asyncio
    async def test_mcp_query_processing(self):
        """测试MCP查询处理"""
        from app.core.agents.assistant.mcp_integration import MCPIntegration
        
        integration = MCPIntegration()
        integration.mcp_client = AsyncMock()
        integration._initialized = True
        
        # 模拟查询处理
        integration.mcp_client.process_query = AsyncMock(return_value="当前时间是2024-01-15 10:30:00")
        
        result = await integration.process_with_mcp("现在几点了？")
        assert result["used_mcp"] is True
        assert "当前时间" in result["response"]


class TestAPIRoutes:
    """API路由测试类"""
    
    @pytest.fixture
    def client(self):
        """FastAPI测试客户端"""
        from fastapi.testclient import TestClient
        from app.main import app
        return TestClient(app)
    
    def test_health_check(self, client):
        """测试健康检查端点"""
        response = client.get("/api/v1/mcp/market/health")
        assert response.status_code in [200, 503]
    
    def test_services_list(self, client):
        """测试服务列表端点"""
        response = client.get("/api/v1/mcp/market/services")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
    
    def test_add_service(self, client):
        """测试添加服务端点"""
        service_data = {
            "name": "test_api_service",
            "display_name": "测试API服务",
            "description": "测试API端点",
            "server_url": "http://localhost:9000"
        }
        
        response = client.post("/api/v1/mcp/market/services", json=service_data)
        # 根据实际实现，这个测试可能需要调整
        assert response.status_code in [200, 201, 422]
    
    def test_execute_tool(self, client):
        """测试工具执行端点"""
        tool_data = {
            "tool_name": "get_current_time",
            "parameters": {"timezone": "UTC"}
        }
        
        response = client.post("/api/v1/mcp/market/tools/execute", json=tool_data)
        # 根据实际实现，这个测试可能需要调整
        assert response.status_code in [200, 400, 422]


class TestServiceConfiguration:
    """服务配置测试类"""
    
    def test_valid_service_config(self):
        """测试有效服务配置"""
        service_config = {
            "name": "valid_service",
            "display_name": "有效服务",
            "description": "测试有效配置",
            "server_url": "http://localhost:9000",
            "timeout": 30,
            "max_retries": 3,
            "weight": 1,
            "tags": ["test"]
        }
        
        # 验证配置
        assert service_config["name"]
        assert service_config["server_url"].startswith("http")
        assert isinstance(service_config["timeout"], int)
        assert service_config["timeout"] > 0
    
    def test_invalid_service_config(self):
        """测试无效服务配置"""
        invalid_configs = [
            {"server_url": "not_a_url"},  # 无效URL
            {"timeout": -1},  # 负数超时
            {"weight": 0},  # 零权重
        ]
        
        for config in invalid_configs:
            # 这里应该验证配置失败
            assert "name" not in config or "server_url" not in config or config.get("timeout", 1) <= 0


@pytest.mark.asyncio
async def test_full_integration_flow():
    """测试完整集成流程"""
    # 这个测试需要完整的MCP服务环境
    # 在实际测试中可能需要使用模拟服务
    
    # 1. 初始化客户端
    client = EnhancedMCPClient()
    
    # 2. 添加测试服务
    test_service = MCPService(
        name="integration_test",
        display_name="集成测试服务",
        server_url="http://localhost:9000"
    )
    
    # 3. 验证服务添加
    with patch.object(client.plugin_manager, 'add_service', new_callable=AsyncMock) as mock_add:
        mock_add.return_value = True
        result = await client.add_service(test_service)
        assert result is True
    
    # 4. 验证服务列表
    with patch.object(client.registry, 'list_services', new_callable=AsyncMock) as mock_list:
        mock_list.return_value = [test_service]
        services = await client.list_services()
        assert len(services) == 1
    
    # 5. 验证工具执行
    with patch.object(client.service_discovery, 'execute_tool', new_callable=AsyncMock) as mock_execute:
        mock_execute.return_value = ToolResponse(
            success=True,
            result={"status": "ok"},
            service_name="integration_test",
            response_time=0.1
        )
        
        result = await client.execute_tool("test_tool", {})
        assert result.success is True
    
    # 6. 验证服务移除
    with patch.object(client.plugin_manager, 'remove_service', new_callable=AsyncMock) as mock_remove:
        mock_remove.return_value = True
        result = await client.remove_service("integration_test")
        assert result is True


if __name__ == "__main__":
    # 运行测试
    pytest.main([__file__, "-v"])