#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 健康检查API模块，提供AI-CloudOps系统的服务健康监控和状态检查功能
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import psutil
from fastapi import APIRouter, HTTPException

from app.core.prediction.predictor import PredictionService
from app.models.response_models import APIResponse
from app.services.kubernetes import KubernetesService
from app.services.llm import LLMService
from app.services.notification import NotificationService
from app.services.prometheus import PrometheusService

logger = logging.getLogger("aiops.health")

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))

# 创建健康检查路由器
router = APIRouter(tags=["health"])

# 应用启动时间戳，用于计算系统运行时间（uptime）
start_time = time.time()

# 全局服务实例缓存，避免重复创建和初始化
_service_instances = {}


def get_service_instance(service_name: str, service_class):
    """
    获取服务实例的单例工厂方法，避免重复创建和健康检查

    Args:
        service_name (str): 服务名称
        service_class: 服务类

    Returns:
        服务实例
    """
    if service_name not in _service_instances:
        try:
            logger.debug(f"创建新的{service_name}服务实例")
            _service_instances[service_name] = service_class()
        except Exception as e:
            logger.warning(f"创建{service_name}服务实例失败: {str(e)}")
            return None
    return _service_instances[service_name]


@router.get("/health")
async def system_health():
    """系统综合健康检查"""
    try:
        current_time = datetime.now(BEIJING_TZ)
        uptime = time.time() - start_time
        components_status = check_components_health()
        system_status = get_system_status()
        is_healthy = all(components_status.values())

        health_data = {
            "status": "healthy" if is_healthy else "unhealthy",
            "timestamp": current_time.isoformat(),
            "uptime": round(uptime, 2),
            "version": "1.0.0",
            "components": components_status,
            "system": system_status,
        }

        return APIResponse(code=0, message="健康检查完成", data=health_data).model_dump()

    except Exception as e:
        logger.error(f"健康检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"健康检查失败: {str(e)}")


@router.get("/components/health")
async def components_health():
    """组件详细健康检查"""
    try:
        components_detail = {}
        
        # 检查各服务健康状态
        services = [
            ("prometheus", PrometheusService, "Prometheus监控服务", "负责收集和存储系统监控数据"),
            ("kubernetes", KubernetesService, "Kubernetes集群服务", "负责容器编排和集群资源管理"),
            ("llm", LLMService, "大语言模型服务", "负责AI推理和智能分析"),
            ("notification", NotificationService, "通知服务", "负责告警通知和消息推送"),
            ("prediction", PredictionService, "预测服务", "负责负载预测和容量规划")
        ]
        
        for service_name, service_class, display_name, description in services:
            service = get_service_instance(service_name, service_class)
            healthy = service.is_healthy() if service else False
            
            components_detail[service_name] = {
                "healthy": healthy,
                "name": display_name,
                "description": description,
                "last_check": datetime.now(BEIJING_TZ).isoformat()
            }
            
            if not healthy and service:
                try:
                    error_method = getattr(service, 'get_health_details', None) or \
                                   getattr(service, 'get_cluster_status', None) or \
                                   getattr(service, 'get_model_status', None) or \
                                   getattr(service, 'get_service_status', None)
                    
                    if error_method:
                        components_detail[service_name]["error"] = error_method()
                    else:
                        components_detail[service_name]["error"] = f"{service_name}服务异常"
                except Exception as e:
                    components_detail[service_name]["error"] = f"无法获取{service_name}状态: {str(e)}"

        return APIResponse(
            code=0,
            message="组件健康检查完成",
            data={
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
                "components": components_detail,
            },
        ).model_dump()

    except Exception as e:
        logger.error(f"组件健康检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"组件健康检查失败: {str(e)}")


@router.get("/metrics/health")
async def metrics_health():
    """系统健康指标"""
    try:
        system_metrics = get_system_status()
        process_metrics = get_process_metrics()

        health_metrics = {
            "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            "system": system_metrics,
            "process": process_metrics,
            "uptime": time.time() - start_time,
        }

        return APIResponse(code=0, message="系统健康指标获取成功", data=health_metrics).model_dump()

    except Exception as e:
        logger.error(f"获取健康指标失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取健康指标失败: {str(e)}")


@router.get("/readiness/health")
async def readiness_health():
    """就绪性探针"""
    try:
        components_status = check_components_health()
        critical_components = ["prometheus", "kubernetes", "llm"]
        
        critical_failed = [
            component for component in critical_components
            if component in components_status and not components_status[component]
        ]

        if len(critical_failed) == len(critical_components):
            return APIResponse(
                code=1,
                message="服务部分就绪，部分功能可能不可用",
                data={
                    "ready": True,
                    "timestamp": datetime.now(BEIJING_TZ).isoformat(),
                    "failed_components": critical_failed,
                    "warning": "部分关键组件不可用，相关功能可能受影响",
                },
            ).model_dump()

        return APIResponse(
            code=0,
            message="就绪性检查通过",
            data={"ready": True, "timestamp": datetime.now(BEIJING_TZ).isoformat()},
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"就绪性检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"就绪性检查失败: {str(e)}")


@router.get("/liveness/health")
async def liveness_health():
    """
    存活性探针
    """
    return APIResponse(
        code=0,
        message="存活性检查通过",
        data={"alive": True, "timestamp": datetime.now(BEIJING_TZ).isoformat()},
    ).model_dump()


def check_components_health():
    """检查各组件健康状态"""
    services = [
        ("prometheus", PrometheusService),
        ("kubernetes", KubernetesService),
        ("llm", LLMService),
        ("notification", NotificationService),
        ("prediction", PredictionService)
    ]
    
    components_status = {}
    for service_name, service_class in services:
        try:
            service = get_service_instance(service_name, service_class)
            components_status[service_name] = service.is_healthy() if service else False
        except Exception as e:
            logger.warning(f"{service_name}健康检查异常: {str(e)}")
            components_status[service_name] = False
    
    return components_status


def get_system_status():
    """获取系统资源状态"""
    try:
        # 获取CPU使用率
        cpu_percent = psutil.cpu_percent(interval=1)

        # 获取内存使用情况
        memory = psutil.virtual_memory()
        memory_percent = memory.percent

        # 获取磁盘使用情况
        disk = psutil.disk_usage("/")
        disk_percent = (disk.used / disk.total) * 100

        return {
            "cpu": {
                "usage_percent": round(cpu_percent, 2),
                "count": psutil.cpu_count(),
            },
            "memory": {
                "usage_percent": round(memory_percent, 2),
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
        }
    except Exception as e:
        logger.error(f"获取系统状态失败: {str(e)}")
        return {
            "cpu": {"usage_percent": 0, "count": 0},
            "memory": {
                "usage_percent": 0,
                "total_gb": 0,
                "used_gb": 0,
                "available_gb": 0,
            },
            "disk": {"usage_percent": 0, "total_gb": 0, "used_gb": 0, "free_gb": 0},
        }


def get_process_metrics():
    """获取当前进程的监控指标"""
    try:
        process = psutil.Process()
        return {
            "pid": process.pid,
            "cpu_percent": round(process.cpu_percent(), 2),
            "memory_mb": round(process.memory_info().rss / (1024**2), 2),
            "threads": process.num_threads(),
            "open_files": len(process.open_files()),
            "connections": len(process.connections()),
        }
    except Exception as e:
        logger.error(f"获取进程指标失败: {str(e)}")
        return {
            "pid": 0,
            "cpu_percent": 0,
            "memory_mb": 0,
            "threads": 0,
            "open_files": 0,
            "connections": 0,
        }
