#!/usr/bin/env python3
"""
创建MCP服务示例配置文件
"""

import os
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from app.mcp.market.config_templates import ConfigTemplateManager

def main():
    """创建示例配置文件"""
    output_dir = "config/mcp_examples"
    
    # 确保目录存在
    os.makedirs(output_dir, exist_ok=True)
    
    # 创建示例配置
    ConfigTemplateManager.create_sample_configs(output_dir)
    
    print(f"示例配置文件已创建在 {output_dir}/ 目录下")
    print("包含以下文件：")
    print("- mcp_services.development.yaml")
    print("- mcp_services.production.yaml") 
    print("- mcp_services.minimal.yaml")
    print("- README.md")

if __name__ == "__main__":
    main()