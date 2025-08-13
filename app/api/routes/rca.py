#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query, Depends
from sqlalchemy import select, func

from app.config.settings import config
from app.core.rca.analyzer import RCAAnalyzer
from app.core.rca.collectors.k8s_state_collector import K8sStateCollector
from app.core.rca.correlator import CorrelationAnalyzer
from app.core.rca.jobs.job_manager import RCAJobManager
from app.core.rca.topology.graph import build_topology_from_state
from app.db.base import session_scope
from app.db.models import RCAAnalysisRecord, utcnow
from app.models.request_models import (
    AutoRCAAnalyzeReq,
    AutoRCACrossCorrelationReq,
    AutoRCAJobReq,
    AutoRCACorrelationReq,
    AutoRCAAnomalyReq,
    AutoRCATimelineReq,
    RCARecordCreateReq,
    RCARecordUpdateReq,
    RCARecordListReq,
)
from app.models.response_models import APIResponse, PaginatedListAPIResponse
from app.models.entities import (
    AnomalyDetectionEntity,
    CorrelationAnalysisEntity,
    RCAJobDetailEntity,
    RCAJobEntity,
    RCAMetricsEntity,
    RCAResultEntity,
    TimelineEntity,
    TopologySnapshotEntity,
    RCARecordEntity,
    DeletionResultEntity,
)
from app.services.prometheus import PrometheusService
from app.utils.pagination import process_list_with_pagination_and_search
from app.utils.time_utils import iso_utc_now
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


def _ensure_str_list(value):
    """确保将配置中的列表值转换为 List[str]。接受 list 或 字符串(逗号分隔/JSON/Python风格)。"""
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if value is None:
        return []
    text = str(value).strip()
    # 优先尝试 JSON
    if text.startswith("[") and text.endswith("]"):
        import json

        try:
            data = json.loads(text.replace("'", '"'))
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass
        # 回退：去掉方括号，按逗号分割
        text = text[1:-1]
    # 逗号分割
    parts = [p.strip().strip("'\"") for p in text.split(",")]
    return [p for p in parts if p]


@router.get("/rca/records/list")
async def list_rca_records(params: RCARecordListReq = Depends()):
    try:
        with session_scope() as session:
            stmt = select(RCAAnalysisRecord).where(
                RCAAnalysisRecord.deleted_at.is_(None)
            )
            if params.namespace:
                stmt = stmt.where(RCAAnalysisRecord.namespace == params.namespace)
            if params.status:
                stmt = stmt.where(RCAAnalysisRecord.status == params.status)
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
                    stmt.order_by(RCAAnalysisRecord.id.desc())
                    .offset((page - 1) * size)
                    .limit(size)
                )
                .scalars()
                .all()
            )
            items = [
                RCARecordEntity(
                    id=r.id,
                    start_time=r.start_time,
                    end_time=r.end_time,
                    metrics=r.metrics,
                    namespace=r.namespace,
                    service_name=r.service_name,
                    status=r.status,
                    summary=r.summary,
                    created_at=r.created_at.isoformat() if r.created_at else None,
                    updated_at=r.updated_at.isoformat() if r.updated_at else None,
                ).model_dump()
                for r in rows
            ]
        return APIResponse(
            code=0, message="ok", data={"items": items, "total": total}
        ).model_dump()
    except Exception as e:
        logger.error(f"list_rca_records 失败: {e}")
        return APIResponse(
            code=0, message="ok", data={"items": [], "total": 0}
        ).model_dump()


