#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops MCP服务端
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: MCP(Model-Context-Protocol)服务端，提供SSE传输接口
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
from urllib.parse import urlparse

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import importlib

import uvicorn

try:
    fastapi_module = importlib.import_module("fastapi")
    responses_module = importlib.import_module("fastapi.responses")
    cors_module = importlib.import_module("fastapi.middleware.cors")

    FastAPI = fastapi_module.FastAPI
    HTTPException = fastapi_module.HTTPException
    Request = fastapi_module.Request
    StreamingResponse = responses_module.StreamingResponse
    CORSMiddleware = cors_module.CORSMiddleware
except Exception as e:
    logging.error(f"缺少FastAPI依赖: {e}. 请安装 'fastapi' 和 'uvicorn' 包。")
    raise
from pydantic import BaseModel, Field

try:
    from app.config.settings import config
    from app.mcp.server.mcp_server import MCPServer
    from app.mcp.server.tools import tools as mcp_tools
except ImportError as e:
    logging.error(f"导入模块失败: {e}")
    sys.exit(1)

# 确保日志目录存在
log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(log_dir, "mcp_server.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("aiops.mcp.server")

# 全局变量
mcp_server: Optional[MCPServer] = None
active_sse_connections: set = set()


class ToolRequest(BaseModel):
    """工具调用请求模型"""

    tool: str = Field(..., description="工具名称")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="工具参数")
    request_id: Optional[str] = Field(None, description="请求ID")


class ToolResponse(BaseModel):
    """工具调用响应模型"""

    request_id: Optional[str] = Field(None, description="请求ID")
    tool: str = Field(..., description="工具名称")
    result: Any = Field(None, description="执行结果")
    error: Optional[str] = Field(None, description="错误信息")
    status: str = Field(default="success", description="执行状态")
    timestamp: float = Field(default_factory=time.time, description="时间戳")


@asynccontextmanager
async def lifespan(app):
    """应用生命周期管理"""
    global mcp_server

    # 启动时初始化
    logger.info("正在启动MCP服务端...")

    try:
        mcp_server = MCPServer()

        # 注册所有工具
        registered_count = 0
        if mcp_tools:
            for tool in mcp_tools:
                await mcp_server.register_tool(tool)
                registered_count += 1
                logger.info(f"已注册工具: {tool.name}")
        else:
            logger.warning("未找到可注册的工具")

        logger.info(f"MCP服务端启动完成，共注册 {registered_count} 个工具")

    except Exception as e:
        logger.error(f"MCP服务端启动失败: {e}")
        raise

    yield

    # 关闭时清理
    try:
        # 清理活跃的SSE连接
        active_sse_connections.clear()

        if mcp_server:
            await mcp_server.shutdown()
            logger.info("MCP服务端已关闭")
    except Exception as e:
        logger.error(f"关闭MCP服务端时出错: {e}")


# 创建FastAPI应用
app = FastAPI(
    title="AI-CloudOps MCP服务端",
    description="提供MCP工具调用能力的SSE服务端",
    version="1.0.0",
    lifespan=lifespan,
)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """健康检查接口"""
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "tools_count": len(mcp_server.tools) if mcp_server else 0,
        "active_connections": len(active_sse_connections),
        "server_info": {
            "version": "1.0.0",
            "python_version": sys.version,
            "pid": os.getpid(),
        },
    }


@app.get("/sse")
async def sse_endpoint(request):
    """SSE端点，提供实时数据流"""
    if not mcp_server:
        raise HTTPException(status_code=503, detail="MCP服务器未初始化")

    async def event_generator():
        """事件生成器"""
        connection_id = id(request)
        active_sse_connections.add(connection_id)

        try:
            # 发送连接事件
            yield f"data: {json.dumps({'type': 'connected', 'message': 'MCP连接已建立', 'connection_id': connection_id})}\n\n"

            # 发送可用工具列表
            tools_info = {
                "type": "tools_list",
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    }
                    for tool in mcp_server.tools.values()
                ],
                "timestamp": time.time(),
            }
            yield f"data: {json.dumps(tools_info)}\n\n"

            # 保持连接活跃
            while True:
                # 检查客户端是否断开连接
                if await request.is_disconnected():
                    logger.info(f"客户端连接 {connection_id} 已断开")
                    break

                # 每30秒发送心跳
                await asyncio.sleep(30)
                heartbeat = {
                    "type": "heartbeat",
                    "timestamp": time.time(),
                    "connection_id": connection_id,
                }
                yield f"data: {json.dumps(heartbeat)}\n\n"

        except asyncio.CancelledError:
            logger.info(f"SSE连接 {connection_id} 已取消")
        except Exception as e:
            logger.error(f"SSE连接 {connection_id} 错误: {str(e)}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e), 'connection_id': connection_id})}\n\n"
        finally:
            # 清理连接
            active_sse_connections.discard(connection_id)
            yield f"data: {json.dumps({'type': 'disconnected', 'message': 'MCP连接已断开', 'connection_id': connection_id})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用nginx缓冲
        },
    )


