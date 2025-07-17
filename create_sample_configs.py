#!/usr/bin/env python3
"""
创建MCP服务示例配置文件
"""

import os
import json
import yaml
from pathlib import Path

def create_sample_configs():
    """创建示例配置文件"""
    
    # 工具服务模板
    tool_templates = {
        "time_service": {
            "name": "time_service",
            "display_name": "时间服务",
            "description": "提供时间相关的工具服务",
            "version": "1.0.0",
            "server_url": "http://localhost:9000",
            "timeout": 30,
            "max_retries": 3,
            "health_check_interval": 60,
            "weight": 1,
            "tags": ["utility", "time", "basic"],
            "metadata": {
                "author": "AI-CloudOps",
                "category": "utility",
                "tools": ["get_current_time", "get_timezone", "convert_time"]
            }
        },
        "file_service": {
            "name": "file_service",
            "display_name": "文件服务",
            "description": "提供文件系统操作相关的工具服务",
            "version": "1.0.0", 
            "server_url": "http://localhost:9001",
            "timeout": 30,
            "max_retries": 3,
            "health_check_interval": 60,
            "weight": 2,
            "tags": ["file", "system", "io"],
            "metadata": {
                "author": "AI-CloudOps",
                "category": "system",
                "tools": ["read_file", "write_file", "list_directory", "create_directory"]
            }
        },
        "calculator_service": {
            "name": "calculator_service",
            "display_name": "计算器服务",
            "description": "提供数学计算相关的工具服务",
            "version": "1.0.0",
            "server_url": "http://localhost:9002",
            "timeout": 30,
            "max_retries": 3,
            "health_check_interval": 60,
            "weight": 1,
            "tags": ["math", "calculator", "utility"],
            "metadata": {
                "author": "AI-CloudOps",
                "category": "utility",
                "tools": ["calculate", "solve_equation", "statistics"]
            }
        }
    }
    
    # 创建输出目录
    output_dir = Path("config/mcp_examples")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 开发环境配置
    dev_services = [
        tool_templates["time_service"],
        tool_templates["file_service"],
        tool_templates["calculator_service"],
    ]
    
    dev_config = {
        "services": dev_services,
        "global_timeout": 30,
        "max_concurrent_requests": 50,
        "health_check_enabled": True,
        "load_balancing": "weighted_round_robin",
        "cache_enabled": True,
        "cache_ttl": 300,
        "environment": "development"
    }
    
    # 生产环境配置
    prod_services = [
        {**tool_templates["time_service"], "server_url": "http://mcp-time:9000"},
        {**tool_templates["file_service"], "server_url": "http://mcp-file:9001"},
        {**tool_templates["calculator_service"], "server_url": "http://mcp-calculator:9002"},
    ]
    
    prod_config = {
        "services": prod_services,
        "global_timeout": 60,
        "max_concurrent_requests": 200,
        "health_check_enabled": True,
        "load_balancing": "weighted_round_robin",
        "cache_enabled": True,
        "cache_ttl": 600,
        "environment": "production"
    }
    
    # 最小配置
    minimal_config = {
        "services": [
            tool_templates["time_service"],
            tool_templates["file_service"],
        ],
        "global_timeout": 30
    }
    
    # 保存配置文件
    def save_config(config, filename, format="yaml"):
        file_path = output_dir / filename
        if format.lower() == "json":
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        else:
            with open(file_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, indent=2, allow_unicode=True)
        print(f"已创建: {file_path}")
    
    save_config(dev_config, "mcp_services.development.yaml")
    save_config(dev_config, "mcp_services.development.json", format="json")
    save_config(prod_config, "mcp_services.production.yaml")
    save_config(prod_config, "mcp_services.production.json", format="json")
    save_config(minimal_config, "mcp_services.minimal.yaml")
    
    # 创建README
    readme_content = """# MCP服务配置示例

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
"""
    
    with open(output_dir / "README.md", "w", encoding="utf-8") as f:
        f.write(readme_content)
    
    print(f"示例配置文件已创建在: {output_dir}")

if __name__ == "__main__":
    create_sample_configs()