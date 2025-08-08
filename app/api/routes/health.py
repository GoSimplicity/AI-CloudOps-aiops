#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 健康检查API模块，提供AI-CloudOps系统的服务健康监控和状态检查功能
"""

from fastapi import APIRouter, HTTPException
from datetime import datetime
import time
import psutil
import logging
from app.services.prometheus import PrometheusService
from app.services.kubernetes import KubernetesService
from app.services.llm import LLMService
from app.services.notification import NotificationService
from app.core.prediction.predictor import PredictionService
from app.models.response_models import APIResponse

logger = logging.getLogger("aiops.health")

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
async def health_check():
    """
    系统综合健康检查API - 主要的健康状态检查接口
    """

    try:
        # 获取当前UTC时间，确保时间戳的一致性
        current_time = datetime.utcnow()
        # 计算系统运行时间（从应用启动到现在的秒数）
        uptime = time.time() - start_time

        # 检查各组件健康状态，这是核心的健康评估步骤
        components_status = check_components_health()

        # 获取系统资源状态，包括CPU、内存、磁盘使用情况
        system_status = get_system_status()

        # 判断整体健康状态 - 只有当所有组件都健康时，系统才被认为是健康的
        is_healthy = all(components_status.values())

        # 构建健康检查响应数据
        health_data = {
            "status": "healthy" if is_healthy else "unhealthy",  # 整体状态
            "timestamp": current_time.isoformat(),  # ISO格式的时间戳
            "uptime": round(uptime, 2),  # 运行时间，保留2位小数
            "version": "1.0.0",  # 系统版本号
            "components": components_status,  # 各组件详细状态
            "system": system_status,  # 系统资源状态
        }

        # 返回标准化的API响应格式
        return APIResponse(code=0, message="健康检查完成", data=health_data).model_dump()

    except Exception as e:
        # 异常处理：记录错误日志并返回500错误响应
        logger.error(f"健康检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"健康检查失败: {str(e)}")


@router.get("/health/components")
async def components_health():
    """
    组件详细健康检查API - 深度组件状态分析接口

    检查的组件包括：
    1. Prometheus服务 - 监控数据收集和存储
    2. Kubernetes服务 - 容器编排和集群管理
    3. LLM服务 - 大语言模型和AI推理
    4. 通知服务 - 告警和消息推送
    5. 预测服务 - 负载预测和容量规划
    """

    try:
        # 初始化组件详细信息字典
        components_detail = {}

        # Prometheus服务健康检查 - 监控数据收集服务
        prometheus_service = get_service_instance("prometheus", PrometheusService)
        prometheus_healthy = (
            prometheus_service.is_healthy() if prometheus_service else False
        )
        components_detail["prometheus"] = {
            "healthy": prometheus_healthy,  # 健康状态
            "name": "Prometheus监控服务",  # 组件名称
            "description": "负责收集和存储系统监控数据",  # 组件描述
            "last_check": datetime.utcnow().isoformat(),  # 最后检查时间
            "endpoint": prometheus_service.get_endpoint()
            if prometheus_service and hasattr(prometheus_service, "get_endpoint")
            else "unknown",
        }

        # 如果Prometheus不健康，添加错误详情
        if not prometheus_healthy:
            try:
                # 尝试获取具体的错误信息
                error_details = (
                    prometheus_service.get_health_details()
                    if prometheus_service
                    and hasattr(prometheus_service, "get_health_details")
                    else "连接失败"
                )
                components_detail["prometheus"]["error"] = error_details
            except Exception as detail_error:
                components_detail["prometheus"]["error"] = (
                    f"无法获取错误详情: {str(detail_error)}"
                )

        # Kubernetes服务健康检查 - 容器编排服务
        k8s_service = get_service_instance("kubernetes", KubernetesService)
        k8s_healthy = k8s_service.is_healthy() if k8s_service else False
        components_detail["kubernetes"] = {
            "healthy": k8s_healthy,
            "name": "Kubernetes集群服务",
            "description": "负责容器编排和集群资源管理",
            "last_check": datetime.utcnow().isoformat(),
        }

        if not k8s_healthy:
            try:
                k8s_status = (
                    k8s_service.get_cluster_status()
                    if k8s_service and hasattr(k8s_service, "get_cluster_status")
                    else "集群连接异常"
                )
                components_detail["kubernetes"]["error"] = k8s_status
            except Exception as k8s_error:
                components_detail["kubernetes"]["error"] = (
                    f"无法获取K8s状态: {str(k8s_error)}"
                )

        # LLM服务健康检查 - AI推理服务 (使用缓存实例避免重复检查)
        llm_service = get_service_instance("llm", LLMService)
        llm_healthy = llm_service.is_healthy() if llm_service else False
        components_detail["llm"] = {
            "healthy": llm_healthy,
            "name": "大语言模型服务",
            "description": "负责AI推理和智能分析",
            "last_check": datetime.utcnow().isoformat(),
        }

        if not llm_healthy:
            try:
                llm_status = (
                    llm_service.get_model_status()
                    if llm_service and hasattr(llm_service, "get_model_status")
                    else "模型服务异常"
                )
                components_detail["llm"]["error"] = llm_status
            except Exception as llm_error:
                components_detail["llm"]["error"] = f"无法获取LLM状态: {str(llm_error)}"

        # 通知服务健康检查 - 告警推送服务
        notification_service = get_service_instance("notification", NotificationService)
        notification_healthy = (
            notification_service.is_healthy() if notification_service else False
        )
        components_detail["notification"] = {
            "healthy": notification_healthy,
            "name": "通知服务",
            "description": "负责告警通知和消息推送",
            "last_check": datetime.utcnow().isoformat(),
        }

        if not notification_healthy:
            try:
                notification_status = (
                    notification_service.get_service_status()
                    if notification_service
                    and hasattr(notification_service, "get_service_status")
                    else "通知服务异常"
                )
                components_detail["notification"]["error"] = notification_status
            except Exception as notification_error:
                components_detail["notification"]["error"] = (
                    f"无法获取通知服务状态: {str(notification_error)}"
                )

        # 预测服务健康检查 - 负载预测服务
        prediction_service = get_service_instance("prediction", PredictionService)
        prediction_healthy = (
            prediction_service.is_healthy() if prediction_service else False
        )
        components_detail["prediction"] = {
            "healthy": prediction_healthy,
            "name": "预测服务",
            "description": "负责负载预测和容量规划",
            "last_check": datetime.utcnow().isoformat(),
        }

        if not prediction_healthy:
            try:
                prediction_status = (
                    prediction_service.get_model_status()
                    if prediction_service
                    and hasattr(prediction_service, "get_model_status")
                    else "预测模型异常"
                )
                components_detail["prediction"]["error"] = prediction_status
            except Exception as prediction_error:
                components_detail["prediction"]["error"] = (
                    f"无法获取预测服务状态: {str(prediction_error)}"
                )

        # 返回组件详细健康检查结果
        return APIResponse(
            code=0,
            message="组件健康检查完成",
            data={
                "timestamp": datetime.utcnow().isoformat(),  # 检查时间戳
                "components": components_detail,  # 所有组件的详细状态
            },
        ).model_dump()

    except Exception as e:
        # 异常处理：记录错误日志并返回500错误响应
        logger.error(f"组件健康检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"组件健康检查失败: {str(e)}")


@router.get("/health/metrics")
async def health_metrics():
    """
    系统健康指标API - 详细的系统资源监控接口
    """

    try:
        # 获取系统资源使用情况
        system_metrics = get_system_status()

        # 获取进程特定的监控指标
        process_metrics = get_process_metrics()

        # 合并所有健康指标
        health_metrics = {
            "timestamp": datetime.utcnow().isoformat(),
            "system": system_metrics,
            "process": process_metrics,
            "uptime": time.time() - start_time,
        }

        # 返回健康指标数据
        return APIResponse(
            code=0, message="系统健康指标获取成功", data=health_metrics
        ).model_dump()

    except Exception as e:
        # 异常处理：记录错误日志并返回500错误响应
        logger.error(f"获取健康指标失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取健康指标失败: {str(e)}")


@router.get("/health/ready")
async def readiness_probe():
    """
    Kubernetes就绪性探针API - 服务就绪状态检查接口
    """

    try:
        # 检查关键组件是否就绪
        components_status = check_components_health()

        # 定义关键组件列表（服务就绪必须的组件）
        critical_components = ["prometheus", "kubernetes", "llm"]

        # 检查关键组件状态，但只检查真正必要的组件
        critical_failed = []
        for component in critical_components:
            if component in components_status and not components_status[component]:
                critical_failed.append(component)
                logger.warning(f"关键组件 {component} 不可用")

        # 如果所有关键组件都失败，只是警告而不阻塞启动
        if len(critical_failed) == len(critical_components):
            logger.warning("所有关键组件都不可用，但系统将继续运行（功能受限）")
            return APIResponse(
                code=1,
                message="服务部分就绪，部分功能可能不可用",
                data={
                    "ready": True,  # 仍然标记为就绪，但功能受限
                    "timestamp": datetime.utcnow().isoformat(),
                    "failed_components": critical_failed,
                    "warning": "部分关键组件不可用，相关功能可能受影响",
                },
            ).model_dump()

        # 所有关键组件都就绪
        return APIResponse(
            code=0,
            message="就绪性检查通过",
            data={"ready": True, "timestamp": datetime.utcnow().isoformat()},
        ).model_dump()

    except HTTPException:
        # 重新抛出HTTP异常
        raise
    except Exception as e:
        # 异常处理：记录错误日志并返回500错误响应
        logger.error(f"就绪性检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"就绪性检查失败: {str(e)}")


@router.get("/health/live")
async def liveness_probe():
    """
    存活性探针
    """
    return APIResponse(
        code=0,
        message="存活性检查通过",
        data={"alive": True, "timestamp": datetime.utcnow().isoformat()},
    ).model_dump()


def check_components_health():
    """检查各组件健康状态 (使用缓存实例避免重复创建)"""
    components_status = {}

    try:
        # Prometheus服务检查 (使用缓存实例)
        prometheus_service = get_service_instance("prometheus", PrometheusService)
        components_status["prometheus"] = (
            prometheus_service.is_healthy() if prometheus_service else False
        )
    except Exception as e:
        logger.warning(f"Prometheus健康检查异常: {str(e)}")
        components_status["prometheus"] = False

    try:
        # Kubernetes服务检查 (使用缓存实例)
        k8s_service = get_service_instance("kubernetes", KubernetesService)
        components_status["kubernetes"] = (
            k8s_service.is_healthy() if k8s_service else False
        )
    except Exception as e:
        logger.warning(f"Kubernetes健康检查异常: {str(e)}")
        components_status["kubernetes"] = False

    try:
        # LLM服务检查 (使用缓存实例避免重复初始化和健康检查)
        llm_service = get_service_instance("llm", LLMService)
        components_status["llm"] = llm_service.is_healthy() if llm_service else False
    except Exception as e:
        logger.warning(f"LLM健康检查异常: {str(e)}")
        components_status["llm"] = False

    try:
        # 通知服务检查 (使用缓存实例)
        notification_service = get_service_instance("notification", NotificationService)
        components_status["notification"] = (
            notification_service.is_healthy() if notification_service else False
        )
    except Exception as e:
        logger.warning(f"通知服务健康检查异常: {str(e)}")
        components_status["notification"] = False

    try:
        # 预测服务检查 (使用缓存实例)
        prediction_service = get_service_instance("prediction", PredictionService)
        components_status["prediction"] = (
            prediction_service.is_healthy() if prediction_service else False
        )
    except Exception as e:
        logger.warning(f"预测服务健康检查异常: {str(e)}")
        components_status["prediction"] = False

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
