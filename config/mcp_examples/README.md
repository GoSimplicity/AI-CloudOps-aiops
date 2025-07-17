# MCP服务配置示例

## 文件说明

- `mcp_services.development.yaml` - 开发环境配置，包含基础服务
- `mcp_services.production.yaml` - 生产环境配置，使用服务发现
- `mcp_services.minimal.yaml` - 最小化配置，仅包含核心服务

## 使用方式

1. 复制合适的配置文件到 `config/mcp_services.yaml`
2. 根据需要修改服务地址和参数
3. 重启应用使配置生效

## 模板服务

- **time_service**: 时间服务，提供时间相关工具
- **file_service**: 文件服务，提供文件系统操作
- **calculator_service**: 计算器服务，提供数学计算功能

## 自定义配置

可以通过以下方式添加自定义服务：

```yaml
services:
  - name: my_custom_service
    display_name: 我的自定义服务
    description: 自定义服务描述
    version: 1.0.0
    server_url: http://localhost:9009
    timeout: 30
    max_retries: 3
    health_check_interval: 60
    weight: 1
    tags: ["custom", "utility"]
    metadata:
      author: MyTeam
      category: custom
      tools: ["custom_tool"]
```

## API接口

- GET /mcp/market/status - 获取市场状态
- GET /mcp/market/services - 获取服务列表
- POST /mcp/market/services - 添加服务
- DELETE /mcp/market/services/{name} - 删除服务
- POST /mcp/market/tools/execute - 执行工具
