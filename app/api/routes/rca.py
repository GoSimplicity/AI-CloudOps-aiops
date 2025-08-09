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
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.config.settings import config
from app.core.rca.analyzer import RCAAnalyzer
from app.core.rca.collectors.k8s_state_collector import K8sStateCollector
from app.core.rca.correlator import CorrelationAnalyzer
from app.core.rca.jobs.job_manager import RCAJobManager
from app.core.rca.topology.graph import build_topology_from_state
from app.models.request_models import RCARequest
from app.models.response_models import APIResponse, PaginatedListAPIResponse
from app.services.prometheus import PrometheusService
from app.utils.pagination import process_list_with_pagination_and_search
from app.utils.validators import validate_metric_list, validate_time_range

logger = logging.getLogger("aiops.rca")

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))

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

    start_time: datetime = Field(..., description="起始时间，ISO8601 格式")
    end_time: datetime = Field(..., description="结束时间，ISO8601 格式")
    metrics: Optional[list] = Field(default=None, description="指标列表（可选）")
    namespace: Optional[str] = Field(default=None, description="命名空间（可选）")


@router.post("/rca/create")
async def create_root_cause_analysis(request_data: RCARequest):
    """
    创建根因分析
    """
    try:
        # 验证时间范围
        if not validate_time_range(request_data.start_time, request_data.end_time):
            raise HTTPException(status_code=400, detail="无效的时间范围")

        # 检查时间范围限制
        time_diff = (
            request_data.end_time - request_data.start_time
        ).total_seconds() / 60
        max_minutes = config.rca.max_time_range

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
                include_logs=request_data.include_logs,
                include_traces=request_data.include_traces,
                namespace=request_data.namespace,
                service_name=request_data.service_name,
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


# 兼容旧接口：POST /api/v1/rca
@router.post("/rca")
async def create_root_cause_analysis_alias(request_data: RCARequest):
    return await create_root_cause_analysis(request_data)


@router.post("/jobs/create")
async def create_rca_job(request_data: RCAJobRequest):
    """创建异步根因分析任务，返回 job_id"""
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
            code=0,
            message="任务已提交",
            data={
                "job_id": job_id,
                "flags": {
                    "request_override": config.rca.request_override,
                    "logs_enabled": getattr(config, "logs").enabled,
                    "tracing_enabled": getattr(config, "tracing").enabled,
                },
            },
        ).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"提交RCA任务失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"提交任务失败: {str(e)}")


# 兼容旧接口：POST /api/v1/rca/jobs
@router.post("/rca/jobs")
async def create_rca_job_alias(request_data: RCAJobRequest):
    return await create_rca_job(request_data)


@router.get("/jobs/{job_id}")
async def get_job_detail(job_id: str = Path(..., description="RCA任务ID")):
    """查询异步根因分析任务状态与结果"""
    try:
        if job_manager is None:
            raise HTTPException(status_code=503, detail="异步任务服务未就绪")

        doc = job_manager.get_job(job_id)
        if not doc:
            raise HTTPException(status_code=404, detail="未找到该任务")
        # 附带平台flags，便于前端渲染
        payload = dict(doc)
        payload["flags"] = {
            "request_override": config.rca.request_override,
            "logs_enabled": getattr(config, "logs").enabled,
            "tracing_enabled": getattr(config, "tracing").enabled,
        }
        return APIResponse(code=0, message="ok", data=payload).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询RCA任务失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"查询任务失败: {str(e)}")


@router.get("/metrics/list")
async def list_available_metrics():
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
                "flags": {
                    "request_override": config.rca.request_override,
                    "logs_enabled": getattr(config, "logs").enabled,
                    "tracing_enabled": getattr(config, "tracing").enabled,
                },
            },
        ).model_dump()
    except Exception as e:
        logger.error(f"获取可用指标失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取可用指标失败: {str(e)}")


# 兼容旧接口：GET /api/v1/rca/metrics
@router.get("/rca/metrics")
async def list_available_metrics_alias():
    return await list_available_metrics()


@router.get("/topology/list")
async def list_topology(
    namespace: Optional[str] = Query(None, description="目标命名空间（可选）"),
    source: Optional[str] = Query(None, description="源节点名称，用于影响范围计算（可选）"),
    max_hops: Optional[int] = Query(1, description="最大跳数（默认1）"),
    direction: Optional[str] = Query("out", description="边方向（out/in），默认out"),
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
                "flags": {
                    "request_override": config.rca.request_override,
                    "logs_enabled": getattr(config, "logs").enabled,
                    "tracing_enabled": getattr(config, "tracing").enabled,
                },
            },
        ).model_dump()
    except Exception as e:
        logger.error(f"获取拓扑失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取拓扑失败: {str(e)}")


# 兼容旧接口：GET /api/v1/rca/topology
@router.get("/rca/topology")
async def list_topology_alias(
    namespace: Optional[str] = Query(None, description="目标命名空间（可选）"),
    source: Optional[str] = Query(None, description="源节点名称（可选）"),
    max_hops: Optional[int] = Query(1, description="最大跳数（默认1）"),
    direction: Optional[str] = Query("out", description="边方向（out/in），默认out"),
):
    return await list_topology(namespace, source, max_hops, direction)


