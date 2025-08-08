#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 根因分析API路由 - 提供异常检测、相关性分析和根本原因识别功能
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config.settings import config
from app.core.rca.analyzer import RCAAnalyzer
from app.core.rca.collectors.k8s_state_collector import K8sStateCollector
from app.core.rca.correlator import CorrelationAnalyzer
from app.core.rca.jobs.job_manager import RCAJobManager
from app.core.rca.topology.graph import build_topology_from_state
from app.models.request_models import RCARequest
from app.models.response_models import APIResponse
from app.services.prometheus import PrometheusService
from app.utils.validators import validate_metric_list, validate_time_range

logger = logging.getLogger("aiops.rca")

router = APIRouter(tags=["rca"])

# 初始化分析器
rca_analyzer = RCAAnalyzer()

# 尝试初始化任务管理器（Redis 不可用时降级）
try:
    job_manager = RCAJobManager()
except Exception as e:
    job_manager = None  # 延迟到端点中报错
    logger.warning(f"RCAJobManager 初始化失败（异步任务功能不可用）: {e}")


class RCAJobRequest(BaseModel):
    """异步RCA任务提交请求模型"""

    start_time: datetime
    end_time: datetime
    metrics: Optional[list] = None
    namespace: Optional[str] = None


@router.post("/rca")
async def root_cause_analysis(request_data: RCARequest):
    """
    根因分析接口
    """
    try:
        # 验证时间范围
        if not validate_time_range(request_data.start_time, request_data.end_time):
            raise HTTPException(status_code=400, detail="无效的时间范围")

        # 检查时间范围限制
        time_diff = (
            request_data.end_time - request_data.start_time
        ).total_seconds() / 60
        max_minutes = getattr(config, "rca_max_time_range_minutes", 1440)  # 默认24小时

        if time_diff > max_minutes:
            raise HTTPException(
                status_code=400, detail=f"时间范围不能超过{max_minutes}分钟"
            )

        # 验证指标列表
        if request_data.metrics and not validate_metric_list(request_data.metrics):
            raise HTTPException(status_code=400, detail="无效的指标列表")

        logger.info(
            f"开始根因分析: {request_data.start_time} 到 {request_data.end_time}"
        )

        # 调用根因分析服务
        try:
            analysis_result = await rca_analyzer.analyze(
                request_data.start_time,
                request_data.end_time,
                request_data.metrics,
            )
        except Exception as analysis_error:
            logger.error(f"根因分析执行失败: {str(analysis_error)}")
            raise HTTPException(status_code=500, detail="根因分析执行失败")

        return APIResponse(
            code=0, message="根因分析完成", data=analysis_result
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"根因分析请求处理失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"根因分析失败: {str(e)}")


@router.post("/rca/jobs")
async def submit_rca_job(request_data: RCAJobRequest):
    """提交异步根因分析任务，返回 job_id"""
    try:
        if job_manager is None:
            raise HTTPException(status_code=503, detail="异步任务服务未就绪")

        if not validate_time_range(request_data.start_time, request_data.end_time):
            raise HTTPException(status_code=400, detail="无效的时间范围")

        # 仅校验指标列表格式，具体存在性交由下游处理
        if request_data.metrics and not validate_metric_list(request_data.metrics):
            raise HTTPException(status_code=400, detail="无效的指标列表")

        job_id = job_manager.submit_job(
            {
                "start_time": request_data.start_time,
                "end_time": request_data.end_time,
                "metrics": request_data.metrics,
                "namespace": request_data.namespace,
            }
        )

        return APIResponse(
            code=0, message="任务已提交", data={"job_id": job_id}
        ).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"提交RCA任务失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"提交任务失败: {str(e)}")


@router.get("/rca/jobs/{job_id}")
async def get_rca_job(job_id: str):
    """查询异步根因分析任务状态与结果"""
    try:
        if job_manager is None:
            raise HTTPException(status_code=503, detail="异步任务服务未就绪")

        doc = job_manager.get_job(job_id)
        if not doc:
            raise HTTPException(status_code=404, detail="未找到该任务")
        return APIResponse(code=0, message="ok", data=doc).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询RCA任务失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"查询任务失败: {str(e)}")


