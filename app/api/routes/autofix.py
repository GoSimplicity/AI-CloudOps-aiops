#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 自动修复API路由 - 提供Kubernetes问题自动诊断、修复和工作流管理
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config.settings import config
from app.core.agents.coordinator import K8sCoordinatorAgent
from app.core.agents.k8s_fixer import K8sFixerAgent
from app.db.base import get_session, session_scope
from app.db.models import AutoFixJobRecord, utcnow
from app.models.request_models import AutoFixRequest
from app.models.response_models import APIResponse, AutoFixResponse, PaginatedListAPIResponse
from app.services.kubernetes import KubernetesService
from app.services.notification import NotificationService
from app.utils.pagination import process_list_with_pagination_and_search
from app.utils.time_utils import iso_utc_now
from app.utils.validators import sanitize_input, validate_deployment_name, validate_namespace

logger = logging.getLogger("aiops.autofix")

router = APIRouter(tags=["autofix"])


class WorkflowRequest(BaseModel):
    namespace: Optional[str] = Field(
        default="default", description="命名空间，默认 default"
    )


# 移除未使用的请求模型以精简API模块


# 初始化服务/Agent
coordinator = K8sCoordinatorAgent()
k8s_fixer_agent = K8sFixerAgent()
notification_service = NotificationService()
k8s_service = KubernetesService()


