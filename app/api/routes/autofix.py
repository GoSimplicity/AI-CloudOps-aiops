#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 自动修复API路由 - 提供Kubernetes问题自动诊断、修复和工作流管理
"""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.agents.k8s_fixer import K8sFixerAgent
from app.core.agents.notifier import NotifierAgent
from app.core.agents.supervisor import SupervisorAgent
from app.models.request_models import AutoFixRequest
from app.models.response_models import APIResponse, AutoFixResponse, PaginatedListAPIResponse
from app.services.notification import NotificationService
from app.utils.validators import (
    sanitize_input,
    validate_deployment_name,
    validate_namespace,
)
from app.utils.pagination import process_list_with_pagination_and_search

logger = logging.getLogger("aiops.autofix")

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))

router = APIRouter(tags=["autofix"])


class WorkflowRequest(BaseModel):
    namespace: Optional[str] = "default"


class StatusRequest(BaseModel):
    task_id: str


# 初始化Agent
supervisor_agent = SupervisorAgent()
k8s_fixer_agent = K8sFixerAgent()
notifier_agent = NotifierAgent()
notification_service = NotificationService()


@router.post("/autofix")
async def autofix_k8s(request_data: AutoFixRequest):
    """自动修复Kubernetes问题"""
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

        # 生成任务ID
        task_id = f"autofix_{int(time.time())}_{deployment}"

        # 创建修复任务
        try:
            fix_result = await asyncio.to_thread(
                supervisor_agent.coordinate_fix,
                deployment=deployment,
                namespace=namespace,
                task_id=task_id,
            )
        except Exception as fix_error:
            logger.error(f"自动修复执行失败: {str(fix_error)}")
            raise HTTPException(
                status_code=500, detail=f"自动修复失败: {str(fix_error)}"
            )

        # 发送通知
        try:
            await asyncio.to_thread(
                notification_service.send_autofix_notification,
                deployment,
                namespace,
                fix_result.get("success", False),
                fix_result.get("summary", "修复完成"),
            )
        except Exception as notify_error:
            logger.warning(f"发送通知失败: {str(notify_error)}")

        # 构建响应
        response = AutoFixResponse(
            status="success" if fix_result.get("success", False) else "failed",
            result=fix_result.get("summary", "修复完成"),
            deployment=deployment,
            namespace=namespace,
            actions_taken=fix_result.get("actions", []),
            timestamp=datetime.now(BEIJING_TZ).isoformat(),
            success=fix_result.get("success", False)
        )

        return APIResponse(
            code=0, message="自动修复完成", data=response.model_dump()
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"自动修复请求处理失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"自动修复失败: {str(e)}")


@router.post("/autofix/workflow")
async def start_workflow(request_data: WorkflowRequest):
    """启动自动修复工作流"""
    try:
        namespace = request_data.namespace

        if not validate_namespace(namespace):
            raise HTTPException(status_code=400, detail="无效的命名空间名称")

        logger.info(f"启动自动修复工作流: namespace={namespace}")

        # 启动工作流
        workflow_result = await asyncio.to_thread(
            supervisor_agent.start_workflow, namespace=namespace
        )

        return APIResponse(
            code=0, message="工作流启动成功", data=workflow_result
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"启动工作流失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"启动工作流失败: {str(e)}")


@router.post("/autofix/diagnosis")
async def run_diagnosis(request_data: AutoFixRequest):
    """运行问题诊断"""
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

        # 运行诊断
        diagnosis_result = await asyncio.to_thread(
            k8s_fixer_agent.diagnose_issues, deployment=deployment, namespace=namespace
        )

        return APIResponse(
            code=0, message="问题诊断完成", data=diagnosis_result
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"问题诊断失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"问题诊断失败: {str(e)}")


@router.get("/autofix/status/{task_id}")
async def get_task_status(task_id: str):
    """获取任务状态"""
    try:
        task_id = sanitize_input(task_id)

        if not task_id:
            raise HTTPException(status_code=400, detail="任务ID不能为空")

        logger.info(f"查询任务状态: task_id={task_id}")

        # 查询任务状态
        status = await asyncio.to_thread(
            supervisor_agent.get_task_status, task_id=task_id
        )

        if status is None:
            raise HTTPException(status_code=404, detail="任务不存在")

        return APIResponse(code=0, message="任务状态查询成功", data=status).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询任务状态失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"查询任务状态失败: {str(e)}")


@router.get("/autofix/history")
async def get_history(
    page: Optional[int] = 1,
    size: Optional[int] = 20,
    search: Optional[str] = None
):
    """获取修复历史（支持分页和搜索）"""
    try:
        logger.info(f"获取修复历史: page={page}, size={size}, search={search}")

        # 获取所有历史记录（需要先获取更多记录用于分页）
        max_records = 1000  # 最大获取1000条记录用于分页
        history = await asyncio.to_thread(supervisor_agent.get_fix_history, limit=max_records)
        
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
        # 检查各个Agent的健康状态
        supervisor_healthy = await asyncio.to_thread(supervisor_agent.is_healthy)
        fixer_healthy = await asyncio.to_thread(k8s_fixer_agent.is_healthy)
        notifier_healthy = await asyncio.to_thread(notifier_agent.is_healthy)

        overall_healthy = supervisor_healthy and fixer_healthy and notifier_healthy

        return APIResponse(
            code=0,
            message="自动修复服务健康检查完成",
            data={
                "healthy": overall_healthy,
                "components": {
                    "supervisor": supervisor_healthy,
                    "fixer": fixer_healthy,
                    "notifier": notifier_healthy,
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