@router.get("/rca/metrics")
async def get_available_metrics():
    """获取 Prometheus 可用指标与默认指标"""
    try:
        prom = PrometheusService()
        metrics = await prom.get_available_metrics()
        return APIResponse(
            code=0,
            message="ok",
            data={
                "default_metrics": config.rca.default_metrics,
                "available_metrics": metrics,
            },
        ).model_dump()
    except Exception as e:
        logger.error(f"获取可用指标失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取可用指标失败: {str(e)}")


@router.get("/rca/topology")
async def get_topology(
    namespace: Optional[str] = None,
    source: Optional[str] = None,
    max_hops: Optional[int] = 1,
    direction: Optional[str] = "out",
):
    """获取指定命名空间的拓扑快照"""
    try:
        collector = K8sStateCollector(namespace=namespace)
        state = await collector.snapshot()
        graph = build_topology_from_state(state)
        topo = graph.to_dict()
        impact: Optional[list] = None
        if source:
            try:
                hops = max(0, min(int(max_hops or 1), 5))
                dirn = direction if direction in ("out", "in") else "out"
                impact = graph.reachable([source], max_hops=hops, direction=dirn)
            except Exception:
                impact = []
        return APIResponse(
            code=0,
            message="ok",
            data={
                "namespace": state.get("namespace"),
                "counts": {
                    "pods": len(state.get("pods") or []),
                    "deployments": len(state.get("deployments") or []),
                    "services": len(state.get("services") or []),
                },
                "topology": topo,
                "impact_scope": impact,
            },
        ).model_dump()
    except Exception as e:
        logger.error(f"获取拓扑失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取拓扑失败: {str(e)}")


