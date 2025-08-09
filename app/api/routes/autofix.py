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
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.core.agents.coordinator import K8sCoordinatorAgent
from app.core.agents.k8s_fixer import K8sFixerAgent
from app.models.request_models import AutoFixRequest
from app.models.response_models import (APIResponse, AutoFixResponse,
                                        PaginatedListAPIResponse)
from app.services.kubernetes import KubernetesService
from app.services.notification import NotificationService
from app.utils.pagination import process_list_with_pagination_and_search
from app.utils.validators import (sanitize_input, validate_deployment_name,
                                  validate_namespace)

logger = logging.getLogger("aiops.autofix")

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))

router = APIRouter(tags=["autofix"])


class WorkflowRequest(BaseModel):
    namespace: Optional[str] = Field(
        default="default", description="命名空间，默认 default"
    )


class StatusRequest(BaseModel):
    task_id: str = Field(..., description="任务ID")


class WorkflowStartRequest(BaseModel):
    deployment: Optional[str] = Field(default=None, description="目标部署名称（可选）")
    namespace: Optional[str] = Field(default="default", description="命名空间")
    problem_description: Optional[str] = Field(default=None, description="问题描述（可选）")


class NotificationRequest(BaseModel):
    title: str = Field(..., description="通知标题")
    message: str = Field(..., description="通知内容")
    level: Optional[str] = Field(default="info", description="级别: info/warning/error")
    timestamp: Optional[str] = Field(default=None, description="时间戳(可选)")


# 初始化服务/Agent
coordinator = K8sCoordinatorAgent()
k8s_fixer_agent = K8sFixerAgent()
notification_service = NotificationService()
k8s_service = KubernetesService()


@router.post("/autofix/create")
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
            raise HTTPException(status_code=500, detail=f"自动修复失败: {str(fix_error)}")

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
            timestamp=datetime.now(BEIJING_TZ).isoformat(),
            success=bool(report.get("success"))
        )

        return APIResponse(
            code=0, message="自动修复完成", data=response.model_dump()
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"自动修复请求处理失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"自动修复失败: {str(e)}")


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
        raise HTTPException(status_code=500, detail=f"启动工作流失败: {str(e)}")


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
        raise HTTPException(status_code=500, detail=f"问题诊断失败: {str(e)}")


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
        raise HTTPException(status_code=500, detail=f"查询任务状态失败: {str(e)}")


@router.get("/history/list")
async def list_fix_history(
    page: Optional[int] = Query(1, description="页码（从1开始）"),
    size: Optional[int] = Query(20, description="每页大小"),
    search: Optional[str] = Query(None, description="搜索关键词")
):
    """获取修复历史列表（支持分页和搜索）"""
    try:
        logger.info(f"获取修复历史: page={page}, size={size}, search={search}")

        # 获取所有历史记录（需要先获取更多记录用于分页）
        history = coordinator.get_workflow_history() or []
        
        # 确保history是字典列表格式
        if history and not isinstance(history[0], dict):
            # 如果history是其他格式，尝试转换为字典
            history_dict = []
            for i, record in enumerate(history):
                if hasattr(record, '__dict__'):
                    record_dict = record.__dict__
                elif hasattr(record, 'model_dump'):
                    record_dict = record.model_dump()
                else:
                    # 简单转换
                    record_dict = {
                        "id": getattr(record, 'id', f"fix_{i}"),
                        "name": getattr(record, 'deployment', f"Fix {i+1}"),
                        "deployment": getattr(record, 'deployment', ''),
                        "namespace": getattr(record, 'namespace', 'default'),
                        "status": getattr(record, 'status', 'unknown'),
                        "timestamp": getattr(record, 'timestamp', ''),
                        "summary": getattr(record, 'summary', '')
                    }
                history_dict.append(record_dict)
            history = history_dict

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

    except ValueError as e:
        logger.error(f"参数验证失败: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取修复历史失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取修复历史失败: {str(e)}")


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
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
                "service": "autofix",
            },
        ).model_dump()

    except Exception as e:
        logger.error(f"自动修复服务健康检查失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"自动修复服务健康检查失败: {str(e)}"
        )


# 兼容旧接口：POST /api/v1/autofix
@router.post("/autofix")
async def create_autofix_alias(request_data: AutoFixRequest):
    return await create_autofix(request_data)


# 兼容旧接口：POST /api/v1/autofix/diagnose（集群/命名空间级诊断）
@router.post("/autofix/diagnose")
async def diagnose_cluster(request_data: WorkflowRequest):
    try:
        namespace = request_data.namespace or "default"
        result = await k8s_fixer_agent.diagnose_cluster_health(namespace=namespace)
        return APIResponse(code=0, message="诊断完成", data={"namespace": namespace, "report": result}).model_dump()
    except Exception as e:
        logger.error(f"集群诊断失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"集群诊断失败: {str(e)}")


# 兼容旧接口：POST /api/v1/autofix/workflow
@router.post("/autofix/workflow")
async def start_autofix_workflow(request_data: WorkflowStartRequest):
    try:
        ns = request_data.namespace or "default"
        if request_data.deployment:
            result = await coordinator.run_full_workflow(deployment=request_data.deployment, namespace=ns)
        else:
            result = await coordinator.run_batch_workflow(namespace=ns)
        return APIResponse(code=0, message="工作流执行完成", data=result).model_dump()
    except Exception as e:
        logger.error(f"启动工作流失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"启动工作流失败: {str(e)}")


# 兼容旧接口：POST /api/v1/autofix/notify
@router.post("/autofix/notify")
async def send_notification(req: NotificationRequest):
    try:
        ok = await notification_service.send_feishu_message(message=req.message, title=req.title)
        return APIResponse(code=0, message="通知已发送" if ok else "通知发送失败", data={"sent": ok, "level": req.level, "timestamp": req.timestamp or datetime.now(BEIJING_TZ).isoformat()}).model_dump()
    except Exception as e:
        logger.error(f"发送通知失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"发送通知失败: {str(e)}")
