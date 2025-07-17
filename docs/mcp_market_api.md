# MCP服务市场API文档

## 概述

MCP服务市场提供插件化的MCP服务管理功能，支持动态添加、移除和管理多个MCP服务，实现工具调用的负载均衡和故障转移。

## 基础信息

- **Base URL**: `/api/v1/mcp/market`
- **Content-Type**: `application/json`
- **认证**: Bearer Token (可选)

## API端点

### 1. 获取市场状态

**GET** `/status`

获取MCP市场的整体状态信息。

#### 响应示例
```json
{
  "available": true,
  "total_services": 5,
  "healthy_services": 4,
  "unhealthy_services": 1,
  "total_tools": 15
}
```

### 2. 获取服务列表

**GET** `/services`

获取所有已注册的MCP服务列表。

#### 响应示例
```json
[
  {
    "name": "time_service",
    "display_name": "时间服务",
    "description": "提供时间相关的工具服务",
    "version": "1.0.0",
    "server_url": "http://localhost:9000",
    "health": "healthy",
    "last_check": "2024-01-15T10:30:00",
    "weight": 1,
    "tags": ["utility", "time", "basic"],
    "metadata": {
      "author": "AI-CloudOps",
      "category": "utility",
      "tools": ["get_current_time", "get_timezone", "convert_time"]
    }
  }
]
```

### 3. 添加服务

**POST** `/services`

添加新的MCP服务到市场。

#### 请求体
```json
{
  "name": "new_service",
  "display_name": "新服务",
  "description": "新添加的MCP服务",
  "version": "1.0.0",
  "server_url": "http://localhost:9006",
  "timeout": 30,
  "max_retries": 3,
  "health_check_interval": 60,
  "weight": 1,
  "tags": ["custom", "utility"],
  "metadata": {
    "author": "MyTeam",
    "category": "custom",
    "tools": ["custom_tool"]
  }
}
```

#### 响应示例
```json
{
  "success": true,
  "service": {
    "name": "new_service",
    "display_name": "新服务",
    ...
  }
}
```

### 4. 移除服务

**DELETE** `/services/{service_name}`

移除指定的MCP服务。

#### 响应示例
```json
{
  "success": true,
  "service_name": "new_service"
}
```

### 5. 获取单个服务

**GET** `/services/{service_name}`

获取指定服务的详细信息。

#### 响应示例
```json
{
  "name": "time_service",
  "display_name": "时间服务",
  "description": "提供时间相关的工具服务",
  "version": "1.0.0",
  "server_url": "http://localhost:9000",
  "health": "healthy",
  "last_check": "2024-01-15T10:30:00",
  "weight": 1,
  "tags": ["utility", "time", "basic"],
  "metadata": {
    "author": "AI-CloudOps",
    "category": "utility",
    "tools": ["get_current_time", "get_timezone", "convert_time"]
  }
}
```

### 6. 获取所有可用工具

**GET** `/tools`

获取所有MCP服务提供的工具列表。

#### 响应示例
```json
{
  "time_service": [
    {
      "name": "get_current_time",
      "description": "获取当前时间",
      "parameters": {
        "timezone": {
          "type": "string",
          "description": "时区",
          "default": "UTC"
        }
      }
    }
  ],
  "file_service": [
    {
      "name": "read_file",
      "description": "读取文件内容",
      "parameters": {
        "file_path": {
          "type": "string",
          "description": "文件路径"
        }
      }
    }
  ]
}
```

### 7. 执行工具

**POST** `/tools/execute`

执行指定的MCP工具。

#### 请求体
```json
{
  "tool_name": "get_current_time",
  "parameters": {
    "timezone": "Asia/Shanghai"
  },
  "preferred_services": ["time_service"]
}
```

#### 响应示例
```json
{
  "success": true,
  "result": {
    "current_time": "2024-01-15 18:30:00",
    "timezone": "Asia/Shanghai",
    "timestamp": 1705315800
  },
  "service_name": "time_service",
  "response_time": 0.05,
  "error": null
}
```

### 8. 批量执行工具

**POST** `/tools/batch_execute`

批量执行多个工具调用。

#### 请求体
```json
[
  {
    "tool_name": "get_current_time",
    "parameters": {"timezone": "UTC"}
  },
  {
    "tool_name": "calculate",
    "parameters": {"expression": "2 + 2"}
  }
]
```

#### 响应示例
```json
[
  {
    "success": true,
    "result": {"current_time": "2024-01-15 10:30:00", "timezone": "UTC"},
    "service_name": "time_service",
    "response_time": 0.03
  },
  {
    "success": true,
    "result": {"result": 4, "expression": "2 + 2"},
    "service_name": "calculator_service",
    "response_time": 0.02
  }
]
```

### 9. 服务发现

**POST** `/discover`

发现并注册新的MCP服务。

#### 请求体
```json
{
  "service_urls": [
    "http://localhost:9006",
    "http://localhost:9007"
  ]
}
```

#### 响应示例
```json
{
  "success": true,
  "results": {
    "http://localhost:9006": true,
    "http://localhost:9007": false
  }
}
```

### 10. 健康检查

**GET** `/health`

检查MCP市场的健康状态。

#### 响应示例
```json
{
  "status": "healthy",
  "services": {
    "total_services": 5,
    "healthy_services": 4,
    "unhealthy_services": 1
  }
}
```

## 错误处理

所有API端点都遵循统一的错误响应格式：

```json
{
  "detail": "错误描述信息"
}
```

## 状态码

- **200**: 成功
- **400**: 请求参数错误
- **404**: 资源未找到
- **500**: 服务器内部错误

## 使用示例

### Python客户端示例

```python
import requests

# 添加服务
service_config = {
    "name": "custom_service",
    "display_name": "自定义服务",
    "description": "自定义MCP服务",
    "server_url": "http://localhost:9006"
}

response = requests.post(
    "http://localhost:8000/api/v1/mcp/market/services",
    json=service_config
)

# 执行工具
tool_request = {
    "tool_name": "get_current_time",
    "parameters": {"timezone": "Asia/Shanghai"}
}

response = requests.post(
    "http://localhost:8000/api/v1/mcp/market/tools/execute",
    json=tool_request
)
```

## 配置说明

MCP服务配置存储在 `config/mcp_services.yaml` 文件中，支持以下配置项：

```yaml
services:
  - name: service_name
    display_name: 显示名称
    description: 服务描述
    server_url: http://localhost:9000
    timeout: 30
    max_retries: 3
    health_check_interval: 60
    weight: 1
    tags: ["utility", "custom"]
    metadata:
      author: TeamName
      category: utility
      tools: ["tool1", "tool2"]

global_timeout: 30
max_concurrent_requests: 100
health_check_enabled: true
load_balancing: weighted_round_robin
cache_enabled: true
cache_ttl: 300
```