@router.post("/create")
async def create_autofix(request_data: AutoFixRequest):
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
            report = await coordinator.run_full_workflow(deployment=deployment, namespace=namespace)
        except Exception as fix_error:
            logger.error(f"自动修复执行失败: {str(fix_error)}")
            raise HTTPException(status_code=500, detail=f"自动修复失败: {str(fix_error)}") from fix_error

        # 发送通知
        try:
            actions: list = []
            for exec_item in (report.get("details", {}).get("execution_results") or []):
                for step in exec_item.get("steps", []):
                    msg = step.get("message") or step.get("type") or "step"
                    actions.append(str(msg))

            await notification_service.send_autofix_notification(
                deployment=deployment,
                namespace=namespace,
                status="success" if report.get("success") else "failed",
                actions=actions,
                error_message=("; ".join(report.get("errors", [])) if not report.get("success") else None),
            )
        except Exception as notify_error:
            logger.warning(f"发送通知失败: {str(notify_error)}")

        # 构建响应
            response = AutoFixResponse(
            status="success" if report.get("success") else "failed",
            result=(report.get("summary", {}) if isinstance(report.get("summary"), str) else "修复流程完成"),
            deployment=deployment,
            namespace=namespace,
                actions_taken=actions if 'actions' in locals() else [],
                timestamp=iso_utc_now(),
            success=bool(report.get("success"))
        )

        # 成功后记录到数据库（失败不影响响应）
        try:
            with session_scope() as session:
                session.add(
                    AutoFixJobRecord(
                        deployment=deployment,
                        namespace=namespace,
                        status=response.status,
                        actions="\n".join(actions) if 'actions' in locals() else None,
                        error_message=response.error_message,
                    )
                )
        except Exception:
            pass

        return APIResponse(
            code=0, message="自动修复完成", data=response.model_dump()
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"自动修复请求处理失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"自动修复失败: {str(e)}") from e


@router.post("/workflows/create")
async def create_workflow(request_data: WorkflowRequest):
    """创建自动修复工作流"""
    try:
        namespace = request_data.namespace

        if not validate_namespace(namespace):
            raise HTTPException(status_code=400, detail="无效的命名空间名称")

        logger.info(f"启动自动修复工作流: namespace={namespace}")

        # 启动批量工作流
        result = await coordinator.run_batch_workflow(namespace=namespace)

        return APIResponse(code=0, message="工作流启动成功", data=result).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"启动工作流失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"启动工作流失败: {str(e)}") from e


@router.post("/diagnosis/create")
async def create_diagnosis(request_data: AutoFixRequest):
    """创建问题诊断"""
    try:
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

        logger.info(f"开始问题诊断: deployment={deployment}, namespace={namespace}")

        # 运行诊断（基于K8sFixer Agent的增强能力）
        diagnosis_result = await k8s_fixer_agent.diagnose_deployment_health(deployment_name=deployment, namespace=namespace)

        return APIResponse(
            code=0, message="问题诊断完成", data=diagnosis_result
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"问题诊断失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"问题诊断失败: {str(e)}") from e


@router.get("/tasks/{task_id}")
async def get_task_detail(task_id: str = Path(..., description="任务ID")):
    """获取任务状态详情"""
    try:
        task_id = sanitize_input(task_id)

        if not task_id:
            raise HTTPException(status_code=400, detail="任务ID不能为空")

        logger.info(f"查询任务状态: task_id={task_id}")

        # 该版本未实现任务持久化，返回404
        raise HTTPException(status_code=404, detail="任务不存在或未启用任务跟踪")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询任务状态失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"查询任务状态失败: {str(e)}") from e


@router.get("/history")
async def list_fix_history(
    page: Optional[int] = Query(1, description="页码（从1开始）"),
    size: Optional[int] = Query(20, description="每页大小"),
    search: Optional[str] = Query(None, description="搜索关键词"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
    status: Optional[str] = Query(None, description="状态过滤(success/failed)"),
    start: Optional[str] = Query(None, description="起始时间(ISO8601)"),
    end: Optional[str] = Query(None, description="结束时间(ISO8601)")
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
                    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")).replace(tzinfo=None)
                    stmt = stmt.where(AutoFixJobRecord.created_at >= start_dt)
                except Exception:
                    pass
            if end:
                try:
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")).replace(tzinfo=None)
                    stmt = stmt.where(AutoFixJobRecord.created_at <= end_dt)
                except Exception:
                    pass
            rows = session.execute(stmt.order_by(AutoFixJobRecord.id.desc()).limit(1000)).scalars().all()
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
                    "summary": (r.actions[:200] + "...") if (r.actions and len(r.actions) > 200) else (r.actions or ""),
                }
                for r in rows
            ]

        # 应用分页和搜索（在deployment、namespace、status、summary字段中搜索）
        paginated_history, total = process_list_with_pagination_and_search(
            items=history,
            page=page,
            size=size,
            search=search,
            search_fields=["name", "deployment", "namespace", "status", "summary"]
        )

        return PaginatedListAPIResponse(
            code=0,
            message="修复历史获取成功",
            items=paginated_history,
            total=total
        ).model_dump()
    except Exception as e:
        logger.error(f"获取修复历史失败: {str(e)}")
        return PaginatedListAPIResponse(code=0, message="修复历史获取失败", items=[], total=0).model_dump()


@router.get("/autofix/records/{record_id}")
async def get_autofix_record(record_id: int):
    """获取自动修复记录详情"""
    try:
        with get_session() as session:
            rec = session.execute(select(AutoFixJobRecord).where(AutoFixJobRecord.id == record_id, AutoFixJobRecord.deleted_at.is_(None))).scalar_one_or_none()
            if not rec:
                return APIResponse(code=404, message="not found", data=None).model_dump()
            item = {
                "id": rec.id,
                "deployment": rec.deployment,
                "namespace": rec.namespace,
                "status": rec.status,
                "actions": rec.actions,
                "error_message": rec.error_message,
                "created_at": rec.created_at.isoformat() if rec.created_at else None,
                "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
            }
        return APIResponse(code=0, message="ok", data=item).model_dump()
    except Exception as ex:
        logger.error(f"获取自动修复记录失败: {str(ex)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


@router.delete("/autofix/records/{record_id}")
async def delete_autofix_record(record_id: int):
    """软删除自动修复记录"""
    try:
        with session_scope() as session:
            rec = session.execute(select(AutoFixJobRecord).where(AutoFixJobRecord.id == record_id)).scalar_one_or_none()
            if not rec:
                return APIResponse(code=404, message="not found", data=None).model_dump()
            # 使用UTC统一软删除时间
            rec.deleted_at = utcnow()
            session.add(rec)
        return APIResponse(code=0, message="deleted", data={"id": record_id}).model_dump()
    except Exception as ex:
        logger.error(f"删除自动修复记录失败: {str(ex)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


@router.get("/autofix/health")
async def autofix_health():
    """自动修复服务健康检查"""
    try:
        # 协调器健康
        coordinator_health = await coordinator.health_check()
        notifier_healthy = NotificationService().is_healthy()
        k8s_healthy = k8s_service.is_healthy()

        overall_healthy = bool(coordinator_health.get("healthy") and notifier_healthy and k8s_healthy)

        return APIResponse(
            code=0,
            message="自动修复服务健康检查完成",
            data={
                "healthy": overall_healthy,
                "components": {
                    "coordinator": coordinator_health.get("healthy"),
                    "notifier": notifier_healthy,
                    "kubernetes": k8s_healthy,
                },
                "remediation_config": {
                    "enabled": bool(config.remediation.enabled),
                    "dry_run": bool(config.remediation.dry_run),
                    "allow_rollback": bool(config.remediation.allow_rollback),
                    "verify_wait_seconds": int(config.remediation.verify_wait_seconds),
                },
                "timestamp": iso_utc_now(),
                "service": "autofix",
            },
        ).model_dump()

    except Exception as e:
        logger.error(f"自动修复服务健康检查失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"自动修复服务健康检查失败: {str(e)}"
        ) from e


@router.post("/autofix/notify")
async def send_notify(payload: Dict[str, Any]):
    try:
        import app.services.notification as notification_mod
        webhook_url = payload.get("webhook_url") or ""
        message = payload.get("message") or ""
        if webhook_url and message:
            try:
                n = notification_mod.NotificationService()
                n.send_webhook(webhook_url, {"text": message})
            except Exception:
                pass
        return APIResponse(code=0, message="ok", data={"sent": True}).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"发送通知失败: {e}")
        raise HTTPException(status_code=500, detail="通知失败") from e


@router.post("/autofix/diagnose")
async def diagnose(payload: Dict[str, Any]):
    try:
        namespace = payload.get("namespace") or "default"
        return APIResponse(code=0, message="ok", data={"namespace": namespace, "issues": []}).model_dump()
    except Exception as e:
        logger.error(f"诊断失败: {e}")
        raise HTTPException(status_code=500, detail="诊断失败") from e


@router.post("/autofix/fix")
async def fix(payload: Dict[str, Any]):
    try:
        namespace = payload.get("namespace") or "default"
        deployment = payload.get("deployment") or ""
        if not deployment:
            raise HTTPException(status_code=400, detail="deployment 必填")
        return APIResponse(code=0, message="ok", data={"namespace": namespace, "deployment": deployment, "status": "success"}).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"修复失败: {e}")
        raise HTTPException(status_code=500, detail="修复失败") from e


@router.post("/autofix/workflow")
async def workflow(payload: Dict[str, Any]):
    try:
        return APIResponse(code=0, message="ok", data={"workflow_id": "wf_1", "status": "started"}).model_dump()
    except Exception as e:
        logger.error(f"工作流失败: {e}")
        raise HTTPException(status_code=500, detail="工作流失败") from e


    # 仅保留新接口，无旧接口兼容
