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
                    "health": {
                        "system": "/api/v1/health",
                        "components": "/api/v1/components/health",
                        "metrics": "/api/v1/metrics/health",
                        "readiness": "/api/v1/readiness/health",
                        "liveness": "/api/v1/liveness/health"
                    },
                    "predict": {
                        "post": "/api/v1/predict",
                        "health": "/api/v1/predict/health",
                        "trend": "/api/v1/predict/trend",
                        "models": {
                            "info": "/api/v1/models/info",
                            "reload": "/api/v1/models/reload"
                        }
                    },
                    "rca": {
                        "create": "/api/v1/rca/create",
                        "health": "/api/v1/rca/health",
                        "aliases": {
                            "create": "/api/v1/rca",
                            "jobs": "/api/v1/rca/jobs",
                            "metrics": "/api/v1/rca/metrics",
                            "topology": "/api/v1/rca/topology",
                            "anomalies": "/api/v1/rca/anomalies",
                            "correlations": "/api/v1/rca/correlations"
                        }
                    },
                    "jobs": {
                        "create": "/api/v1/jobs/create",
                        "detail": "/api/v1/jobs/{job_id}"
                    },
                    "metrics": {"list": "/api/v1/metrics/list"},
                    "topology": {
                        "list": "/api/v1/topology/list"
                    },
                    "anomalies": {
                        "create": "/api/v1/anomalies/create",
                        "list": "/api/v1/anomalies/list"
                    },
                    "correlations": {
                        "create": "/api/v1/correlations/create",
                        "list": "/api/v1/correlations/list"
                    },
                    "cross_correlations": {
                        "create": "/api/v1/cross-correlations/create"
                    },
                    "timelines": {
                        "create": "/api/v1/timelines/create"
                    },
                    "history": {"list": "/api/v1/history/list"},
                    "autofix": {
                        "create": "/api/v1/autofix/create",
                        "health": "/api/v1/autofix/health",
                        "aliases": {
                            "create": "/api/v1/autofix",
                            "diagnose": "/api/v1/autofix/diagnose",
                            "workflow": "/api/v1/autofix/workflow",
                            "notify": "/api/v1/autofix/notify"
                        }
                    },
                    "workflows": {"create": "/api/v1/workflows/create"},
                    "diagnosis": {"create": "/api/v1/diagnosis/create"},
                    "tasks": {
                        "detail": "/api/v1/tasks/{task_id}",
                        "list": "/api/v1/history/list"
                    },
                    "assistant": {"reinitialize": "/api/v1/assistant/reinitialize"},
                    "queries": {"create": "/api/v1/queries/create"},
                    "sessions": {"create": "/api/v1/sessions/create"},
                    "knowledge": {"refresh": "/api/v1/knowledge/refresh"},
                    "documents": {"create": "/api/v1/documents/create"},
                    "cache": {"clear": "/api/v1/cache/clear"},
                    "multi_agent": {"health": "/api/v1/multi-agent/health"},
                    "repairs": {
                        "create": "/api/v1/repairs/create",
                        "create_all": "/api/v1/repairs/create-all"
                    },
                    "analysis": {
                        "create": "/api/v1/analysis/create"
                    },
                    "coordinator": {
                        "status": "/api/v1/coordinator/status"
                    },
                    "agents": {
                        "list": "/api/v1/agents/list"
                    },
                    "docs": "/docs",
                    "redoc": "/redoc"
                },
            },
        ).model_dump()
