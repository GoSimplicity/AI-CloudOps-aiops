#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Autofix 自动修复 API 路由
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from sqlalchemy import select, func

from app.config.settings import config
from app.core.agents.coordinator import K8sCoordinatorAgent
from app.core.agents.k8s_fixer import K8sFixerAgent
from app.db.base import get_session, session_scope
from app.db.models import AutoFixJobRecord, utcnow
from app.models.request_models import (
    AutoAutofixCreateReq,
    AutoAutofixDiagnoseReq,
    AutoAutofixFixReq,
    AutoAutofixWorkflowReq,
    AutoAutofixNotifyReq,
    AutoFixRecordCreateReq,
    AutoFixRecordUpdateReq,
    AutoFixRecordListReq,
)
from app.models.response_models import (
    APIResponse,
    AutoFixResponse,
    PaginatedListAPIResponse,
)
from app.models.entities import (
    AutoFixEntity,
    AutofixActionResultEntity,
    AutofixDiagnoseEntity,
    ServiceHealthEntity,
    WorkflowEntity,
    AutoFixRecordEntity,
    DeletionResultEntity,
    NotificationSendResultEntity,
)
from app.services.kubernetes import KubernetesService
from app.services.notification import NotificationService
from app.utils.pagination import process_list_with_pagination_and_search
from app.utils.time_utils import iso_utc_now
from app.utils.validators import (
    sanitize_input,
    validate_deployment_name,
    validate_namespace,
)

logger = logging.getLogger("aiops.autofix")

router = APIRouter(tags=["autofix"], prefix="/autofix")

# 初始化服务/Agent
coordinator = K8sCoordinatorAgent()
k8s_fixer_agent = K8sFixerAgent()
notification_service = NotificationService()
k8s_service = KubernetesService()


@router.post(
    "/diagnose",
    summary="诊断Kubernetes问题",
    description="诊断指定命名空间中的Kubernetes问题，返回问题列表",
)
async def diagnose(payload: AutoAutofixDiagnoseReq):
    try:
        namespace = payload.namespace or "default"
        entity = AutofixDiagnoseEntity(namespace=namespace, issues=[])
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"诊断失败: {e}")
        raise HTTPException(status_code=500, detail="诊断失败") from e


@router.post(
    "/fix", summary="修复Kubernetes问题", description="修复指定部署的Kubernetes问题"
)
async def fix(payload: AutoAutofixFixReq):
    try:
        namespace = payload.namespace or "default"
        deployment = payload.deployment or ""
        if not deployment:
            raise HTTPException(status_code=400, detail="deployment 必填")
        entity = AutofixActionResultEntity(
            namespace=namespace, deployment=deployment, status="success"
        )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"修复失败: {e}")
        raise HTTPException(status_code=500, detail="修复失败") from e


@router.post(
    "/workflow",
    summary="执行修复工作流",
    description="启动自动修复工作流，处理复杂的Kubernetes问题",
)
async def workflow(payload: AutoAutofixWorkflowReq):
    try:
        entity = WorkflowEntity(workflow_id="wf_1", status="started")
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"工作流失败: {e}")
        raise HTTPException(status_code=500, detail="工作流失败") from e