@router.post("/rca/records/create")
async def create_rca_record(payload: RCARecordCreateReq):
    try:
        with session_scope() as session:
            rec = RCAAnalysisRecord(
                start_time=payload.start_time,
                end_time=payload.end_time,
                metrics=payload.metrics,
                namespace=payload.namespace,
                service_name=payload.service_name,
                status=payload.status or "ok",
                summary=payload.summary,
            )
            session.add(rec)
            session.flush()
            entity = RCARecordEntity(
                id=rec.id,
                start_time=rec.start_time,
                end_time=rec.end_time,
                metrics=rec.metrics,
                namespace=rec.namespace,
                service_name=rec.service_name,
                status=rec.status,
                summary=rec.summary,
                created_at=rec.created_at.isoformat() if rec.created_at else None,
                updated_at=rec.updated_at.isoformat() if rec.updated_at else None,
            )
        return APIResponse(
            code=0, message="created", data=entity.model_dump()
        ).model_dump()
    except Exception as e:
        logger.error(f"create_rca_record 失败: {e}")
        raise HTTPException(status_code=500, detail="create record failed") from e


@router.get("/rca/records/detail/{record_id}")
async def get_rca_record_db(record_id: int):
    try:
        with session_scope() as session:
            r = session.get(RCAAnalysisRecord, record_id)
            if not r or r.deleted_at is not None:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            entity = RCARecordEntity(
                id=r.id,
                start_time=r.start_time,
                end_time=r.end_time,
                metrics=r.metrics,
                namespace=r.namespace,
                service_name=r.service_name,
                status=r.status,
                summary=r.summary,
                created_at=r.created_at.isoformat() if r.created_at else None,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
            )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"get_rca_record 失败: {e}")
        raise HTTPException(status_code=500, detail="get record failed") from e


@router.put("/rca/records/update/{record_id}")
async def update_rca_record(record_id: int, payload: RCARecordUpdateReq):
    try:
        with session_scope() as session:
            r = session.get(RCAAnalysisRecord, record_id)
            if not r or r.deleted_at is not None:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            for field in (
                "start_time",
                "end_time",
                "metrics",
                "namespace",
                "service_name",
                "status",
                "summary",
            ):
                value = getattr(payload, field)
                if value is not None:
                    setattr(r, field, value)
            session.add(r)
            entity = RCARecordEntity(
                id=r.id,
                start_time=r.start_time,
                end_time=r.end_time,
                metrics=r.metrics,
                namespace=r.namespace,
                service_name=r.service_name,
                status=r.status,
                summary=r.summary,
                created_at=r.created_at.isoformat() if r.created_at else None,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
            )
        return APIResponse(
            code=0, message="updated", data=entity.model_dump()
        ).model_dump()
    except Exception as e:
        logger.error(f"update_rca_record 失败: {e}")
        raise HTTPException(status_code=500, detail="update record failed") from e


@router.delete("/rca/records/delete/{record_id}")
async def delete_rca_record_db(record_id: int):
    try:
        with session_scope() as session:
            r = session.get(RCAAnalysisRecord, record_id)
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
        logger.error(f"delete_rca_record 失败: {e}")
        raise HTTPException(status_code=500, detail="delete record failed") from e