@router.post("/anomalies/create")
async def create_anomaly_detection(
    start_time: datetime = Query(..., description="起始时间，ISO8601 格式"),
    end_time: datetime = Query(..., description="结束时间，ISO8601 格式"),
    metrics: Optional[list] = Query(None, description="指标列表（可选）"),
    sensitivity: Optional[float] = Query(0.8, description="检测敏感度(0.1-1.0)，默认0.8"),
):
    """
    创建异常检测
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


# 兼容旧接口：POST /api/v1/rca/anomalies
@router.post("/rca/anomalies")
async def create_anomaly_detection_alias(
    start_time: datetime = Query(..., description="起始时间，ISO8601 格式"),
    end_time: datetime = Query(..., description="结束时间，ISO8601 格式"),
    metrics: Optional[list] = Query(None, description="指标列表（可选）"),
    sensitivity: Optional[float] = Query(0.8, description="检测敏感度(0.1-1.0)，默认0.8"),
):
    return await create_anomaly_detection(start_time, end_time, metrics, sensitivity)


# 兼容测试：/anomalies/list
@router.get("/anomalies/list")
async def list_anomalies(
    start_time: datetime = Query(..., description="起始时间，ISO8601 格式"),
    end_time: datetime = Query(..., description="结束时间，ISO8601 格式"),
    metrics: Optional[list] = Query(None, description="指标列表（可选）"),
    threshold: Optional[float] = Query(0.8, description="阈值（历史参数，当前不使用）"),
    namespace: Optional[str] = Query(None, description="命名空间（可选）"),
):
    return await create_anomaly_detection(start_time, end_time, metrics, threshold)


@router.post("/correlations/create")
async def create_correlation_analysis(
    start_time: datetime = Query(..., description="起始时间，ISO8601 格式"),
    end_time: datetime = Query(..., description="结束时间，ISO8601 格式"),
    target_metric: str = Query(..., description="目标指标名称"),
    metrics: Optional[list] = Query(None, description="指标列表（可选）"),
):
    """
    创建相关性分析
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


# 兼容旧接口：POST /api/v1/rca/correlations
@router.post("/rca/correlations")
async def create_correlation_analysis_alias(
    start_time: datetime = Query(..., description="起始时间，ISO8601 格式"),
    end_time: datetime = Query(..., description="结束时间，ISO8601 格式"),
    target_metric: str = Query("", description="目标指标名称，可留空"),
    metrics: Optional[list] = Query(None, description="指标列表（可选）"),
):
    return await create_correlation_analysis(start_time, end_time, target_metric, metrics)


class CrossCorrelationRequest(BaseModel):
    start_time: datetime = Field(..., description="起始时间，ISO8601 格式")
    end_time: datetime = Field(..., description="结束时间，ISO8601 格式")
    metrics: Optional[list] = Field(default=None, description="指标列表（可选）")
    max_lags: Optional[int] = Field(default=10, description="最大时滞阶数，默认10")


@router.post("/cross-correlations/create")
async def create_cross_correlation(req: CrossCorrelationRequest):
    """创建跨时滞相关分析"""
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


# 兼容测试：/correlations/list
@router.get("/correlations/list")
async def list_correlations(
    start_time: datetime = Query(..., description="起始时间，ISO8601 格式"),
    end_time: datetime = Query(..., description="结束时间，ISO8601 格式"),
    metrics: Optional[list] = Query(None, description="指标列表（可选）"),
    namespace: Optional[str] = Query(None, description="命名空间（可选）"),
    min_correlation: Optional[float] = Query(None, description="最小相关系数（历史参数，当前不使用）"),
):
    # 直接调用已有端点；当前忽略 min_correlation（内部使用配置阈值）
    # 为保持向后兼容与接口完整性，这里消费但不使用该参数
    _ = min_correlation
    return await create_correlation_analysis(
        start_time, end_time, target_metric="", metrics=metrics
    )


@router.post("/timelines/create")
async def create_timeline(
    start_time: datetime = Query(..., description="起始时间，ISO8601 格式"),
    end_time: datetime = Query(..., description="结束时间，ISO8601 格式"),
    events: Optional[list] = Query(None, description="事件列表（可选）"),
):
    """
    创建事件时间线
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


@router.get("/history/list")
async def list_analysis_history(
    page: Optional[int] = Query(1, description="页码（从1开始）"),
    size: Optional[int] = Query(20, description="每页大小"),
    search: Optional[str] = Query(None, description="搜索关键词")
):
    """
    获取分析历史记录列表（支持分页和搜索）
    """
    try:
        logger.info(f"获取分析历史记录: page={page}, size={size}, search={search}")

        # 获取所有历史记录（需要先获取更多记录用于分页）
        # 这里我们获取更多记录，然后在内存中分页
        max_records = 1000  # 最大获取1000条记录用于分页
        history = await asyncio.to_thread(rca_analyzer.get_analysis_history, max_records)
        
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
                        "id": getattr(record, 'id', f"analysis_{i}"),
                        "name": getattr(record, 'name', f"Analysis {i+1}"),
                        "status": getattr(record, 'status', 'unknown'),
                        "timestamp": getattr(record, 'timestamp', ''),
                        "type": getattr(record, 'type', 'rca')
                    }
                history_dict.append(record_dict)
            history = history_dict

        # 应用分页和搜索（在name、status、type字段中搜索）
        paginated_history, total = process_list_with_pagination_and_search(
            items=history,
            page=page,
            size=size,
            search=search,
            search_fields=["name", "status", "type", "summary"]
        )

        return PaginatedListAPIResponse(
            code=0,
            message="分析历史记录获取成功",
            items=paginated_history,
            total=total
        ).model_dump()

    except ValueError as e:
        logger.error(f"参数验证失败: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
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
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
                "service": "rca",
                "flags": {
                    "request_override": config.rca.request_override,
                    "logs_enabled": getattr(config, "logs").enabled,
                    "tracing_enabled": getattr(config, "tracing").enabled,
                },
            },
        ).model_dump()

    except Exception as e:
        logger.error(f"RCA服务健康检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"RCA服务健康检查失败: {str(e)}")