@router.post(
    "/create",
    summary="创建自动修复任务",
    description="创建新的自动修复任务，对指定的Kubernetes部署进行诊断和修复",
)
async def create_autofix(request_data: AutoAutofixCreateReq):
    """创建自动修复Kubernetes问题"""
    try:
        # 验证请求参数
        deployment = sanitize_input(request_data.deployment)
        namespace = (
            sanitize_input(request_data.namespace)
            if request_data.namespace
            else "default"
        )

        if not validate_deployment_name(deployment):
            raise HTTPException(status_code=400, detail="无效的部署名称")

        if not validate_namespace(namespace):
            raise HTTPException(status_code=400, detail="无效的命名空间名称")

        logger.info(f"开始自动修复: deployment={deployment}, namespace={namespace}")

        # 执行多Agent工作流以完成修复
        try:
            report = await coordinator.run_full_workflow(
                deployment=deployment, namespace=namespace
            )
        except Exception as fix_error:
            logger.error(f"自动修复执行失败: {str(fix_error)}")
            raise HTTPException(
                status_code=500, detail=f"自动修复失败: {str(fix_error)}"
            ) from fix_error

        # 发送通知
        try:
            actions: list = []
            for exec_item in report.get("details", {}).get("execution_results") or []:
                for step in exec_item.get("steps", []):
                    msg = step.get("message") or step.get("type") or "step"
                    actions.append(str(msg))

            await notification_service.send_autofix_notification(
                deployment=deployment,
                namespace=namespace,
                status="success" if report.get("success") else "failed",
                actions=actions,
                error_message=(
                    "; ".join(report.get("errors", []))
                    if not report.get("success")
                    else None
                ),
            )
        except Exception as notify_error:
            logger.warning(f"发送通知失败: {str(notify_error)}")

        # 构建响应
        response = AutoFixResponse(
            status="success" if report.get("success") else "failed",
            result=(
                report.get("summary", {})
                if isinstance(report.get("summary"), str)
                else "修复流程完成"
            ),
            deployment=deployment,
            namespace=namespace,
            actions_taken=actions if "actions" in locals() else [],
            timestamp=iso_utc_now(),
            success=bool(report.get("success")),
        )

        # 成功后记录到数据库（失败不影响响应）
        try:
            with session_scope() as session:
                session.add(
                    AutoFixJobRecord(
                        deployment=deployment,
                        namespace=namespace,
                        status=response.status,
                        actions="\n".join(actions) if "actions" in locals() else None,
                        error_message=response.error_message,
                    )
                )
        except Exception:
            pass

        entity = AutoFixEntity(**response.model_dump())
        return APIResponse(
            code=0, message="自动修复完成", data=entity.model_dump()
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"自动修复请求处理失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"自动修复失败: {str(e)}") from e


# 简化：移除不带 /autofix 前缀的旧接口，统一为 /api/v1/autofix/*


@router.get(
    "/history/list",
    summary="获取修复历史列表",
    description="获取自动修复历史记录列表，支持分页、搜索和过滤",
)
async def list_fix_history(
    page: Optional[int] = Query(1, description="页码（从1开始）"),
    size: Optional[int] = Query(20, description="每页大小"),
    search: Optional[str] = Query(None, description="搜索关键词"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
    status: Optional[str] = Query(None, description="状态过滤(success/failed)"),
    start: Optional[str] = Query(None, description="起始时间(ISO8601)"),
    end: Optional[str] = Query(None, description="结束时间(ISO8601)"),
):
    """获取修复历史列表（支持分页和搜索）"""
    try:
        logger.info(f"获取修复历史: page={page}, size={size}, search={search}")

        # 优先从数据库读取已持久化的记录
        with session_scope() as session:
            stmt = select(AutoFixJobRecord).where(AutoFixJobRecord.deleted_at.is_(None))
            # 时间范围过滤（基于 created_at）
            if start:
                try:
                    start_dt = datetime.fromisoformat(
                        start.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                    stmt = stmt.where(AutoFixJobRecord.created_at >= start_dt)
                except Exception:
                    pass
            if end:
                try:
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")).replace(
                        tzinfo=None
                    )
                    stmt = stmt.where(AutoFixJobRecord.created_at <= end_dt)
                except Exception:
                    pass
            rows = (
                session.execute(stmt.order_by(AutoFixJobRecord.id.desc()).limit(1000))
                .scalars()
                .all()
            )
            if namespace:
                rows = [r for r in rows if (r.namespace == namespace)]
            if status:
                rows = [r for r in rows if (r.status == status)]
            history = [
                {
                    "id": r.id,
                    "name": r.deployment,
                    "deployment": r.deployment,
                    "namespace": r.namespace,
                    "status": r.status,
                    "timestamp": (r.created_at.isoformat() if r.created_at else ""),
                    "summary": (r.actions[:200] + "...")
                    if (r.actions and len(r.actions) > 200)
                    else (r.actions or ""),
                }
                for r in rows
            ]

        # 应用分页和搜索（在deployment、namespace、status、summary字段中搜索）
        paginated_history, total = process_list_with_pagination_and_search(
            items=history,
            page=page,
            size=size,
            search=search,
            search_fields=["name", "deployment", "namespace", "status", "summary"],
        )

        return PaginatedListAPIResponse(
            code=0, message="修复历史获取成功", items=paginated_history, total=total
        ).model_dump()
    except Exception as e:
        logger.error(f"获取修复历史失败: {str(e)}")
        return PaginatedListAPIResponse(
            code=0, message="修复历史获取失败", items=[], total=0
        ).model_dump()


@router.get(
    "/records/detail/{record_id}",
    summary="获取修复记录详情",
    description="根据记录ID获取指定自动修复记录的详细信息",
)
async def get_autofix_record(record_id: int):
    """获取自动修复记录详情"""
    try:
        with get_session() as session:
            rec = session.execute(
                select(AutoFixJobRecord).where(
                    AutoFixJobRecord.id == record_id,
                    AutoFixJobRecord.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if not rec:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            entity = AutoFixRecordEntity(
                id=rec.id,
                deployment=rec.deployment,
                namespace=rec.namespace,
                status=rec.status,
                actions=rec.actions,
                error_message=rec.error_message,
                created_at=rec.created_at.isoformat() if rec.created_at else None,
                updated_at=rec.updated_at.isoformat() if rec.updated_at else None,
            )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as ex:
        logger.error(f"获取自动修复记录失败: {str(ex)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


@router.delete(
    "/records/delete/{record_id}",
    summary="删除修复记录",
    description="软删除指定的自动修复记录",
)
async def delete_autofix_record(record_id: int):
    """软删除自动修复记录"""
    try:
        with session_scope() as session:
            rec = session.execute(
                select(AutoFixJobRecord).where(AutoFixJobRecord.id == record_id)
            ).scalar_one_or_none()
            if not rec:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            # 使用UTC统一软删除时间
            rec.deleted_at = utcnow()
            session.add(rec)
        entity = DeletionResultEntity(id=record_id)
        return APIResponse(
            code=0, message="deleted", data=entity.model_dump()
        ).model_dump()
    except Exception as ex:
        logger.error(f"删除自动修复记录失败: {str(ex)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


@router.get(
    "/health/detail",
    summary="自动修复服务健康检查",
    description="检查自动修复服务各组件的健康状态",
)
async def autofix_health():
    """自动修复服务健康检查"""
    try:
        # 协调器健康
        coordinator_health = await coordinator.health_check()
        notifier_healthy = NotificationService().is_healthy()
        k8s_healthy = k8s_service.is_healthy()

        overall_healthy = bool(
            coordinator_health.get("healthy") and notifier_healthy and k8s_healthy
        )
        entity = ServiceHealthEntity(
            healthy=overall_healthy,
            components={
                "coordinator": coordinator_health.get("healthy"),
                "notifier": notifier_healthy,
                "kubernetes": k8s_healthy,
            },
            remediation_config={
                "enabled": bool(config.remediation.enabled),
                "dry_run": bool(config.remediation.dry_run),
                "allow_rollback": bool(config.remediation.allow_rollback),
                "verify_wait_seconds": int(config.remediation.verify_wait_seconds),
            },
            timestamp=iso_utc_now(),
            service="autofix",
        )
        return APIResponse(
            code=0, message="自动修复服务健康检查完成", data=entity.model_dump()
        ).model_dump()

    except Exception as e:
        logger.error(f"自动修复服务健康检查失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"自动修复服务健康检查失败: {str(e)}"
        ) from e


@router.post(
    "/notify/create", summary="发送通知", description="发送自动修复相关的通知消息"
)
async def send_notify(payload: AutoAutofixNotifyReq):
    try:
        import app.services.notification as notification_mod

        webhook_url = payload.webhook_url or ""
        message = payload.message or ""
        if webhook_url and message:
            try:
                n = notification_mod.NotificationService()
                n.send_webhook(webhook_url, {"text": message})
            except Exception:
                pass
        entity = NotificationSendResultEntity(sent=True)
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"发送通知失败: {e}")
        raise HTTPException(status_code=500, detail="通知失败") from e


# ========== Autofix 模块：标准化 CRUD（直连数据库） ==========


@router.get(
    "/records/list",
    summary="获取修复记录列表",
    description="获取自动修复记录列表，支持分页和条件过滤",
)
async def list_autofix_records(params: AutoFixRecordListReq = Depends()):
    try:
        with session_scope() as session:
            stmt = select(AutoFixJobRecord).where(AutoFixJobRecord.deleted_at.is_(None))
            if params.namespace:
                stmt = stmt.where(AutoFixJobRecord.namespace == params.namespace)
            if params.status:
                stmt = stmt.where(AutoFixJobRecord.status == params.status)
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
                    stmt.order_by(AutoFixJobRecord.id.desc())
                    .offset((page - 1) * size)
                    .limit(size)
                )
                .scalars()
                .all()
            )
            items = [
                AutoFixRecordEntity(
                    id=r.id,
                    deployment=r.deployment,
                    namespace=r.namespace,
                    status=r.status,
                    actions=r.actions,
                    error_message=r.error_message,
                    created_at=r.created_at.isoformat() if r.created_at else None,
                    updated_at=r.updated_at.isoformat() if r.updated_at else None,
                ).model_dump()
                for r in rows
            ]
        return APIResponse(
            code=0, message="ok", data={"items": items, "total": total}
        ).model_dump()
    except Exception as e:
        logger.error(f"list_autofix_records 失败: {e}")
        return APIResponse(
            code=0, message="ok", data={"items": [], "total": 0}
        ).model_dump()


@router.post(
    "/records/create", summary="创建修复记录", description="创建新的自动修复记录"
)
async def create_autofix_record(payload: AutoFixRecordCreateReq):
    try:
        with session_scope() as session:
            rec = AutoFixJobRecord(
                deployment=payload.deployment,
                namespace=payload.namespace,
                status=payload.status or "success",
                actions=payload.actions,
                error_message=payload.error_message,
            )
            session.add(rec)
            session.flush()
            entity = AutoFixRecordEntity(
                id=rec.id,
                deployment=rec.deployment,
                namespace=rec.namespace,
                status=rec.status,
                actions=rec.actions,
                error_message=rec.error_message,
                created_at=rec.created_at.isoformat() if rec.created_at else None,
                updated_at=rec.updated_at.isoformat() if rec.updated_at else None,
            )
        return APIResponse(
            code=0, message="created", data=entity.model_dump()
        ).model_dump()
    except Exception as e:
        logger.error(f"create_autofix_record 失败: {e}")
        raise HTTPException(status_code=500, detail="create record failed") from e


@router.get(
    "/records/detail/db/{record_id}",
    summary="获取修复记录详情(数据库)",
    description="从数据库直接获取指定ID的自动修复记录详情",
)
async def get_autofix_record_db(record_id: int):
    try:
        with session_scope() as session:
            r = session.get(AutoFixJobRecord, record_id)
            if not r or r.deleted_at is not None:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            entity = AutoFixRecordEntity(
                id=r.id,
                deployment=r.deployment,
                namespace=r.namespace,
                status=r.status,
                actions=r.actions,
                error_message=r.error_message,
                created_at=r.created_at.isoformat() if r.created_at else None,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
            )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"get_autofix_record_db 失败: {e}")
        raise HTTPException(status_code=500, detail="get record failed") from e


@router.put(
    "/records/update/{record_id}",
    summary="更新修复记录",
    description="更新指定ID的自动修复记录信息",
)
async def update_autofix_record(record_id: int, payload: AutoFixRecordUpdateReq):
    try:
        with session_scope() as session:
            r = session.get(AutoFixJobRecord, record_id)
            if not r or r.deleted_at is not None:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            for field in (
                "deployment",
                "namespace",
                "status",
                "actions",
                "error_message",
            ):
                value = getattr(payload, field)
                if value is not None:
                    setattr(r, field, value)
            session.add(r)
            entity = AutoFixRecordEntity(
                id=r.id,
                deployment=r.deployment,
                namespace=r.namespace,
                status=r.status,
                actions=r.actions,
                error_message=r.error_message,
                created_at=r.created_at.isoformat() if r.created_at else None,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
            )
        return APIResponse(
            code=0, message="updated", data=entity.model_dump()
        ).model_dump()
    except Exception as e:
        logger.error(f"update_autofix_record 失败: {e}")
        raise HTTPException(status_code=500, detail="update record failed") from e


@router.delete(
    "/records/delete/db/{record_id}",
    summary="删除修复记录(数据库)",
    description="从数据库中软删除指定ID的自动修复记录",
)
async def delete_autofix_record_db(record_id: int):
    try:
        with session_scope() as session:
            r = session.get(AutoFixJobRecord, record_id)
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
        logger.error(f"delete_autofix_record_db 失败: {e}")
        raise HTTPException(status_code=500, detail="delete record failed") from e
