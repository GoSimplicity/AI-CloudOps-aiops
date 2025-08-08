#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: API路由模块初始化文件，负责注册和管理所有API端点
"""

import logging

from fastapi import APIRouter

from app.models.response_models import APIResponse

logger = logging.getLogger("aiops.routes")

# 创建API v1路由器
api_v1 = APIRouter(prefix="/api/v1", tags=["api_v1"])

# 注册各个路由模块
try:
    from .health import router as health_router

    api_v1.include_router(health_router)
    logger.info("已注册健康检查路由")
except Exception as e:
    logger.warning(f"注册健康检查路由失败: {str(e)}")

try:
    from .predict import router as predict_router

    api_v1.include_router(predict_router)
    logger.info("已注册预测路由")
except Exception as e:
    logger.warning(f"注册预测路由失败: {str(e)}")

try:
    from .rca import router as rca_router

    api_v1.include_router(rca_router)
    logger.info("已注册根因分析路由")
except Exception as e:
    logger.warning(f"注册根因分析路由失败: {str(e)}")

try:
    from .autofix import router as autofix_router

    api_v1.include_router(autofix_router)
    logger.info("已注册自动修复路由")
except Exception as e:
    logger.warning(f"注册自动修复路由失败: {str(e)}")

try:
    from .assistant import router as assistant_router

    api_v1.include_router(assistant_router)
    logger.info("已注册智能助手路由")
except Exception as e:
    logger.warning(f"注册智能助手路由失败: {str(e)}")

try:
    from .multi_agent import router as multi_agent_router

    api_v1.include_router(multi_agent_router)
    logger.info("已注册多Agent路由")
except Exception as e:
    logger.warning(f"注册多Agent路由失败: {str(e)}")


def register_routes(app):
    """注册所有路由"""

    # 注册API v1路由
    app.include_router(api_v1)

    # 根路径
    @app.get("/", tags=["root"])
    async def root():
        return APIResponse(
            code=0,
            message="AIOps Platform API",
            data={
                "service": "AIOps Platform",
                "version": "1.0.0",
                "status": "running",
                "endpoints": {
                    "health": "/api/v1/health",
                    "prediction": "/api/v1/predict",
                    "rca": "/api/v1/rca",
                    "autofix": "/api/v1/autofix",
                    "assistant": "/api/v1/assistant",
                    "multi_agent": "/api/v1/multi-agent",
                    "docs": "/docs",
                    "redoc": "/redoc",
                },
            },
        ).model_dump()
