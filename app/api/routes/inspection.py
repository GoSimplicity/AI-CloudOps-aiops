#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Inspection API 路由
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Body

from app.api.decorators import api_response, log_api_call
from app.core.inspection.profiles import PROFILES, list_rules_flat
from app.core.inspection.reporter import report_to_markdown
from app.models import BaseResponse
from app.models.inspection_models import InspectionRunRequest
from app.services.factory import ServiceFactory
from app.services.inspection_service import InspectionService

logger = logging.getLogger("aiops.api.inspection")

router = APIRouter(tags=["inspection"])
inspection_service = None


async def get_inspection_service() -> InspectionService:
    global inspection_service
    if inspection_service is None:
        inspection_service = await ServiceFactory.get_service(
            "inspection", InspectionService, initialize=False
        )
    return inspection_service


@router.get(
    "/rules",
    summary="获取已加载规则列表",
    response_model=BaseResponse,
)
@api_response("获取规则列表")
async def list_rules() -> Dict[str, Any]:
    return {"items": list_rules_flat()}


@router.get(
    "/profiles",
    summary="获取规则集配置",
    response_model=BaseResponse,
)
@api_response("获取规则集")
async def list_profiles() -> Dict[str, Any]:
    items = []
    for name, rules in PROFILES.items():
        items.append({
            "name": name,
            "description": "扩展规则集" if name != "basic" else "基础体检规则集",
            "rules": [r.id for r in rules],
        })
    return {"items": items}


@router.post(
    "/run",
    summary="执行一次巡检",
    response_model=BaseResponse,
)
@api_response("执行巡检")
@log_api_call(log_request=True)
async def run_inspection(
    request: InspectionRunRequest = Body(
        ..., examples={"default": {"value": {"scope": "namespace", "namespace": "default"}}}
    ),
) -> Dict[str, Any]:
    svc = await get_inspection_service()
    await svc.initialize()

    # 异步任务
    if request.async_task:
        return await svc.run_inspection_async(request)

    # 同步执行
    return await svc.run_inspection(request)


@router.get(
    "/tasks/{task_id}",
    summary="查询巡检任务状态",
    response_model=BaseResponse,
)
@api_response("查询任务状态")
async def get_task(task_id: str) -> Dict[str, Any]:
    svc = await get_inspection_service()
    return await svc.get_task_status(task_id)


@router.get(
    "/history",
    summary="查询历史巡检报告摘要",
    response_model=BaseResponse,
)
@api_response("获取历史")
async def list_history(limit: int = 50) -> Dict[str, Any]:
    svc = await get_inspection_service()
    return await svc.list_history(limit=limit)


@router.get(
    "/report/{report_id}",
    summary="获取巡检报告详情",
    response_model=BaseResponse,
)
@api_response("获取报告")
async def get_report(report_id: str) -> Dict[str, Any]:
    svc = await get_inspection_service()
    return await svc.get_report(report_id)


@router.get(
    "/report/{report_id}/markdown",
    summary="获取巡检报告Markdown",
    response_model=BaseResponse,
)
@api_response("导出报告Markdown")
async def get_report_markdown(report_id: str) -> Dict[str, Any]:
    svc = await get_inspection_service()
    data = await svc.get_report(report_id)
    if data.get("error"):
        return data
    md = report_to_markdown(data)
    return {"report_id": report_id, "markdown": md}


@router.get(
    "/config",
    summary="查看巡检配置",
    response_model=BaseResponse,
)
@api_response("获取巡检配置")
async def get_config() -> Dict[str, Any]:
    from app.config.settings import config as app_config

    c = app_config.inspection
    return {
        "enabled": c.enabled,
        "default_profile": c.default_profile,
        "severity_threshold": c.severity_threshold,
        "time_window_minutes": c.time_window_minutes,
        "k8s": {
            "include_events": c.include_events,
            "include_logs": c.include_logs,
            "log_tail_lines": c.log_tail_lines,
        },
        "prometheus": {
            "step": c.prometheus_step,
            "queries_timeout": c.prometheus_queries_timeout,
        },
        "retention": {"enabled": c.retention_enabled, "max_reports": c.max_reports},
        "scheduler": {
            "enabled": c.scheduler_enabled,
            "cron": c.scheduler_cron,
        },
    }


@router.post(
    "/scheduler/start",
    summary="启动巡检调度",
    response_model=BaseResponse,
)
@api_response("启动调度")
async def start_scheduler() -> Dict[str, Any]:
    svc = await get_inspection_service()
    return await svc.start_scheduler()


@router.post(
    "/scheduler/stop",
    summary="停止巡检调度",
    response_model=BaseResponse,
)
@api_response("停止调度")
async def stop_scheduler() -> Dict[str, Any]:
    svc = await get_inspection_service()
    return await svc.stop_scheduler()


@router.get(
    "/scheduler/status",
    summary="查看巡检调度状态",
    response_model=BaseResponse,
)
@api_response("调度状态")
async def scheduler_status() -> Dict[str, Any]:
    svc = await get_inspection_service()
    return await svc.scheduler_status()


