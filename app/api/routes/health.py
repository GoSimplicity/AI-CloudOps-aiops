#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 健康检查 API 路由
"""

import logging
import time
from typing import Any, Dict

import psutil
from fastapi import APIRouter, HTTPException

from app.core.prediction.predictor import PredictionService
from app.core.agents.coordinator import K8sCoordinatorAgent
from app.di import get_service
from app.models.response_models import APIResponse
from app.models.entities import HealthEntity
from app.services.kubernetes import KubernetesService
from app.services.llm import LLMService
from app.services.notification import NotificationService
from app.services.prometheus import PrometheusService
from app.utils.time_utils import iso_utc_now

logger = logging.getLogger("aiops.health")

# 创建健康检查路由器
router = APIRouter(tags=["health"])

# 应用启动时间戳，用于计算系统运行时间（uptime）
start_time = time.time()

# 统一的服务配置
HEALTH_CHECK_SERVICES = [
    ("prometheus", PrometheusService),
    ("kubernetes", KubernetesService),
    ("llm", LLMService),
    ("notification", NotificationService),
    ("prediction", PredictionService),
]


def get_service_instance(service_name: str, service_class):
    """通过全局DI容器获取单例服务实例。"""
    try:
        return get_service(service_class)
    except Exception as e:
        logger.warning(f"获取{service_name}服务实例失败: {str(e)}")
        return None


@router.get("/health")
async def system_health() -> Dict[str, Any]:
    """系统综合健康检查"""
    try:
        uptime = time.time() - start_time
        components_status = await check_components_health()
        system_status = get_system_status()
        is_healthy = all(components_status.values())

        entity = HealthEntity(
            status="healthy" if is_healthy else "unhealthy",
            timestamp=iso_utc_now(),
            uptime=round(uptime, 2),
            version="1.0.0",
            components=components_status,
            system=system_status,
        )

        return APIResponse(
            code=0, message="健康检查完成", data=entity.model_dump()
        ).model_dump()

    except Exception as e:
        logger.error(f"健康检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"健康检查失败: {str(e)}") from e


async def check_components_health() -> Dict[str, bool]:
    """检查各组件健康状态"""
    components_status = {}
    
    # 检查基础服务
    for service_name, service_class in HEALTH_CHECK_SERVICES:
        try:
            service = get_service_instance(service_name, service_class)
            if service:
                if hasattr(service, 'health_check') and callable(getattr(service, 'health_check')):
                    health_result = await service.health_check()
                    components_status[service_name] = health_result.get('healthy', False) if isinstance(health_result, dict) else bool(health_result)
                elif hasattr(service, 'is_healthy') and callable(getattr(service, 'is_healthy')):
                    components_status[service_name] = service.is_healthy()
                else:
                    components_status[service_name] = True
            else:
                components_status[service_name] = False
        except Exception as e:
            logger.warning(f"{service_name}健康检查异常: {str(e)}")
            components_status[service_name] = False

    # 检查协调器组件
    try:
        coordinator = K8sCoordinatorAgent()
        coordinator_health = await coordinator.health_check()
        components_status["coordinator"] = coordinator_health.get("healthy", False)
        
        if "components" in coordinator_health:
            for comp_name, comp_status in coordinator_health["components"].items():
                components_status[f"coordinator_{comp_name}"] = comp_status
                
    except Exception as e:
        logger.warning(f"协调器健康检查异常: {str(e)}")
        components_status["coordinator"] = False

    return components_status


def get_system_status() -> Dict[str, Any]:
    """获取系统资源状态"""
    try:
        cpu_percent = psutil.cpu_percent(interval=0)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        disk_percent = (disk.used / disk.total) * 100
        
        # 获取进程指标
        process = psutil.Process()
        
        # 安全地获取网络连接数和打开文件数
        try:
            connections_count = len(process.net_connections())
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            connections_count = 0
            
        try:
            open_files_count = len(process.open_files())
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            open_files_count = 0

        return {
            "cpu": {
                "usage_percent": round(cpu_percent, 2),
                "count": psutil.cpu_count(),
            },
            "memory": {
                "usage_percent": round(memory.percent, 2),
                "total_gb": round(memory.total / (1024**3), 2),
                "used_gb": round(memory.used / (1024**3), 2),
                "available_gb": round(memory.available / (1024**3), 2),
            },
            "disk": {
                "usage_percent": round(disk_percent, 2),
                "total_gb": round(disk.total / (1024**3), 2),
                "used_gb": round(disk.used / (1024**3), 2),
                "free_gb": round(disk.free / (1024**3), 2),
            },
            "process": {
                "pid": process.pid,
                "cpu_percent": round(process.cpu_percent(), 2),
                "memory_mb": round(process.memory_info().rss / (1024**2), 2),
                "threads": process.num_threads(),
                "open_files": open_files_count,
                "connections": connections_count,
            }
        }
    except Exception as e:
        logger.error(f"获取系统状态失败: {str(e)}")
        return {
            "cpu": {"usage_percent": 0, "count": 0},
            "memory": {"usage_percent": 0, "total_gb": 0, "used_gb": 0, "available_gb": 0},
            "disk": {"usage_percent": 0, "total_gb": 0, "used_gb": 0, "free_gb": 0},
            "process": {"pid": 0, "cpu_percent": 0, "memory_mb": 0, "threads": 0, "open_files": 0, "connections": 0}
        }