@router.post(
    "/rca/analyses/create",
    summary="创建根因分析",
    description="根据时间范围与指标执行根因分析，返回异常、相关性与候选根因等结果。",
    response_model=APIResponse,
)
async def create_root_cause_analysis(request_data: AutoRCAAnalyzeReq):
    """RESTful 主入口：创建根因分析"""
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
            raise HTTPException(
                status_code=500, detail="根因分析执行失败"
            ) from analysis_error

        # 成功后记录分析摘要到数据库（不影响返回）
        try:
            with session_scope() as session:
                session.add(
                    RCAAnalysisRecord(
                        start_time=request_data.start_time.isoformat(),
                        end_time=request_data.end_time.isoformat(),
                        metrics=",".join(request_data.metrics)
                        if request_data.metrics
                        else None,
                        namespace=request_data.namespace,
                        service_name=request_data.service_name,
                        status="ok",
                        summary=(
                            analysis_result.get("summary")
                            if isinstance(analysis_result, dict)
                            else None
                        ),
                    )
                )
        except Exception:
            pass

        entity = RCAResultEntity(
            result=analysis_result
            if isinstance(analysis_result, dict)
            else {"result": analysis_result}
        )
        return APIResponse(
            code=0, message="根因分析完成", data=entity.model_dump()
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"根因分析请求处理失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"根因分析失败: {str(e)}") from e


# 仅保留 RESTful 新接口


@router.post(
    "/rca/jobs",
    summary="提交RCA异步任务",
    description="提交根因分析异步任务并返回 job_id。",
    response_model=APIResponse,
)
async def create_rca_job(request_data: AutoRCAJobReq):
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

        entity = RCAJobEntity(
            job_id=job_id,
            flags={
                "request_override": config.rca.request_override,
                "logs_enabled": config.logs.enabled,
                "tracing_enabled": config.tracing.enabled,
            },
        )
        return APIResponse(
            code=0, message="任务已提交", data=entity.model_dump()
        ).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"提交RCA任务失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"提交任务失败: {str(e)}") from e


@router.get(
    "/rca/jobs/detail/{job_id}",
    summary="查询RCA异步任务详情",
    description="根据 job_id 查询异步根因分析任务的状态与结果。",
    response_model=APIResponse,
)
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
            "logs_enabled": config.logs.enabled,
            "tracing_enabled": config.tracing.enabled,
        }
        entity = RCAJobDetailEntity(data=payload)
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询RCA任务失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"查询任务失败: {str(e)}") from e


@router.get(
    "/rca/metrics/detail",
    summary="获取指标列表",
    description="返回平台默认指标与 Prometheus 可用指标列表。",
    response_model=APIResponse,
)
async def list_available_metrics():
    """获取 Prometheus 可用指标与默认指标"""
    try:
        prom = PrometheusService()
        metrics = await prom.get_available_metrics()
        entity = RCAMetricsEntity(
            default_metrics=_ensure_str_list(config.rca.default_metrics),
            available_metrics=metrics,
            flags={
                "request_override": config.rca.request_override,
                "logs_enabled": config.logs.enabled,
                "tracing_enabled": config.tracing.enabled,
            },
        )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"获取可用指标失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"获取可用指标失败: {str(e)}"
        ) from e


@router.get(
    "/rca/topology/detail",
    summary="获取Kubernetes拓扑快照",
    description="获取指定命名空间的拓扑结构，以及可选的影响范围计算结果。",
    response_model=APIResponse,
)
async def list_topology(
    namespace: Optional[str] = Query(None, description="目标命名空间（可选）"),
    source: Optional[str] = Query(
        None, description="源节点名称，用于影响范围计算（可选）"
    ),
    max_hops: Optional[int] = Query(1, description="最大跳数（默认1）"),
    direction: Optional[str] = Query("out", description="边方向（out/in），默认out"),
):
    """获取指定命名空间的拓扑快照"""
    try:
        collector = K8sStateCollector(namespace=namespace)
        state = await collector.snapshot()
        graph = build_topology_from_state(state)
        topo = (
            graph.get_graph_data()
            if hasattr(graph, "get_graph_data")
            else graph.to_dict()
        )
        impact: Optional[list] = None
        if source:
            try:
                hops = max(0, min(int(max_hops or 1), 5))
                dirn = direction if direction in ("out", "in") else "out"
                impact = graph.reachable([source], max_hops=hops, direction=dirn)
            except Exception:
                impact = []
        entity = TopologySnapshotEntity(
            namespace=state.get("namespace"),
            counts={
                "pods": len(state.get("pods") or []),
                "deployments": len(state.get("deployments") or []),
                "services": len(state.get("services") or []),
            },
            topology=topo,
            impact_scope=impact,
            flags={
                "request_override": config.rca.request_override,
                "logs_enabled": config.logs.enabled,
                "tracing_enabled": config.tracing.enabled,
            },
        )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"获取拓扑失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取拓扑失败: {str(e)}") from e


