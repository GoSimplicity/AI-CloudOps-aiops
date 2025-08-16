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
from fastapi import APIRouter, HTTPException, status, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.prediction.predictor import PredictionService
from app.db.base import get_engine
from app.di import get_service
from app.models.response_models import APIResponse
from app.models.entities import (
    HealthEntity,
    HealthSnapshotRecordEntity,
    DeletionResultEntity,
)
from app.models.request_models import (
    HealthSnapshotCreateReq,
    HealthSnapshotUpdateReq,
    HealthSnapshotListReq,
)
from app.db.base import session_scope
from app.db.models import HealthSnapshotRecord, utcnow
from sqlalchemy import select, func
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

# 统一的服务配置，避免重复定义
HEALTH_CHECK_SERVICES = [
    (
        "prometheus",
        PrometheusService,
        "Prometheus监控服务",
        "负责收集和存储系统监控数据",
    ),
    (
        "kubernetes",
        KubernetesService,
        "Kubernetes集群服务",
        "负责容器编排和集群资源管理",
    ),
    ("llm", LLMService, "大语言模型服务", "负责AI推理和智能分析"),
    ("notification", NotificationService, "通知服务", "负责告警通知和消息推送"),
    ("prediction", PredictionService, "预测服务", "负责负载预测和容量规划"),
]


def get_service_instance(service_name: str, service_class):
    """通过全局DI容器获取单例服务实例。"""
    try:
        return get_service(service_class)
    except Exception as e:
        logger.warning(f"获取{service_name}服务实例失败: {str(e)}")
        return None


@router.get("/health")
async def system_health_root() -> Dict[str, Any]:
    """基础健康检查（向后兼容的简化路径）。"""
    return await system_health()


@router.get("/health/detail")
async def system_health() -> Dict[str, Any]:
    """系统综合健康检查"""
    try:
        # 返回数据统一使用 iso_utc_now()
        uptime = time.time() - start_time
        components_status = check_components_health()
        # 附加数据库健康探针（不影响总体健康判断）
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            components_status["database"] = True
        except Exception:
            components_status["database"] = False
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


@router.get("/health/detailed")
async def system_health_detailed() -> Dict[str, Any]:
    """详细健康检查（兼容测试所需路径）。"""
    return await system_health()


