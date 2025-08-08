#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
多Agent修复API路由
Author: AI Assistant
License: Apache 2.0
Description: 提供多Agent协作的K8s修复API接口
"""

from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
import asyncio
import logging
from app.core.agents.coordinator import K8sCoordinatorAgent
from app.models.response_models import APIResponse
from app.utils.validators import validate_deployment_name, validate_namespace, sanitize_input
from pydantic import BaseModel
from typing import Optional

logger = logging.getLogger("aiops.multi_agent")

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

@router.post('/multi-agent/repair')
async def repair_deployment(request_data: RepairRequest):
    """修复单个部署"""
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
            coordinator.repair_deployment,
            deployment=deployment,
            namespace=namespace
        )
        
        return APIResponse(
            code=0,
            message="部署修复完成",
            data=result
        ).dict()
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"修复部署失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"修复部署失败: {str(e)}")

@router.post('/multi-agent/repair-all')
async def repair_all_deployments(request_data: RepairAllRequest):
    """修复命名空间下所有部署"""
    try:
        namespace = sanitize_input(request_data.namespace)
        
        if not validate_namespace(namespace):
            raise HTTPException(status_code=400, detail="无效的命名空间名称")

        logger.info(f"开始修复命名空间 {namespace} 下的所有部署")
        
        # 执行批量修复
        result = await asyncio.to_thread(
            coordinator.repair_all_deployments,
            namespace=namespace
        )
        
        return APIResponse(
            code=0,
            message="批量修复完成",
            data=result
        ).dict()
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"批量修复失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"批量修复失败: {str(e)}")

@router.post('/multi-agent/analyze')
async def analyze_cluster(request_data: ClusterRequest):
    """分析集群健康状态"""
    try:
        cluster_name = sanitize_input(request_data.cluster_name)

        logger.info(f"开始分析集群: {cluster_name}")
        
        # 执行集群分析
        result = await asyncio.to_thread(
            coordinator.analyze_cluster,
            cluster_name=cluster_name
        )
        
        return APIResponse(
            code=0,
            message="集群分析完成",
            data=result
        ).dict()
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"集群分析失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"集群分析失败: {str(e)}")

@router.get('/multi-agent/status')
async def get_coordinator_status():
    """获取协调器状态"""
    try:
        logger.info("获取多Agent协调器状态")
        
        # 获取协调器状态
        status = await asyncio.to_thread(coordinator.get_status)
        
        return APIResponse(
            code=0,
            message="协调器状态获取成功",
            data=status
        ).dict()
        
    except Exception as e:
        logger.error(f"获取协调器状态失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取协调器状态失败: {str(e)}")

@router.get('/multi-agent/agents')
async def list_agents():
    """列出所有Agent"""
    try:
        logger.info("获取Agent列表")
        
        # 获取Agent列表
        agents = await asyncio.to_thread(coordinator.list_agents)
        
        return APIResponse(
            code=0,
            message="Agent列表获取成功",
            data={
                "agents": agents,
                "count": len(agents),
                "timestamp": datetime.utcnow().isoformat()
            }
        ).dict()
        
    except Exception as e:
        logger.error(f"获取Agent列表失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取Agent列表失败: {str(e)}")

@router.get('/multi-agent/health')
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
                "timestamp": datetime.utcnow().isoformat(),
                "service": "multi_agent"
            }
        ).dict()
        
    except Exception as e:
        logger.error(f"多Agent服务健康检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"多Agent服务健康检查失败: {str(e)}")