@app.post("/tools/execute")
async def execute_tool(request: ToolRequest) -> ToolResponse:
    """执行工具调用"""
    if not mcp_server:
        raise HTTPException(status_code=503, detail="MCP服务器未初始化")

    start_time = time.time()

    try:
        logger.info(f"执行工具调用: {request.tool}, 参数: {request.parameters}")

        # 验证工具是否存在
        if request.tool not in mcp_server.tools:
            return ToolResponse(
                request_id=request.request_id,
                tool=request.tool,
                result=None,
                error=f"工具 '{request.tool}' 不存在",
                status="error",
            )

        # 执行工具
        result = await mcp_server.execute_tool(
            tool_name=request.tool, parameters=request.parameters
        )

        execution_time = time.time() - start_time
        logger.info(f"工具 {request.tool} 执行完成，耗时: {execution_time:.2f}秒")

        return ToolResponse(
            request_id=request.request_id,
            tool=request.tool,
            result=result,
            status="success",
        )

    except Exception as err:
        execution_time = time.time() - start_time
        logger.error(f"工具调用失败: {str(err)}, 耗时: {execution_time:.2f}秒")

        return ToolResponse(
            request_id=request.request_id,
            tool=request.tool,
            result=None,
            error=str(err),
            status="error",
        )


@app.get("/tools")
async def list_tools() -> Dict[str, Any]:
    """获取可用工具列表"""
    if not mcp_server:
        raise HTTPException(status_code=503, detail="MCP服务器未初始化")

    tools = [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
            "metadata": getattr(tool, "metadata", {}),
        }
        for tool in mcp_server.tools.values()
    ]

    return {"tools": tools, "total_count": len(tools), "timestamp": time.time()}


@app.get("/tools/{tool_name}")
async def get_tool_info(tool_name: str) -> Dict[str, Any]:
    """获取特定工具信息"""
    if not mcp_server:
        raise HTTPException(status_code=503, detail="MCP服务器未初始化")

    if tool_name not in mcp_server.tools:
        raise HTTPException(status_code=404, detail=f"工具 '{tool_name}' 不存在")

    tool = mcp_server.tools[tool_name]
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
        "metadata": getattr(tool, "metadata", {}),
        "timestamp": time.time(),
    }


def parse_server_url(url: str) -> tuple[str, int]:
    """解析服务器URL，返回主机和端口"""
    try:
        if "://" not in url:
            url = f"http://{url}"

        parsed = urlparse(url)
        host = parsed.hostname or "0.0.0.0"
        port = parsed.port or 9000

        return host, port
    except Exception as e:
        logger.warning(f"解析URL失败: {e}，使用默认配置")
        return "0.0.0.0", 9000


def signal_handler(signum, _):
    """信号处理器"""
    logger.info(f"接收到信号 {signum}，正在优雅关闭...")
    # 清理全局资源
    active_sse_connections.clear()
    sys.exit(0)


if __name__ == "__main__":
    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # 启动服务
        logger.info("正在启动MCP服务端...")

        # 解析服务器配置
        server_host = getattr(config, "host", "0.0.0.0")

        # 从MCP服务器URL中提取端口
        server_url = getattr(config.mcp, "server_url", "http://0.0.0.0:9000")
        _, server_port = parse_server_url(server_url)

        logger.info(f"服务器将在 {server_host}:{server_port} 启动")

        uvicorn.run(
            "app.mcp.server.main:app",
            host=server_host,
            port=server_port,
            log_level=getattr(config, "log_level", "INFO").lower(),
            access_log=True,
            reload=getattr(config, "debug", False),
        )

    except Exception as e:
        logger.error(f"启动服务失败: {e}")
        sys.exit(1)
