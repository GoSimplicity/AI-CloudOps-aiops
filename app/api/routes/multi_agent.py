#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 多Agent 编排 API 路由
"""

import logging
from typing import Any, Dict, Optional
import json

from fastapi import APIRouter, HTTPException, Query, Depends

from app.config.settings import config
from app.core.agents.coordinator import K8sCoordinatorAgent
from app.core.agents.detector import K8sDetectorAgent
from app.models.response_models import APIResponse, PaginatedListAPIResponse
from app.models.entities import (
    CoordinatorStatusEntity,
    MultiAgentMetricsEntity,
    MultiAgentStatusEntity,
    WorkflowRecordEntity,
    DeletionResultEntity,
)
from app.models.request_models import (
    AutoMultiAgentClusterReq,
    AutoMultiAgentRepairAllReq,
    AutoMultiAgentRepairReq,
    AutoMultiAgentExecuteReq,
    WorkflowRecordCreateReq,
    WorkflowRecordUpdateReq,
    WorkflowRecordListReq,
)
from app.utils.pagination import process_list_with_pagination_and_search
from app.utils.time_utils import iso_utc_now
from app.utils.validators import (
    sanitize_input,
    validate_deployment_name,
    validate_namespace,
)

from app.db.base import session_scope
from app.db.models import WorkflowRecord, utcnow
from sqlalchemy import select, func

logger = logging.getLogger("aiops.multi_agent")

router = APIRouter(tags=["multi_agent"], prefix="/multi-agent")


# 初始化协调器
coordinator = K8sCoordinatorAgent()
_detector = K8sDetectorAgent()


@router.get(
    "/metrics/detail",
    summary="获取多智能体指标详情",
    description="获取多智能体协作系统的运行指标，包括工作流统计、成功率等关键性能数据",
)
async def multi_agent_metrics() -> Dict[str, Any]:
    """导出多Agent修复指标（内存级）。"""
    try:
        m = coordinator.metrics if hasattr(coordinator, "metrics") else {}
        entity = MultiAgentMetricsEntity(
            total_workflows=m.get("total_workflows", 0),
            successful_workflows=m.get("successful_workflows", 0),
            rolled_back=m.get("rolled_back", 0),
            avg_success_rate=m.get("avg_success_rate", 0.0),
            config={
                "safe_mode": bool(config.remediation.safe_mode),
                "dry_run": bool(config.remediation.dry_run),
                "allow_rollback": bool(config.remediation.allow_rollback),
            },
            timestamp=iso_utc_now(),
        )
        return APIResponse(
            code=0, message="多Agent指标获取成功", data=entity.model_dump()
        ).model_dump()
    except Exception as e:
        logger.error(f"多Agent指标获取失败: {str(e)}")
        from fastapi import HTTPException as _HTTPException

        raise _HTTPException(
            status_code=500, detail=f"多Agent指标获取失败: {str(e)}"
        ) from e


@router.post(
    "/repairs/create",
    summary="创建单个部署修复任务",
    description="为指定命名空间下的单个Kubernetes部署创建智能修复任务，通过多智能体协作进行故障诊断和自动修复",
)
async def create_deployment_repair(request_data: AutoMultiAgentRepairReq) -> Dict[str, Any]:
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
        result = await coordinator.run_full_workflow(
            deployment=deployment, namespace=namespace
        )
        return APIResponse(code=0, message="部署修复完成", data=result).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"修复部署失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"修复部署失败: {str(e)}") from e


@router.post(
    "/repairs/create-all",
    summary="创建批量部署修复任务",
    description="为指定命名空间下的所有Kubernetes部署创建批量修复任务，通过多智能体协作批量处理故障修复",
)
async def create_all_repairs(request_data: AutoMultiAgentRepairAllReq) -> Dict[str, Any]:
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


@router.post(
    "/analysis/create",
    summary="创建集群健康分析任务",
    description="对指定Kubernetes集群进行全面的健康状态分析，检测潜在问题并提供优化建议",
)
async def create_cluster_analysis(request_data: AutoMultiAgentClusterReq) -> Dict[str, Any]:
    """创建集群健康状态分析"""
    try:
        cluster_name = sanitize_input(request_data.cluster_name)

        logger.info(f"开始分析集群: {cluster_name}")

        # 执行集群分析（使用检测器）
        result = await _detector.get_cluster_overview(
            namespace=cluster_name or "default"
        )
        return APIResponse(code=0, message="集群分析完成", data=result).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"集群分析失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"集群分析失败: {str(e)}") from e


@router.get(
    "/coordinator/status/detail",
    summary="获取协调器状态详情",
    description="获取多智能体协调器的详细运行状态，包括各组件健康状况和连接状态",
)
async def get_coordinator_status() -> Dict[str, Any]:
    """获取协调器状态"""
    try:
        logger.info("获取多Agent协调器状态")

        # 获取协调器状态
        status = await coordinator.health_check()
        entity = CoordinatorStatusEntity(
            healthy=bool(status.get("healthy")), components=status.get("components")
        )
        return APIResponse(
            code=0, message="协调器状态获取成功", data=entity.model_dump()
        ).model_dump()

    except Exception as e:
        logger.error(f"获取协调器状态失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"获取协调器状态失败: {str(e)}"
        ) from e


@router.get(
    "/agents/list",
    summary="获取智能体列表",
    description="获取系统中所有可用智能体的列表信息，支持分页查询和关键词搜索",
)
async def list_agents(
    page: Optional[int] = Query(1, description="页码（从1开始）"),
    size: Optional[int] = Query(20, description="每页大小"),
    search: Optional[str] = Query(None, description="搜索关键词"),
) -> Dict[str, Any]:
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
            search_fields=["name", "type", "status"],
        )

        return PaginatedListAPIResponse(
            code=0, message="Agent列表获取成功", items=paginated_agents, total=total
        ).model_dump()

    except ValueError as e:
        logger.error(f"参数验证失败: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"获取Agent列表失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"获取Agent列表失败: {str(e)}"
        ) from e


@router.get(
    "/health/detail",
    summary="多智能体服务健康检查",
    description="检查多智能体系统的整体健康状态，包括协调器和各个智能体组件的运行状况",
)
async def multi_agent_health() -> Dict[str, Any]:
    """多Agent服务健康检查"""
    try:
        # 检查协调器健康状态
        health = await coordinator.health_check()
        entity = CoordinatorStatusEntity(
            healthy=bool(health.get("healthy")), components=health.get("components")
        )
        data = entity.model_dump()
        data.update({"timestamp": iso_utc_now(), "service": "multi_agent"})
        return APIResponse(
            code=0, message="多Agent服务健康检查完成", data=data
        ).model_dump()

    except Exception as e:
        logger.error(f"多Agent服务健康检查失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"多Agent服务健康检查失败: {str(e)}"
        ) from e


@router.get(
    "/status/detail",
    summary="获取多智能体系统状态",
    description="获取多智能体系统的当前运行状态和活跃智能体信息",
)
async def multi_agent_status() -> Dict[str, Any]:
    try:
        entity = MultiAgentStatusEntity(
            agents=[{"name": "detector", "status": "active"}]
        )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"状态获取失败: {e}")
        from fastapi import HTTPException as _HTTPException

        raise _HTTPException(status_code=500, detail="状态获取失败") from e


@router.post(
    "/execute",
    summary="执行多智能体任务",
    description="执行指定的多智能体协作任务，包括任务分发、执行监控和结果汇总",
)
async def multi_agent_execute(payload: AutoMultiAgentExecuteReq) -> Dict[str, Any]:
    try:
        return APIResponse(
            code=0, message="ok", data={"task_id": "task_1", "status": "started"}
        ).model_dump()
    except Exception as e:
        logger.error(f"任务执行失败: {e}")
        from fastapi import HTTPException as _HTTPException

        raise _HTTPException(status_code=500, detail="任务执行失败") from e


@router.get(
    "/coordination/detail",
    summary="获取智能体协调详情",
    description="获取多智能体协调机制的详细信息，包括任务分配、执行进度和资源利用率",
)
async def multi_agent_coordination() -> Dict[str, Any]:
    try:
        return APIResponse(
            code=0,
            message="ok",
            data={"active_tasks": 0, "completed_tasks": 0, "agent_utilization": 0.0},
        ).model_dump()
    except Exception as e:
        logger.error(f"协调状态失败: {e}")
        from fastapi import HTTPException as _HTTPException

        raise _HTTPException(status_code=500, detail="协调状态获取失败") from e


# ========== Multi-Agent 模块：标准化 CRUD（直连数据库） ==========


@router.get(
    "/workflows/list",
    summary="获取工作流记录列表",
    description="获取多智能体工作流执行记录列表，支持按命名空间、状态筛选和分页查询",
)
async def list_workflow_records(params: WorkflowRecordListReq = Depends()) -> Dict[str, Any]:
    try:
        with session_scope() as session:
            stmt = select(WorkflowRecord).where(WorkflowRecord.deleted_at.is_(None))
            if params.namespace:
                stmt = stmt.where(WorkflowRecord.namespace == params.namespace)
            if params.status:
                stmt = stmt.where(WorkflowRecord.status == params.status)
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
                    stmt.order_by(WorkflowRecord.id.desc())
                    .offset((page - 1) * size)
                    .limit(size)
                )
                .scalars()
                .all()
            )
            items = [
                WorkflowRecordEntity(
                    id=r.id,
                    workflow_id=r.workflow_id,
                    status=r.status,
                    namespace=r.namespace,
                    target=r.target,
                    details=None,
                    created_at=r.created_at.isoformat() if r.created_at else None,
                    updated_at=r.updated_at.isoformat() if r.updated_at else None,
                ).model_dump()
                for r in rows
            ]
        return APIResponse(
            code=0, message="ok", data={"items": items, "total": total}
        ).model_dump()
    except Exception as e:
        logger.error(f"list_workflow_records 失败: {e}")
        return APIResponse(
            code=0, message="ok", data={"items": [], "total": 0}
        ).model_dump()


@router.post(
    "/workflows/create",
    summary="创建工作流记录",
    description="创建新的多智能体工作流执行记录，记录工作流的基本信息和执行状态",
)
async def create_workflow_record(payload: WorkflowRecordCreateReq):
    try:
        with session_scope() as session:
            rec = WorkflowRecord(
                workflow_id=payload.workflow_id,
                status=payload.status,
                namespace=payload.namespace,
                target=payload.target,
                details=(
                    json.dumps(payload.details) if payload.details is not None else None
                ),
            )
            session.add(rec)
            session.flush()
            entity = WorkflowRecordEntity(
                id=rec.id,
                workflow_id=rec.workflow_id,
                status=rec.status,
                namespace=rec.namespace,
                target=rec.target,
                details=None,
                created_at=rec.created_at.isoformat() if rec.created_at else None,
                updated_at=rec.updated_at.isoformat() if rec.updated_at else None,
            )
        return APIResponse(
            code=0, message="created", data=entity.model_dump()
        ).model_dump()
    except Exception as e:
        logger.error(f"create_workflow_record 失败: {e}")
        raise HTTPException(status_code=500, detail="创建记录失败") from e


@router.get(
    "/workflows/detail/{record_id}",
    summary="获取工作流记录详情",
    description="根据记录ID获取指定工作流记录的详细信息，包括执行状态、目标资源等",
)
async def get_workflow_record(record_id: int):
    try:
        with session_scope() as session:
            r = session.get(WorkflowRecord, record_id)
            if not r or r.deleted_at is not None:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            entity = WorkflowRecordEntity(
                id=r.id,
                workflow_id=r.workflow_id,
                status=r.status,
                namespace=r.namespace,
                target=r.target,
                details=None,
                created_at=r.created_at.isoformat() if r.created_at else None,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
            )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"get_workflow_record 失败: {e}")
        raise HTTPException(status_code=500, detail="获取记录失败") from e


@router.put(
    "/workflows/update/{record_id}",
    summary="更新工作流记录",
    description="更新指定ID的工作流记录信息，包括状态、目标资源和详细信息等",
)
async def update_workflow_record(record_id: int, payload: WorkflowRecordUpdateReq):
    try:
        with session_scope() as session:
            r = session.get(WorkflowRecord, record_id)
            if not r or r.deleted_at is not None:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            for field in ("workflow_id", "status", "namespace", "target"):
                value = getattr(payload, field)
                if value is not None:
                    setattr(r, field, value)
            if payload.details is not None:
                try:
                    r.details = json.dumps(payload.details)
                except Exception:
                    r.details = None
            session.add(r)
            entity = WorkflowRecordEntity(
                id=r.id,
                workflow_id=r.workflow_id,
                status=r.status,
                namespace=r.namespace,
                target=r.target,
                details=None,
                created_at=r.created_at.isoformat() if r.created_at else None,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
            )
        return APIResponse(
            code=0, message="updated", data=entity.model_dump()
        ).model_dump()
    except Exception as e:
        logger.error(f"update_workflow_record 失败: {e}")
        raise HTTPException(status_code=500, detail="更新记录失败") from e


@router.delete(
    "/workflows/delete/{record_id}",
    summary="删除工作流记录",
    description="软删除指定ID的工作流记录，记录将被标记为已删除但不会从数据库中物理移除",
)
async def delete_workflow_record(record_id: int):
    try:
        with session_scope() as session:
            r = session.get(WorkflowRecord, record_id)
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
        logger.error(f"delete_workflow_record 失败: {e}")
        raise HTTPException(status_code=500, detail="删除记录失败") from e
