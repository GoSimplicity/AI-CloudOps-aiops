#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
多Agent修复API路由
Author: AI Assistant
License: Apache 2.0
Description: 提供多Agent协作的K8s修复API接口
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.config.settings import config
from app.core.agents.coordinator import K8sCoordinatorAgent
from app.core.agents.detector import K8sDetectorAgent
from app.models.response_models import APIResponse, PaginatedListAPIResponse
from app.utils.pagination import process_list_with_pagination_and_search
from app.utils.time_utils import iso_utc_now
from app.utils.validators import sanitize_input, validate_deployment_name, validate_namespace

logger = logging.getLogger("aiops.multi_agent")

router = APIRouter(tags=["multi_agent"])


class RepairRequest(BaseModel):
    deployment: str = Field(..., description="目标部署名称")
    namespace: Optional[str] = Field(default="default", description="命名空间")


class RepairAllRequest(BaseModel):
    namespace: Optional[str] = Field(default="default", description="命名空间")


class ClusterRequest(BaseModel):
    cluster_name: Optional[str] = Field(default="default", description="集群名称")


# 初始化协调器
coordinator = K8sCoordinatorAgent()
_detector = K8sDetectorAgent()

@router.get("/metrics")
async def multi_agent_metrics():
    """导出多Agent修复指标（内存级）。"""
    try:
        m = coordinator.metrics if hasattr(coordinator, "metrics") else {}
        return APIResponse(
            code=0,
            message="多Agent指标获取成功",
            data={
                "total_workflows": m.get("total_workflows", 0),
                "successful_workflows": m.get("successful_workflows", 0),
                "rolled_back": m.get("rolled_back", 0),
                "avg_success_rate": m.get("avg_success_rate", 0.0),
                "config": {
                    "safe_mode": bool(config.remediation.safe_mode),
                    "dry_run": bool(config.remediation.dry_run),
                    "allow_rollback": bool(config.remediation.allow_rollback),
                },
                "timestamp": iso_utc_now(),
            },
        ).model_dump()
    except Exception as e:
        logger.error(f"多Agent指标获取失败: {str(e)}")
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=500, detail=f"多Agent指标获取失败: {str(e)}") from e


@router.post("/repairs/create")
async def create_deployment_repair(request_data: RepairRequest):
    """创建单个部署修复"""
    try:
        deployment = sanitize_input(request_data.deployment)
        namespace = sanitize_input(request_data.namespace)

        # 验证参数
        if not deployment:
            raise HTTPException(status_code=400, detail="必须提供部署名称")

        if not validate_deployment_name(deployment):
            raise HTTPException(status_code=400, detail="无效的部署名称")

        if not validate_namespace(namespace):
            raise HTTPException(status_code=400, detail="无效的命名空间名称")

        logger.info(f"开始多Agent修复部署: {deployment} in {namespace}")

        # 执行完整工作流
        result = await coordinator.run_full_workflow(deployment=deployment, namespace=namespace)
        return APIResponse(code=0, message="部署修复完成", data=result).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"修复部署失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"修复部署失败: {str(e)}") from e


@router.post("/repairs/create-all")
async def create_all_repairs(request_data: RepairAllRequest):
    """创建命名空间下所有部署修复"""
    try:
        namespace = sanitize_input(request_data.namespace)

        if not validate_namespace(namespace):
            raise HTTPException(status_code=400, detail="无效的命名空间名称")

        logger.info(f"开始修复命名空间 {namespace} 下的所有部署")

        # 执行批量修复
        result = await coordinator.run_batch_workflow(namespace=namespace)
        return APIResponse(code=0, message="批量修复完成", data=result).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"批量修复失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"批量修复失败: {str(e)}") from e


@router.post("/analysis/create")
async def create_cluster_analysis(request_data: ClusterRequest):
    """创建集群健康状态分析"""
    try:
        cluster_name = sanitize_input(request_data.cluster_name)

        logger.info(f"开始分析集群: {cluster_name}")

        # 执行集群分析（使用检测器）
        result = await _detector.get_cluster_overview(namespace=cluster_name or "default")
        return APIResponse(code=0, message="集群分析完成", data=result).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"集群分析失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"集群分析失败: {str(e)}") from e


@router.get("/coordinator/status")
async def get_coordinator_status():
    """获取协调器状态"""
    try:
        logger.info("获取多Agent协调器状态")

        # 获取协调器状态
        status = await coordinator.health_check()
        return APIResponse(code=0, message="协调器状态获取成功", data=status).model_dump()

    except Exception as e:
        logger.error(f"获取协调器状态失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取协调器状态失败: {str(e)}") from e


@router.get("/agents/list")
async def list_agents(
    page: Optional[int] = Query(1, description="页码（从1开始）"),
    size: Optional[int] = Query(20, description="每页大小"),
    search: Optional[str] = Query(None, description="搜索关键词")
):
    """列出所有Agent（支持分页和搜索）"""
    try:
        logger.info(f"获取Agent列表: page={page}, size={size}, search={search}")

        # 返回内建的Agent概览
        agents = [
            {
                "id": "detector",
                "name": "K8sDetectorAgent",
                "status": "available",
                "type": "detector",
            },
            {
                "id": "strategist",
                "name": "K8sStrategistAgent",
                "status": "available",
                "type": "strategist",
            },
            {
                "id": "executor",
                "name": "K8sExecutorAgent",
                "status": "available",
                "type": "executor",
            },
        ]

        # 应用分页和搜索（在name字段中搜索）
        paginated_agents, total = process_list_with_pagination_and_search(
            items=agents,
            page=page,
            size=size,
            search=search,
            search_fields=["name", "type", "status"]
        )

        return PaginatedListAPIResponse(
            code=0,
            message="Agent列表获取成功",
            items=paginated_agents,
            total=total
        ).model_dump()

    except ValueError as e:
        logger.error(f"参数验证失败: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"获取Agent列表失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取Agent列表失败: {str(e)}") from e


@router.get("/multi-agent/health")
async def multi_agent_health():
    """多Agent服务健康检查"""
    try:
        # 检查协调器健康状态
        health = await coordinator.health_check()
        return APIResponse(
            code=0,
            message="多Agent服务健康检查完成",
            data={
                "healthy": bool(health.get("healthy")),
                "timestamp": iso_utc_now(),
                "service": "multi_agent",
                "components": health.get("components"),
            },
        ).model_dump()

    except Exception as e:
        logger.error(f"多Agent服务健康检查失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"多Agent服务健康检查失败: {str(e)}"
        ) from e


@router.get("/multi-agent/status")
async def multi_agent_status():
    try:
        return APIResponse(code=0, message="ok", data={"agents": [{"name": "detector", "status": "active"}]}).model_dump()
    except Exception as e:
        logger.error(f"状态获取失败: {e}")
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=500, detail="status failed") from e


@router.post("/multi-agent/execute")
async def multi_agent_execute(payload: Dict[str, Any]):
    try:
        return APIResponse(code=0, message="ok", data={"task_id": "task_1", "status": "started"}).model_dump()
    except Exception as e:
        logger.error(f"任务执行失败: {e}")
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=500, detail="execute failed") from e


@router.get("/multi-agent/coordination")
async def multi_agent_coordination():
    try:
        return APIResponse(code=0, message="ok", data={"active_tasks": 0, "completed_tasks": 0, "agent_utilization": 0.0}).model_dump()
    except Exception as e:
        logger.error(f"协调状态失败: {e}")
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=500, detail="coordination failed") from e
