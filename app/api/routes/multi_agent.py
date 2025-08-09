#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
多Agent修复API路由
Author: AI Assistant
License: Apache 2.0
Description: 提供多Agent协作的K8s修复API接口
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.agents.coordinator import K8sCoordinatorAgent
from app.models.response_models import APIResponse, PaginatedListAPIResponse
from app.utils.pagination import process_list_with_pagination_and_search
from app.utils.validators import (sanitize_input, validate_deployment_name,
                                  validate_namespace)

logger = logging.getLogger("aiops.multi_agent")

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))

router = APIRouter(tags=["multi_agent"])


class RepairRequest(BaseModel):
    deployment: str
    namespace: Optional[str] = "default"


class RepairAllRequest(BaseModel):
    namespace: Optional[str] = "default"


class ClusterRequest(BaseModel):
    cluster_name: Optional[str] = "default"


# 初始化协调器
coordinator = K8sCoordinatorAgent()


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

        # 执行修复
        result = await asyncio.to_thread(
            coordinator.repair_deployment, deployment=deployment, namespace=namespace
        )

        return APIResponse(code=0, message="部署修复完成", data=result).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"修复部署失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"修复部署失败: {str(e)}")


@router.post("/repairs/create-all")
async def create_all_repairs(request_data: RepairAllRequest):
    """创建命名空间下所有部署修复"""
    try:
        namespace = sanitize_input(request_data.namespace)

        if not validate_namespace(namespace):
            raise HTTPException(status_code=400, detail="无效的命名空间名称")

        logger.info(f"开始修复命名空间 {namespace} 下的所有部署")

        # 执行批量修复
        result = await asyncio.to_thread(
            coordinator.repair_all_deployments, namespace=namespace
        )

        return APIResponse(code=0, message="批量修复完成", data=result).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"批量修复失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"批量修复失败: {str(e)}")


@router.post("/analysis/create")
async def create_cluster_analysis(request_data: ClusterRequest):
    """创建集群健康状态分析"""
    try:
        cluster_name = sanitize_input(request_data.cluster_name)

        logger.info(f"开始分析集群: {cluster_name}")

        # 执行集群分析
        result = await asyncio.to_thread(
            coordinator.analyze_cluster, cluster_name=cluster_name
        )

        return APIResponse(code=0, message="集群分析完成", data=result).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"集群分析失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"集群分析失败: {str(e)}")


@router.get("/coordinator/status")
async def get_coordinator_status():
    """获取协调器状态"""
    try:
        logger.info("获取多Agent协调器状态")

        # 获取协调器状态
        status = await asyncio.to_thread(coordinator.get_status)

        return APIResponse(
            code=0, message="协调器状态获取成功", data=status
        ).model_dump()

    except Exception as e:
        logger.error(f"获取协调器状态失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取协调器状态失败: {str(e)}")


@router.get("/agents/list")
async def list_agents(
    page: Optional[int] = 1,
    size: Optional[int] = 20,
    search: Optional[str] = None
):
    """列出所有Agent（支持分页和搜索）"""
    try:
        logger.info(f"获取Agent列表: page={page}, size={size}, search={search}")

        # 获取所有Agent列表
        agents = await asyncio.to_thread(coordinator.list_agents)
        
        # 确保agents是字典列表格式，如果不是则转换
        if agents and not isinstance(agents[0], dict):
            # 如果agents是其他格式，尝试转换为字典
            agents_dict = []
            for i, agent in enumerate(agents):
                if hasattr(agent, '__dict__'):
                    agent_dict = agent.__dict__
                elif hasattr(agent, 'model_dump'):
                    agent_dict = agent.model_dump()
                else:
                    # 简单转换
                    agent_dict = {
                        "id": getattr(agent, 'id', f"agent_{i}"),
                        "name": getattr(agent, 'name', f"Agent {i+1}"),
                        "status": getattr(agent, 'status', 'unknown'),
                        "type": getattr(agent, 'type', 'unknown')
                    }
                agents_dict.append(agent_dict)
            agents = agents_dict

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
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"获取Agent列表失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取Agent列表失败: {str(e)}")


@router.get("/multi-agent/health")
async def multi_agent_health():
    """多Agent服务健康检查"""
    try:
        # 检查协调器健康状态
        health_status = await asyncio.to_thread(coordinator.is_healthy)

        return APIResponse(
            code=0,
            message="多Agent服务健康检查完成",
            data={
                "healthy": health_status,
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
                "service": "multi_agent",
            },
        ).model_dump()

    except Exception as e:
        logger.error(f"多Agent服务健康检查失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"多Agent服务健康检查失败: {str(e)}"
        )
