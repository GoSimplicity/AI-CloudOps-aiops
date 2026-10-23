#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 主应用程序入口
"""

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import signal
import sys
import time
from typing import Any, AsyncGenerator, Dict, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import uvicorn

from app.api.middleware.auth import setup_auth
from app.api.middleware.cors import setup_cors

try:
    from ..config.settings import config
    from .server.mcp_server import MCPServer
    from .server.tools import create_tools as mcp_tools
except ImportError:
    try:
        from app.config.settings import config
        from app.mcp.server.mcp_server import MCPServer
        from app.mcp.server.tools import create_tools as mcp_tools
    except ImportError as e:
        logging.error(f"导入模块失败: {e}")
        sys.exit(1)

from app.config.envmode import ensure_debug_safe
from app.config.logging import setup_logging

setup_logging()
logger = logging.getLogger("aiops.mcp.main")

mcp_server: Optional[MCPServer] = None
active_sse_connections: set = set()
sse_shutdown = asyncio.Event()


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
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global mcp_server, sse_shutdown

    # 与主应用一致：生产环境开启 debug 时拒绝启动（MCP 也注册了同一套错误处理器）
    ensure_debug_safe(config.env, config.debug)
    logger.info("正在启动MCP服务端...")
    sse_shutdown = asyncio.Event()
    try:
        mcp_server = MCPServer()
        tools = mcp_tools()
        for tool in tools:
            await mcp_server.register_tool(tool)
            logger.info(f"已注册工具: {tool.name}")
        logger.info(f"MCP服务端启动完成，共注册 {len(tools)} 个工具")
        yield
    finally:
        sse_shutdown.set()
        active_sse_connections.clear()
        server, mcp_server = mcp_server, None
        if server is not None:
            try:
                await server.shutdown()
                logger.info("MCP服务端已关闭")
            except Exception as e:
                logger.error("关闭MCP服务端时出错，异常类型: %s", type(e).__name__)


# 创建FastAPI应用
app = FastAPI(
    title="AI-CloudOps MCP服务端",
    description="提供MCP工具调用能力的SSE服务端",
    version="1.0.0",
    lifespan=lifespan,
)

setup_auth(app)
setup_cors(app)


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


# 统一注册错误处理器（与主应用一致）
try:
    from app.api.middleware.error_handler import setup_error_handlers

    setup_error_handlers(app)
    logger.info("MCP 应用已注册统一错误处理器")
except Exception as e:
    logger.warning("MCP 错误处理器注册失败，异常类型: %s", type(e).__name__)


@app.get("/sse")
async def sse_endpoint(request: Request) -> StreamingResponse:
    """SSE端点，提供实时数据流"""
    if not mcp_server:
        raise HTTPException(status_code=503, detail="MCP服务器未初始化")

    server = mcp_server
    shutdown = sse_shutdown

    async def event_generator() -> AsyncGenerator[str, None]:
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
                    for tool in server.tools.values()
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

                from app.config.settings import config

                # 根据配置发送心跳
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=config.mcp.health_check_interval)
                    break
                except asyncio.TimeoutError:
                    pass
                heartbeat = {
                    "type": "heartbeat",
                    "timestamp": time.time(),
                    "connection_id": connection_id,
                }
                yield f"data: {json.dumps(heartbeat)}\n\n"

        except asyncio.CancelledError:
            logger.info(f"SSE连接 {connection_id} 已取消")
            raise
        except Exception as e:
            logger.error("SSE连接 %s 错误，异常类型: %s", connection_id, type(e).__name__)
            yield f"data: {json.dumps({'type': 'error', 'message': 'MCP stream failed', 'connection_id': connection_id})}\n\n"
        finally:
            # 清理连接
            active_sse_connections.discard(connection_id)

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
        # 验证工具是否存在
        if request.tool not in mcp_server.tools:
            return ToolResponse(
                request_id=request.request_id,
                tool=request.tool,
                result=None,
                error=f"工具 '{request.tool}' 不存在",
                status="error",
            )

        logger.info("执行工具调用: %s, 参数字段数: %s", request.tool, len(request.parameters))

        # 执行工具
        result = await mcp_server.execute_tool(
            tool_name=request.tool, parameters=request.parameters
        )

        execution_time = time.time() - start_time
        logger.info(f"工具 {request.tool} 执行完成，耗时: {execution_time:.2f}秒")

        failed = isinstance(result, dict) and result.get("success") is False
        return ToolResponse(
            request_id=request.request_id,
            tool=request.tool,
            result=result,
            error="工具报告操作失败，请查看结构化结果并核对实际状态" if failed else None,
            status="error" if failed else "success",
        )

    except asyncio.TimeoutError:
        return ToolResponse(
            request_id=request.request_id,
            tool=request.tool,
            error=f"Tool execution timed out after {config.mcp.timeout} seconds; 底层请求可能已生效，请核对实际状态，勿盲目重试",
            status="error",
        )
    except Exception as err:
        execution_time = time.time() - start_time
        logger.error("工具调用失败: %s, 异常类型: %s, 耗时: %.2f秒", request.tool, type(err).__name__, execution_time)

        return ToolResponse(
            request_id=request.request_id,
            tool=request.tool,
            result=None,
            error=f"Tool execution failed ({type(err).__name__})",
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
    from app.config.settings import config

    try:
        if "://" not in url:
            url = f"http://{url}"

        parsed = urlparse(url)
        host = parsed.hostname or config.host
        default_mcp_port = getattr(config, "mcp_default_port", 9000)
        port = parsed.port or config.mcp.server_url.split(":")[-1] or default_mcp_port

        return host, port
    except Exception as e:
        logger.warning("解析URL失败，使用默认配置，异常类型: %s", type(e).__name__)
        default_port = (
            config.mcp.server_url.split(":")[-1]
            if config.mcp.server_url
            else getattr(config, "mcp_default_port", 9000)
        )
        return config.host, (
            int(default_port) if str(default_port).isdigit() else default_port
        )


def signal_handler(signum, frame):
    """信号处理器"""
    logger.info(f"接收到信号 {signum}，正在优雅关闭...")
    # 清理全局资源
    active_sse_connections.clear()
    sys.exit(0)


def main():
    """主入口函数"""
    parser = argparse.ArgumentParser(description="AI-CloudOps MCP服务端")
    parser.add_argument("--host", default=None, help="服务器主机地址")
    parser.add_argument("--port", type=int, default=None, help="服务器端口")
    parser.add_argument("--log-level", default=None, help="日志级别")
    parser.add_argument("--reload", action="store_true", help="开发模式，自动重载")

    args = parser.parse_args()

    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # 启动服务
        logger.info("正在启动MCP服务端...")

        server_host = args.host or config.host
        if args.port:
            server_port = args.port
        else:
            _, server_port = parse_server_url(config.mcp.server_url)
        log_level = (args.log_level or config.log_level).lower()
        reload_enabled = args.reload or config.debug

        logger.info(f"服务器将在 {server_host}:{server_port} 启动")
        logger.info("启动命令: python -m app.mcp.main")

        uvicorn.run(
            "app.mcp.main:app",
            host=server_host,
            port=server_port,
            log_level=log_level,
            access_log=True,
            reload=reload_enabled,
        )

    except Exception as e:
        logger.error("启动服务失败，异常类型: %s", type(e).__name__)
        sys.exit(1)


if __name__ == "__main__":
    main()