@router.get("/health/k8s")
async def k8s_health() -> JSONResponse:
    try:
        svc = get_service_instance("kubernetes", KubernetesService)
        ok = bool(svc and svc.check_connectivity())
        payload = APIResponse(
            code=0 if ok else 503,
            message="ok" if ok else "unavailable",
            data={"healthy": ok},
        ).model_dump()
        return JSONResponse(
            content=payload,
            status_code=(
                status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
        )
    except Exception:
        payload = APIResponse(
            code=503, message="unavailable", data={"healthy": False}
        ).model_dump()
        return JSONResponse(
            content=payload, status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        )


@router.get("/health/prometheus")
async def prometheus_health() -> JSONResponse:
    try:
        svc = get_service_instance("prometheus", PrometheusService)
        ok = bool(svc and svc.check_connectivity())
        payload = APIResponse(
            code=0 if ok else 503,
            message="ok" if ok else "unavailable",
            data={"healthy": ok},
        ).model_dump()
        return JSONResponse(
            content=payload,
            status_code=(
                status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
        )
    except Exception:
        payload = APIResponse(
            code=503, message="unavailable", data={"healthy": False}
        ).model_dump()
        return JSONResponse(
            content=payload, status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        )


@router.get("/health/system")
async def system_resources() -> Dict[str, Any]:
    """系统资源健康（兼容测试返回扁平字段）。"""
    try:
        cpu_percent = psutil.cpu_percent(interval=0)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        data = {
            "cpu_percent": round(cpu_percent, 2),
            "memory_percent": round(mem.percent, 2),
            "disk_usage": {
                "total": disk.total,
                "used": disk.used,
                "free": disk.free,
                "percent": round((disk.used / disk.total) * 100, 2)
                if disk.total
                else 0.0,
            },
        }
        return APIResponse(code=0, message="ok", data=data).model_dump()
    except Exception:
        return APIResponse(code=500, message="error", data={}).model_dump()


def check_components_health() -> Dict[str, bool]:
    """检查各组件健康状态"""
    components_status = {}
    for service_name, service_class, _, _ in HEALTH_CHECK_SERVICES:
        try:
            service = get_service_instance(service_name, service_class)
            components_status[service_name] = service.is_healthy() if service else False
        except Exception as e:
            logger.warning(f"{service_name}健康检查异常: {str(e)}")
            components_status[service_name] = False

    return components_status


def get_system_status() -> Dict[str, Any]:
    """获取系统资源状态（非阻塞）"""
    try:
        # 获取CPU使用率（instantaneous，避免1秒阻塞）
        cpu_percent = psutil.cpu_percent(interval=0)

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


# ========== Health 模块：标准化 CRUD（直连数据库） ==========


@router.get("/health/records")
async def list_health_records(params: HealthSnapshotListReq = Depends()):
    try:
        with session_scope() as session:
            stmt = select(HealthSnapshotRecord).where(
                HealthSnapshotRecord.deleted_at.is_(None)
            )
            if params.status:
                stmt = stmt.where(HealthSnapshotRecord.status == params.status)
            total = (
                session.execute(
                    select(func.count()).select_from(stmt.subquery())
                ).scalar()
                or 0
            )
            page = max(1, int(params.page or 1))
            size = max(1, min(100, int(params.size or 20)))
            rows = (
                session.execute(
                    stmt.order_by(HealthSnapshotRecord.id.desc())
                    .offset((page - 1) * size)
                    .limit(size)
                )
                .scalars()
                .all()
            )
            items = [
                HealthSnapshotRecordEntity(
                    id=r.id,
                    status=r.status,
                    components=None,
                    system=None,
                    version=r.version,
                    uptime=r.uptime,
                    created_at=r.created_at.isoformat() if r.created_at else None,
                    updated_at=r.updated_at.isoformat() if r.updated_at else None,
                ).model_dump()
                for r in rows
            ]
        return APIResponse(
            code=0, message="ok", data={"items": items, "total": total}
        ).model_dump()
    except Exception:
        return APIResponse(
            code=0, message="ok", data={"items": [], "total": 0}
        ).model_dump()


@router.post("/health/records")
async def create_health_record(payload: HealthSnapshotCreateReq):
    try:
        with session_scope() as session:
            rec = HealthSnapshotRecord(
                status=payload.status,
                components=(str(payload.components) if payload.components else None),
                system=(str(payload.system) if payload.system else None),
                version=payload.version,
                uptime=payload.uptime,
            )
            session.add(rec)
            session.flush()
            entity = HealthSnapshotRecordEntity(
                id=rec.id,
                status=rec.status,
                components=None,
                system=None,
                version=rec.version,
                uptime=rec.uptime,
                created_at=rec.created_at.isoformat() if rec.created_at else None,
                updated_at=rec.updated_at.isoformat() if rec.updated_at else None,
            )
        return APIResponse(
            code=0, message="created", data=entity.model_dump()
        ).model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail="创建记录失败") from e


@router.get("/health/records/{record_id}")
async def get_health_record(record_id: int):
    try:
        with session_scope() as session:
            r = session.get(HealthSnapshotRecord, record_id)
            if not r or r.deleted_at is not None:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            entity = HealthSnapshotRecordEntity(
                id=r.id,
                status=r.status,
                components=None,
                system=None,
                version=r.version,
                uptime=r.uptime,
                created_at=r.created_at.isoformat() if r.created_at else None,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
            )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail="获取记录失败") from e


@router.put("/health/records/{record_id}")
async def update_health_record(record_id: int, payload: HealthSnapshotUpdateReq):
    try:
        with session_scope() as session:
            r = session.get(HealthSnapshotRecord, record_id)
            if not r or r.deleted_at is not None:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            for field in ("status", "version", "uptime"):
                value = getattr(payload, field)
                if value is not None:
                    setattr(r, field, value)
            if payload.components is not None:
                r.components = str(payload.components)
            if payload.system is not None:
                r.system = str(payload.system)
            session.add(r)
            entity = HealthSnapshotRecordEntity(
                id=r.id,
                status=r.status,
                components=None,
                system=None,
                version=r.version,
                uptime=r.uptime,
                created_at=r.created_at.isoformat() if r.created_at else None,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
            )
        return APIResponse(
            code=0, message="updated", data=entity.model_dump()
        ).model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail="更新记录失败") from e


@router.delete("/health/records/{record_id}")
async def delete_health_record(record_id: int):
    try:
        with session_scope() as session:
            r = session.get(HealthSnapshotRecord, record_id)
            if not r:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            r.deleted_at = utcnow()
            session.add(r)
        entity = DeletionResultEntity(id=record_id)
        return APIResponse(
            code=0, message="deleted", data=entity.model_dump()
        ).model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail="删除记录失败") from e


def get_process_metrics():
    """获取当前进程的监控指标（非阻塞）"""
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