@router.post(
    "/rca/anomalies/create",
    summary="创建异常检测",
    description="根据给定时间范围与指标执行异常检测并返回结果。",
    response_model=APIResponse,
)
async def create_anomaly_detection(req: AutoRCAAnomalyReq):
    """
    创建异常检测
    """
    try:
        # 验证时间范围
        start_time = req.start_time
        end_time = req.end_time
        metrics = req.metrics
        sensitivity = req.sensitivity if req.sensitivity is not None else 0.8

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

        entity = AnomalyDetectionEntity(
            anomalies=anomalies
            if isinstance(anomalies, dict)
            else {"anomalies": anomalies},
            detection_period={
                "start": start_time.isoformat(),
                "end": end_time.isoformat(),
            },
            sensitivity=sensitivity,
        )
        return APIResponse(
            code=0, message="异常检测完成", data=entity.model_dump()
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"异常检测失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"异常检测失败: {str(e)}") from e


@router.post(
    "/rca/correlations/create",
    summary="创建相关性分析",
    description="对目标指标计算与其他指标的相关性。",
    response_model=APIResponse,
)
async def create_correlation_analysis(req: AutoRCACorrelationReq):
    """
    创建相关性分析
    """
    try:
        # 验证时间范围
        start_time = req.start_time
        end_time = req.end_time
        target_metric = req.target_metric or ""
        metrics = req.metrics

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

        entity = CorrelationAnalysisEntity(
            target_metric=target_metric,
            correlations=correlations
            if isinstance(correlations, dict)
            else {"correlations": correlations},
            analysis_period={
                "start": start_time.isoformat(),
                "end": end_time.isoformat(),
            },
        )
        return APIResponse(
            code=0, message="相关性分析完成", data=entity.model_dump()
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"相关性分析失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"相关性分析失败: {str(e)}") from e


@router.post(
    "/rca/cross-correlations/create",
    summary="创建跨时滞相关分析",
    description="对多指标进行跨时滞相关性分析（Cross-Correlation）。",
    response_model=APIResponse,
)
async def create_cross_correlation(req: AutoRCACrossCorrelationReq):
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
        entity = RCAResultEntity(
            result=result if isinstance(result, dict) else {"result": result}
        )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"跨时滞相关分析失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"跨时滞相关分析失败: {str(e)}"
        ) from e


@router.post(
    "/rca/timelines/create",
    summary="创建事件时间线",
    description="基于事件与时间范围生成时间线。",
    response_model=APIResponse,
)
async def create_timeline(req: AutoRCATimelineReq):
    """
    创建事件时间线
    """
    try:
        # 验证时间范围
        start_time = req.start_time
        end_time = req.end_time
        events = req.events

        if not validate_time_range(start_time, end_time):
            raise HTTPException(status_code=400, detail="无效的时间范围")

        logger.info(f"生成事件时间线: {start_time} 到 {end_time}")

        # 调用时间线生成服务
        timeline = await rca_analyzer.generate_timeline(start_time, end_time, events)

        entity = TimelineEntity(
            timeline=timeline if isinstance(timeline, list) else [],
            period={"start": start_time.isoformat(), "end": end_time.isoformat()},
        )
        return APIResponse(
            code=0, message="事件时间线生成完成", data=entity.model_dump()
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"事件时间线生成失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"事件时间线生成失败: {str(e)}"
        ) from e


@router.get(
    "/rca/analyses/history/list",
    summary="获取分析历史记录",
    description="获取根因分析历史记录（支持分页与搜索）。",
    response_model=PaginatedListAPIResponse,
)
async def list_analysis_history(
    page: Optional[int] = Query(1, description="页码（从1开始）"),
    size: Optional[int] = Query(20, description="每页大小"),
    search: Optional[str] = Query(None, description="搜索关键词"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
    status: Optional[str] = Query(None, description="状态过滤"),
    start: Optional[str] = Query(None, description="起始时间(ISO8601)"),
    end: Optional[str] = Query(None, description="结束时间(ISO8601)"),
):
    """
    获取分析历史记录列表（支持分页和搜索）
    """
    try:
        logger.info(f"获取分析历史记录: page={page}, size={size}, search={search}")

        # 改为从数据库读取持久化历史，避免依赖内存
        with session_scope() as session:
            stmt = select(RCAAnalysisRecord).where(
                RCAAnalysisRecord.deleted_at.is_(None)
            )
            if namespace:
                stmt = stmt.where(RCAAnalysisRecord.namespace == namespace)
            # 时间范围过滤（基于 created_at）
            if start:
                try:
                    start_dt = datetime.fromisoformat(
                        start.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                    stmt = stmt.where(RCAAnalysisRecord.created_at >= start_dt)
                except Exception:
                    pass
            if end:
                try:
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")).replace(
                        tzinfo=None
                    )
                    stmt = stmt.where(RCAAnalysisRecord.created_at <= end_dt)
                except Exception:
                    pass
            rows = session.execute(stmt).scalars().all()
            if status:
                rows = [r for r in rows if (r.status == status)]
            history = [
                {
                    "id": r.id,
                    "name": f"RCA {r.id}",
                    "status": r.status,
                    "timestamp": (r.created_at.isoformat() if r.created_at else ""),
                    "type": "rca",
                    "summary": r.summary or "",
                }
                for r in rows
            ]

        # 应用分页和搜索（在name、status、type字段中搜索）
        paginated_history, total = process_list_with_pagination_and_search(
            items=history,
            page=page,
            size=size,
            search=search,
            search_fields=["name", "status", "type", "summary"],
        )

        return PaginatedListAPIResponse(
            code=0, message="分析历史记录获取成功", items=paginated_history, total=total
        ).model_dump()
    except Exception as e:
        logger.error(f"获取分析历史记录失败: {str(e)}")
        return PaginatedListAPIResponse(
            code=0, message="分析历史记录获取失败", items=[], total=0
        ).model_dump()


@router.get(
    "/rca/analyses/detail/{record_id}",
    summary="获取RCA记录详情",
    response_model=APIResponse,
)
async def get_rca_record(record_id: int):
    try:
        with session_scope() as session:
            rec = session.execute(
                select(RCAAnalysisRecord).where(
                    RCAAnalysisRecord.id == record_id,
                    RCAAnalysisRecord.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if not rec:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            entity = RCARecordEntity(
                id=rec.id,
                start_time=rec.start_time,
                end_time=rec.end_time,
                metrics=rec.metrics,
                namespace=rec.namespace,
                service_name=rec.service_name,
                status=rec.status,
                summary=rec.summary,
                created_at=rec.created_at.isoformat() if rec.created_at else None,
                updated_at=rec.updated_at.isoformat() if rec.updated_at else None,
            )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"获取RCA记录失败: {str(e)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


@router.delete(
    "/rca/analyses/delete/{record_id}",
    summary="软删除RCA记录",
    response_model=APIResponse,
)
async def delete_rca_record(record_id: int):
    try:
        with session_scope() as session:
            rec = session.execute(
                select(RCAAnalysisRecord).where(RCAAnalysisRecord.id == record_id)
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
    except Exception as e:
        logger.error(f"删除RCA记录失败: {str(e)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


@router.get(
    "/rca/health/detail",
    summary="RCA服务健康检查",
    description="检测RCA服务可用性与基础运行状态。",
    response_model=APIResponse,
)
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
                "timestamp": iso_utc_now(),
                "service": "rca",
                "flags": {
                    "request_override": config.rca.request_override,
                    "logs_enabled": config.logs.enabled,
                    "tracing_enabled": config.tracing.enabled,
                },
            },
        ).model_dump()

    except Exception as e:
        logger.error(f"RCA服务健康检查失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"RCA服务健康检查失败: {str(e)}"
        ) from e


## 兼容性端点 /rca/analyze/create 已移除，请使用 /rca/analyses/create


## 兼容性端点 /rca/correlate 已移除，请使用 /rca/correlations/create


## 兼容性端点 /rca/detect 已移除，请使用 /rca/anomalies/create
