#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 主应用模块 - 提供FastAPI应用的创建和初始化功能
"""

import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.config.settings import config
from app.config.logging import setup_logging
from app.api.routes import register_routes
from app.api.middleware import register_middleware

start_time = time.time()

# 避免重复初始化的标记
_initialized = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI应用生命周期管理"""
    global _initialized

    # 避免重复启动日志 (uvicorn reload模式下可能多次调用)
    if not _initialized:
        # 启动时执行
        log = logging.getLogger("aiops")
        startup_time = time.time() - start_time
        log.info(f"AIOps平台启动完成，耗时: {startup_time:.2f}秒")
        log.info(f"服务地址: http://{config.host}:{config.port}")
        _initialized = True

    yield

    # 关闭时执行 (只在真正关闭时记录)
    if _initialized:
        log = logging.getLogger("aiops")
        total_time = time.time() - start_time
        log.info(f"AIOps平台运行总时长: {total_time:.2f}秒")
        log.info("AIOps平台已关闭")
        _initialized = False


def create_app():
    """创建FastAPI应用实例"""
    global _initialized

    app = FastAPI(
        title="AIOps Platform",
        description="AI-CloudOps智能运维平台",
        version="1.0.0",
        debug=config.debug,
        lifespan=lifespan,
    )

    # 设置日志系统 (只在首次初始化时记录启动日志)
    setup_logging(app)
    log = logging.getLogger("aiops")

    # 避免重复启动日志
    if not _initialized:
        log.info("=" * 50)
        log.info("AIOps平台启动中...")
        log.info(f"调试模式: {config.debug}")
        log.info(f"日志级别: {config.log_level}")
        log.info("=" * 50)

    # 注册中间件
    try:
        register_middleware(app)
        if not _initialized:
            log.info("中间件注册完成")
    except Exception as e:
        if not _initialized:
            log.error(f"中间件注册失败: {str(e)}")
            log.warning("将继续启动，但部分中间件功能可能不可用")

    # 注册路由
    try:
        register_routes(app)
        if not _initialized:
            log.info("路由注册完成")
    except Exception as e:
        if not _initialized:
            log.error(f"路由注册失败: {str(e)}")
            log.warning("将继续启动，但部分路由功能可能不可用")

    return app


app = create_app()

if __name__ == "__main__":
    """直接运行时的主入口"""
    import uvicorn

    logger = logging.getLogger("aiops")

    try:
        # 只在主进程中记录启动日志，避免reload模式下的重复日志
        import multiprocessing

        if multiprocessing.current_process().name == "MainProcess":
            logger.info(f"在 {config.host}:{config.port} 启动FastAPI服务器")

        uvicorn.run(
            "app.main:app",
            host=config.host,
            port=config.port,
            reload=config.debug,
            log_level=config.log_level.lower(),
        )
    except KeyboardInterrupt:
        if multiprocessing.current_process().name == "MainProcess":
            logger.info("收到中断信号，正在关闭服务...")
    except Exception as e:
        logger.error(f"服务启动失败: {str(e)}")
        raise
