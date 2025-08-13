#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.middleware import register_middleware
from app.api.routes import register_routes
from app.config.logging import setup_logging
from app.config.settings import config
from app.db import init_engine_and_session

start_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger = logging.getLogger("aiops")
    startup_time = time.time() - start_time
    logger.info(f"AIOps平台启动完成，耗时: {startup_time:.2f}秒")
    logger.info(f"服务地址: http://{config.host}:{config.port}")
    # 在启动阶段预热数据库连接（失败不阻塞）
    try:
        init_engine_and_session()
    except Exception:
        pass
    
    yield
    
    total_time = time.time() - start_time
    logger.info(f"AIOps平台运行总时长: {total_time:.2f}秒")
    logger.info("AIOps平台已关闭")


def create_app():
    """创建FastAPI应用实例"""
    app = FastAPI(
        title="AIOps Platform",
        description="AI-CloudOps智能运维平台",
        version="1.0.0",
        debug=config.debug,
        lifespan=lifespan,
    )

    setup_logging(app)
    logger = logging.getLogger("aiops")

    logger.info("=" * 50)
    logger.info("AIOps平台启动中...")
    logger.info(f"调试模式: {config.debug}")
    logger.info(f"日志级别: {config.log_level}")
    logger.info("=" * 50)

    try:
        register_middleware(app)
        logger.info("中间件注册完成")
    except Exception as e:
        logger.error(f"中间件注册失败: {e}")
        logger.warning("将继续启动，但部分中间件功能可能不可用")

    try:
        register_routes(app)
        logger.info("路由注册完成")
    except Exception as e:
        logger.error(f"路由注册失败: {e}")
        logger.warning("将继续启动，但部分路由功能可能不可用")

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    logger = logging.getLogger("aiops")

    try:
        logger.info(f"在 {config.host}:{config.port} 启动FastAPI服务器")

        uvicorn.run(
            "app.main:app",
            host=config.host,
            port=config.port,
            reload=config.debug,
            log_level=config.log_level.lower(),
        )
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭服务...")
    except Exception as e:
        logger.error(f"服务启动失败: {e}")
        raise
