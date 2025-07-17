"""
测试配置文件
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch
import logging

# 设置日志级别
logging.basicConfig(level=logging.INFO)

@pytest.fixture(scope="session")
def event_loop():
    """创建事件循环"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.fixture
def mock_mcp_client():
    """模拟MCP客户端"""
    client = AsyncMock()
    client.get_market_status.return_value = {
        "total_services": 3,
        "healthy_services": 3,
        "total_tools": 8
    }
    client.list_services.return_value = []
    client.add_service.return_value = True
    client.remove_service.return_value = True
    client.execute_tool.return_value = AsyncMock(
        success=True,
        result={"data": "test"},
        service_name="test_service",
        response_time=0.1
    )
    return client

@pytest.fixture(autouse=True)
def mock_redis():
    """模拟Redis连接"""
    with patch('app.core.cache.redis_cache_manager.redis.Redis') as mock_redis:
        mock_redis_client = AsyncMock()
        mock_redis.from_url.return_value = mock_redis_client
        yield mock_redis_client