@router.post("/rca/anomaly")
async def detect_anomaly(
    start_time: datetime,
    end_time: datetime,
    metrics: Optional[list] = None,
    sensitivity: Optional[float] = 0.8,
):
    """
    异常检测接口
    """
    try:
        # 验证时间范围
        if not validate_time_range(start_time, end_time):
            raise HTTPException(status_code=400, detail="无效的时间范围")

        # 验证敏感度参数
        if sensitivity < 0.1 or sensitivity > 1.0:
            raise HTTPException(status_code=400, detail="敏感度参数必须在0.1-1.0之间")

        logger.info(f"开始异常检测: {start_time} 到 {end_time}")

        # 调用异常检测服务
        anomalies = await rca_analyzer.detect_anomalies(
            start_time, end_time, metrics, sensitivity
        )

        return APIResponse(
            code=0,
            message="异常检测完成",
            data={
                "anomalies": anomalies,
                "detection_period": {
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat(),
                },
                "sensitivity": sensitivity,
            },
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"异常检测失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"异常检测失败: {str(e)}")


# 兼容测试：/rca/anomalies
@router.post("/rca/anomalies")
async def detect_anomalies_alias(
    start_time: datetime,
    end_time: datetime,
    metrics: Optional[list] = None,
    threshold: Optional[float] = 0.8,
    namespace: Optional[str] = None,
):
    return await detect_anomaly(start_time, end_time, metrics, threshold)


@router.post("/rca/correlation")
async def analyze_correlation(
    start_time: datetime,
    end_time: datetime,
    target_metric: str,
    metrics: Optional[list] = None,
):
    """
    相关性分析接口
    """
    try:
        # 验证时间范围
        if not validate_time_range(start_time, end_time):
            raise HTTPException(status_code=400, detail="无效的时间范围")

        # 验证目标指标
        if not target_metric or not target_metric.strip():
            raise HTTPException(status_code=400, detail="必须指定目标指标")

        logger.info(f"开始相关性分析: 目标指标={target_metric}")

        # 调用相关性分析服务
        correlations = await rca_analyzer.analyze_correlations(
            start_time, end_time, target_metric, metrics
        )

        return APIResponse(
            code=0,
            message="相关性分析完成",
            data={
                "target_metric": target_metric,
                "correlations": correlations,
                "analysis_period": {
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat(),
                },
            },
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"相关性分析失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"相关性分析失败: {str(e)}")


class CrossCorrelationRequest(BaseModel):
    start_time: datetime
    end_time: datetime
    metrics: Optional[list] = None
    max_lags: Optional[int] = 10


@router.post("/rca/cross-correlation")
async def cross_correlation(req: CrossCorrelationRequest):
    """跨时滞相关分析端点"""
    try:
        if not validate_time_range(req.start_time, req.end_time):
            raise HTTPException(status_code=400, detail="无效的时间范围")

        metrics = req.metrics or config.rca.default_metrics
        # 收集数据
        metrics_data = await rca_analyzer._collect_metrics_data(
            req.start_time, req.end_time, metrics
        )
        if not metrics_data:
            return APIResponse(code=0, message="无有效数据", data={}).model_dump()

        corr = CorrelationAnalyzer()
        result = await corr.analyze_correlations_with_cross_lag(
            metrics_data, max_lags=min(max(1, int(req.max_lags or 10)), 20)
        )
        return APIResponse(code=0, message="ok", data=result).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"跨时滞相关分析失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"跨时滞相关分析失败: {str(e)}")


# 兼容测试：/rca/correlations
@router.post("/rca/correlations")
async def analyze_correlations_alias(
    start_time: datetime,
    end_time: datetime,
    metrics: Optional[list] = None,
    namespace: Optional[str] = None,
    min_correlation: Optional[float] = None,
):
    # 直接调用已有端点；当前忽略 min_correlation（内部使用配置阈值）
    return await analyze_correlation(
        start_time, end_time, target_metric="", metrics=metrics
    )


@router.post("/rca/timeline")
async def generate_timeline(
    start_time: datetime, end_time: datetime, events: Optional[list] = None
):
    """
    事件时间线生成接口
    """
    try:
        # 验证时间范围
        if not validate_time_range(start_time, end_time):
            raise HTTPException(status_code=400, detail="无效的时间范围")

        logger.info(f"生成事件时间线: {start_time} 到 {end_time}")

        # 调用时间线生成服务
        timeline = await rca_analyzer.generate_timeline(start_time, end_time, events)

        return APIResponse(
            code=0,
            message="事件时间线生成完成",
            data={
                "timeline": timeline,
                "period": {
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat(),
                },
            },
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"事件时间线生成失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"事件时间线生成失败: {str(e)}")


@router.get("/rca/history")
async def get_analysis_history(limit: Optional[int] = 50):
    """
    获取分析历史记录接口
    """
    try:
        # 验证限制参数
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=400, detail="limit参数必须在1-500之间")

        logger.info(f"获取分析历史记录，限制数量: {limit}")

        # 获取历史记录
        history = await asyncio.to_thread(rca_analyzer.get_analysis_history, limit)

        return APIResponse(
            code=0,
            message="分析历史记录获取成功",
            data={"history": history, "count": len(history), "limit": limit},
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取分析历史记录失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取分析历史记录失败: {str(e)}")


@router.get("/rca/health")
async def rca_health():
    """
    RCA服务健康检查接口
    """
    try:
        # 检查RCA服务健康状态
        health_status = rca_analyzer.is_healthy()

        return APIResponse(
            code=0,
            message="RCA服务健康检查完成",
            data={
                "healthy": health_status,
                "timestamp": datetime.utcnow().isoformat(),
                "service": "rca",
            },
        ).model_dump()

    except Exception as e:
        logger.error(f"RCA服务健康检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"RCA服务健康检查失败: {str(e)}")
