#!/usr/bin/env python3
"""
启动 AI-CloudOps gRPC 服务器
"""

import sys
import asyncio
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from app.services.grpc_server import main

if __name__ == "__main__":
    print("🚀 启动 AI-CloudOps gRPC 服务器...")
    asyncio.run(main())
