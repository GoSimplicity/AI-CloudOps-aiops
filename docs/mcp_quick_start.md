# MCP服务市场快速入门

## 概述

MCP服务市场让您可以像安装插件一样简单地添加和管理MCP服务，无需手动配置每个SSE连接。

## 快速开始

### 1. 启动应用

```bash
# 启动主应用
python app/main.py

# 或使用uvicorn
uvicorn app.main:app --reload --port 8000
```

### 2. 使用配置文件添加服务

将示例配置文件复制到主配置目录：

```bash
cp config/mcp_examples/mcp_services.development.yaml config/mcp_services.yaml
```

### 3. 通过API动态管理

#### 查看服务状态
```bash
curl http://localhost:8000/api/v1/mcp/market/status
```

#### 添加新服务
```bash
curl -X POST http://localhost:8000/api/v1/mcp/market/services \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my_time_service",
    "display_name": "我的时间服务",
    "description": "提供时间查询功能",
    "server_url": "http://localhost:9000"
  }'
```

#### 查看所有服务
```bash
curl http://localhost:8000/api/v1/mcp/market/services
```

#### 执行工具
```bash
curl -X POST http://localhost:8000/api/v1/mcp/market/tools/execute \
  -H "Content-Type: application/json" \
  -d '{
    "tool_name": "get_current_time",
    "parameters": {"timezone": "Asia/Shanghai"}
  }'
```

### 4. 通过助手使用MCP

助手会自动检测可用的MCP工具，优先使用工具回答：

```bash
# 直接询问助手
curl -X POST http://localhost:8000/api/v1/assistant/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "现在几点了？",
    "session_id": "test_session"
  }'
```

## 配置文件详解

### 基本配置

```yaml
# config/mcp_services.yaml
services:
  - name: time_service
    display_name: 时间服务
    description: 获取当前时间
    server_url: http://localhost:9000
    timeout: 30
    max_retries: 3
    health_check_interval: 60
    weight: 1
    tags: ["utility", "time"]
    metadata:
      tools: ["get_current_time", "get_timezone"]

  - name: file_service
    display_name: 文件服务
    description: 文件操作工具
    server_url: http://localhost:9001
    timeout: 30
    max_retries: 3
    tags: ["file", "system"]

# 全局设置
global_timeout: 30
max_concurrent_requests: 100
health_check_enabled: true
load_balancing: weighted_round_robin
cache_enabled: true
cache_ttl: 300
```

### 高级配置

```yaml
# 生产环境配置
services:
  - name: kubernetes_service
    display_name: Kubernetes服务
    description: K8s集群管理
    server_url: http://k8s-mcp:9100
    timeout: 60
    max_retries: 2
    health_check_interval: 120
    weight: 3
    tags: ["k8s", "devops"]

  - name: monitoring_service
    display_name: 监控服务
    description: 系统监控工具
    server_url: http://monitoring-mcp:9101
    timeout: 45
    max_retries: 2
    tags: ["monitoring", "metrics"]
```

## Python客户端示例

### 基本使用

```python
import asyncio
from app.mcp.market.client_v2 import MCPServiceManager

async def main():
    # 获取MCP客户端
    client = await MCPServiceManager.get_instance()
    
    # 添加服务
    from app.mcp.market.models import MCPService
    
    service = MCPService(
        name="my_service",
        display_name="我的服务",
        description="自定义服务",
        server_url="http://localhost:9000"
    )
    
    await client.add_service(service)
    
    # 执行工具
    response = await client.execute_tool("get_current_time")
    print(f"结果: {response.result}")
    
    # 列出所有服务
    services = await client.list_services()
    for service in services:
        print(f"服务: {service.display_name} - {service.health}")

if __name__ == "__main__":
    asyncio.run(main())
```

### 批量操作

```python
# 批量执行工具
requests = [
    {"tool_name": "get_current_time", "parameters": {"timezone": "UTC"}},
    {"tool_name": "calculate", "parameters": {"expression": "sqrt(16)"}},
    {"tool_name": "read_file", "parameters": {"file_path": "/tmp/test.txt"}}
]

results = await client.batch_execute_tools(requests)
for result in results:
    print(f"成功: {result.success}, 结果: {result.result}")
```

## 服务发现

### 自动发现

```python
# 发现新的MCP服务
service_urls = [
    "http://localhost:9002",
    "http://localhost:9003",
    "http://localhost:9004"
]

results = await client.discover_services(service_urls)
for url, success in results.items():
    print(f"{url}: {'成功' if success else '失败'}")
```

## 故障排除

### 常见问题

1. **服务连接失败**
   - 检查服务URL是否正确
   - 验证服务是否运行
   - 查看服务健康状态

2. **工具调用失败**
   - 确认工具参数格式
   - 检查服务健康状态
   - 验证工具名称是否正确

3. **配置不生效**
   - 重启应用
   - 检查配置文件语法
   - 验证配置路径

### 调试命令

```bash
# 检查服务健康
http :8000/api/v1/mcp/market/health

# 查看详细服务信息
http :8000/api/v1/mcp/market/services

# 测试工具调用
http POST :8000/api/v1/mcp/market/tools/execute tool_name=get_current_time

# 查看MCP配置
cat config/mcp_services.yaml
```

## 扩展开发

### 创建自定义MCP服务

1. 创建MCP服务（使用任何支持MCP协议的语言）
2. 配置服务信息端点 `/info` 和 `/health`
3. 在配置文件中添加服务
4. 重启应用或使用API动态添加

### 服务模板

使用配置模板快速创建服务：

```python
from app.mcp.market.config_templates import ConfigTemplateManager

# 使用时间服务模板
config = ConfigTemplateManager.create_config_from_template(
    "time_service",
    "http://my-server:9000",
    {"weight": 2, "timeout": 45}
)

# 保存配置
ConfigTemplateManager.save_config(config, "my_service.yaml")
```

## 性能优化

### 负载均衡

- 使用 `weight` 参数调整服务权重
- 启用 `health_check` 自动故障转移
- 配置合理的 `timeout` 和 `max_retries`

### 缓存策略

- 启用 `cache_enabled` 减少重复调用
- 调整 `cache_ttl` 控制缓存时间
- 使用 `max_concurrent_requests` 限制并发

## 监控和日志

### 查看日志

```bash
# 查看MCP相关日志
tail -f logs/aiops.mcp.*.log

# 查看助手集成日志
tail -f logs/aiops.assistant.mcp.log
```

### 监控指标

- 服务健康状态
- 工具调用成功率
- 平均响应时间
- 活跃服务数量

## 最佳实践

1. **配置管理**
   - 使用版本控制管理配置文件
   - 为不同环境创建专用配置
   - 定期备份配置

2. **服务管理**
   - 使用服务发现动态添加服务
   - 设置合理的健康检查间隔
   - 监控服务状态变化

3. **工具使用**
   - 优先使用官方提供的服务模板
   - 测试工具调用后再生产使用
   - 记录常用工具的使用模式