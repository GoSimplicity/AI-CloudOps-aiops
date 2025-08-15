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
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from sqlalchemy import select, func

from app.config.settings import config
from app.core.rca.analyzer import RCAAnalyzer
from app.core.rca.collectors.k8s_state_collector import K8sStateCollector
from app.core.rca.jobs.job_manager import RCAJobManager
from app.core.rca.topology.graph import build_topology_from_state
from app.db.base import session_scope
from app.db.models import (
    RCAAnalysisRecord,
    utcnow,
    RCAJobRecord,
    RCARecord,
    RCACorrelationRecord,
    RCASimpleCorrelationRecord,
)
from app.models.request_models import (
    AutoRCAAnalyzeReq,
    AutoRCACrossCorrelationReq,
    AutoRCACorrelationReq,
    AutoRCAAnomalyReq,
    AutoRCATimelineReq,
    RCARecordListReq,
)
from app.models.response_models import APIResponse, PaginatedListAPIResponse
from app.models.entities import (
    AnomalyDetectionEntity,
    RCAJobEntity,
    RCAJobDetailEntity,
    RCAMetricsEntity,
    TopologySnapshotEntity,
    RCARecordEntity,
    DeletionResultEntity,
)
from app.services.prometheus import PrometheusService

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


def _persist_rca_record(
    *,
    record_type: str,
    namespace: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    metrics: Optional[list] = None,
    params: Optional[dict] = None,
    status: str = "success",
    result: Optional[dict] = None,
    job_id: Optional[str] = None,
    summary: Optional[str] = None,
    error: Optional[str] = None,
):
    try:
        with session_scope() as session:
            rec = RCARecord(
                record_type=record_type,
                namespace=namespace,
                start_time=start_time.isoformat()
                if isinstance(start_time, datetime)
                else str(start_time)
                if start_time
                else None,
                end_time=end_time.isoformat()
                if isinstance(end_time, datetime)
                else str(end_time)
                if end_time
                else None,
                metrics=json.dumps(metrics or [], ensure_ascii=False)
                if metrics is not None
                else None,
                params_json=json.dumps(params or {}, ensure_ascii=False)
                if params is not None
                else None,
                job_id=job_id,
                status=status,
                summary=summary,
                result_json=json.dumps(result or {}, ensure_ascii=False)
                if result is not None
                else None,
                error=error,
            )
            session.add(rec)
    except Exception as e:
        logger.warning(f"持久化RCA记录失败（忽略）：{e}")


@router.get("/rca/records/list")
async def list_rca_records(params: RCARecordListReq = Depends()):
    try:
        with session_scope() as session:
            stmt = select(RCARecord).where(RCARecord.deleted_at.is_(None))
            if params.namespace:
                stmt = stmt.where(RCARecord.namespace == params.namespace)
            if params.status:
                stmt = stmt.where(RCARecord.status == params.status)
            if params.record_type:
                stmt = stmt.where(RCARecord.record_type == params.record_type)
            if params.job_id:
                stmt = stmt.where(RCARecord.job_id == params.job_id)
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
                    stmt.order_by(RCARecord.id.desc())
                    .offset((page - 1) * size)
                    .limit(size)
                )
                .scalars()
                .all()
            )
            items = []
            for r in rows:
                try:
                    result_obj = json.loads(r.result_json) if r.result_json else None
                except Exception:
                    result_obj = None
                try:
                    params_obj = (
                        json.loads(r.params_json)
                        if getattr(r, "params_json", None)
                        else None
                    )
                except Exception:
                    params_obj = None
                items.append(
                    RCARecordEntity(
                        id=r.id,
                        start_time=r.start_time or "",
                        end_time=r.end_time or "",
                        metrics=r.metrics,
                        namespace=r.namespace,
                        status=r.status,
                        summary=r.summary,
                        created_at=r.created_at.isoformat() if r.created_at else None,
                        updated_at=r.updated_at.isoformat() if r.updated_at else None,
                        record_type=getattr(r, "record_type", None),
                        job_id=getattr(r, "job_id", None),
                        params=params_obj,
                        result=result_obj,
                        error=r.error,
                    ).model_dump()
                )
        return APIResponse(
            code=0, message="ok", data={"items": items, "total": total}
        ).model_dump()
    except Exception as e:
        logger.error(f"list_rca_records 失败: {e}")
        return APIResponse(
            code=0, message="ok", data={"items": [], "total": 0}
        ).model_dump()


@router.get("/rca/records/detail/{record_id}")
async def get_rca_record_db(record_id: int):
    try:
        with session_scope() as session:
            r = session.get(RCARecord, record_id)
            if not r or r.deleted_at is not None:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            try:
                result_obj = json.loads(r.result_json) if r.result_json else None
            except Exception:
                result_obj = None
            try:
                params_obj = (
                    json.loads(r.params_json)
                    if getattr(r, "params_json", None)
                    else None
                )
            except Exception:
                params_obj = None
            entity = RCARecordEntity(
                id=r.id,
                start_time=r.start_time or "",
                end_time=r.end_time or "",
                metrics=r.metrics,
                namespace=r.namespace,
                status=r.status,
                summary=r.summary,
                created_at=r.created_at.isoformat() if r.created_at else None,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
                record_type=getattr(r, "record_type", None),
                job_id=getattr(r, "job_id", None),
                params=params_obj,
                result=result_obj,
                error=r.error,
            )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"get_rca_record 失败: {e}")
        raise HTTPException(status_code=500, detail="get record failed") from e


@router.delete("/rca/records/delete/{record_id}")
async def delete_rca_record_db(record_id: int):
    try:
        with session_scope() as session:
            r = session.get(RCARecord, record_id)
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
    summary="提交根因分析异步任务",
    description="提交根因分析异步任务，立即返回任务ID。",
    response_model=APIResponse,
)
async def create_root_cause_analysis(request_data: AutoRCAAnalyzeReq):
    """仅用于异步提交根因分析任务，返回 job_id。"""
    try:
        if job_manager is None:
            raise HTTPException(status_code=503, detail="异步任务服务未就绪")

        # 统一时间范围：若 end_time 在未来则截断到当前；若 start_time 缺失或无效则按 time_range_minutes 或默认回退
        now_utc = datetime.now(timezone.utc)
        start_time = request_data.start_time
        end_time = request_data.end_time or now_utc

        if end_time > now_utc:
            end_time = now_utc

        if start_time is None or start_time >= end_time:
            fallback_minutes = (
                request_data.time_range_minutes
                if request_data.time_range_minutes
                else config.rca.default_time_range
            )
            start_time = end_time - timedelta(minutes=fallback_minutes)

        # 统一限制：使用配置项控制最大允许时间跨度
        max_minutes = int(config.rca.max_time_range)
        # 若根据回退计算出的时间跨度超过上限，则截断
        actual_minutes = int((end_time - start_time).total_seconds() / 60)
        if actual_minutes > max_minutes:
            start_time = end_time - timedelta(minutes=max_minutes)
        if not validate_time_range(start_time, end_time, max_minutes):
            raise HTTPException(
                status_code=400,
                detail=f"无效的时间范围（超过{max_minutes}分钟或时间非法）",
            )

        # 验证指标列表格式
        if request_data.metrics and not validate_metric_list(request_data.metrics):
            raise HTTPException(status_code=400, detail="无效的指标列表")

        # 提交异步任务
        job_id = job_manager.submit_job(
            {
                "start_time": start_time,
                "end_time": end_time,
                "metrics": request_data.metrics,
                "namespace": request_data.namespace,
                "include_logs": request_data.include_logs,
            }
        )

        entity = RCAJobEntity(
            job_id=job_id,
            flags={
                "request_override": config.rca.request_override,
                "logs_enabled": config.logs.enabled,
                "tracing_enabled": False,
            },
        )
        # 入库：提交记录一条统一记录，状态为queued（与job表分离，便于统一查询）
        try:
            _persist_rca_record(
                record_type="analysis",
                namespace=request_data.namespace,
                start_time=start_time,
                end_time=end_time,
                metrics=request_data.metrics,
                params={
                    "include_logs": request_data.include_logs,
                },
                status="waiting",
                result=None,
                job_id=job_id,
            )
        except Exception:
            pass
        return APIResponse(
            code=0, message="任务已提交", data=entity.model_dump()
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"根因分析任务提交失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"提交失败: {str(e)}") from e


# 仅保留 RESTful 新接口


## 兼容性说明：原 /rca/jobs 提交端点已合并为 /rca/analyses/create


## 兼容性说明：原 /rca/jobs/detail/{job_id} 查询端点已下线


@router.get(
    "/rca/metrics/list",
    summary="获取指标列表",
    description="返回平台默认指标与 Prometheus 可用指标列表。",
    response_model=APIResponse,
)
async def list_available_metrics():
    """获取 Prometheus 可用指标与默认指标"""
    try:
        prom = PrometheusService()
        # 快速健康检查，避免Prometheus不可用时长时间等待
        available_metrics: list = []
        try:
            if prom.is_healthy() or prom.check_connectivity():
                available_metrics = await prom.get_available_metrics()
            else:
                logger.warning("Prometheus不可用，返回空可用指标列表")
        except Exception:
            available_metrics = []
        entity = RCAMetricsEntity(
            default_metrics=_ensure_str_list(config.rca.default_metrics),
            available_metrics=available_metrics,
            flags={
                "request_override": config.rca.request_override,
                "logs_enabled": config.logs.enabled,
                "tracing_enabled": False,
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
                "tracing_enabled": False,
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

        # 限制由配置控制
        max_minutes = int(config.rca.max_time_range)
        if not validate_time_range(start_time, end_time, max_minutes):
            raise HTTPException(
                status_code=400,
                detail=f"无效的时间范围（超过{max_minutes}分钟或时间非法）",
            )

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
        # 入库
        _persist_rca_record(
            record_type="anomaly",
            namespace=getattr(req, "namespace", None),
            start_time=start_time,
            end_time=end_time,
            metrics=metrics,
            params={"sensitivity": sensitivity},
            status="success",
            result=entity.model_dump(),
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
    summary="创建相关性分析（异步）",
    description="提交普通相关性分析任务，立即返回job_id。结果写入 cl_aiops_rca_correlations。",
    response_model=APIResponse,
)
async def create_correlation_analysis(req: AutoRCACorrelationReq):
    """创建普通相关性分析任务（异步）"""
    try:
        if job_manager is None:
            raise HTTPException(status_code=503, detail="异步任务服务未就绪")

        # 验证时间范围
        # 限制由配置控制
        max_minutes = int(config.rca.max_time_range)
        if not validate_time_range(req.start_time, req.end_time, max_minutes):
            raise HTTPException(
                status_code=400,
                detail=f"无效的时间范围（超过{max_minutes}分钟或时间非法）",
            )

        target_metric = (req.target_metric or "").strip() or None
        metrics = req.metrics or config.rca.default_metrics

        # 提交异步任务
        job_id = job_manager.submit_job(
            {
                "job_type": "correlation",
                "start_time": req.start_time,
                "end_time": req.end_time,
                "target_metric": target_metric,
                "metrics": metrics,
                "namespace": getattr(req, "namespace", None),
            }
        )

        entity = RCAJobEntity(
            job_id=job_id,
            flags={
                "request_override": config.rca.request_override,
                "logs_enabled": config.logs.enabled,
                "tracing_enabled": False,
            },
        )

        # 入库：统一记录表写入 waiting 状态
        try:
            _persist_rca_record(
                record_type="correlation",
                namespace=getattr(req, "namespace", None),
                start_time=req.start_time,
                end_time=req.end_time,
                metrics=metrics,
                params={"target_metric": target_metric},
                status="waiting",
                result=None,
                job_id=job_id,
            )
        except Exception:
            pass

        return APIResponse(
            code=0, message="任务已提交", data=entity.model_dump()
        ).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"提交相关性分析任务失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"提交相关性分析任务失败: {str(e)}"
        ) from e


@router.get(
    "/rca/correlations/list",
    summary="列出普通相关性分析异步任务（数据库）",
    description="从数据库返回普通相关性分析任务列表（分页，可按状态/命名空间过滤）",
    response_model=PaginatedListAPIResponse,
)
async def list_correlation_jobs(
    page: Optional[int] = Query(1, description="页码（从1开始)"),
    size: Optional[int] = Query(20, description="每页大小"),
    status: Optional[str] = Query(None, description="任务状态过滤"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
):
    try:
        with session_scope() as session:
            stmt = select(RCAJobRecord).where(RCAJobRecord.deleted_at.is_(None))
            # 仅筛选 correlation 任务，避免在分页后再做内存过滤导致看不到
            stmt = stmt.where(
                RCAJobRecord.params_json.like('%"job_type": "correlation"%')
            )
            if status:
                stmt = stmt.where(RCAJobRecord.status == status)
            if namespace:
                stmt = stmt.where(RCAJobRecord.namespace == namespace)
            total = (
                session.execute(
                    select(func.count()).select_from(stmt.subquery())
                ).scalar()
                or 0
            )
            page_num = max(1, int(page or 1))
            size_num = max(1, min(100, int(size or 20)))
            rows = (
                session.execute(
                    stmt.order_by(RCAJobRecord.id.desc())
                    .offset((page_num - 1) * size_num)
                    .limit(size_num)
                )
                .scalars()
                .all()
            )
            items = []
            for r in rows:
                try:
                    params_obj = json.loads(r.params_json) if r.params_json else {}
                except Exception:
                    params_obj = {}
                time_range = None
                try:
                    time_range = {
                        "start": params_obj.get("start_time"),
                        "end": params_obj.get("end_time"),
                    }
                except Exception:
                    time_range = None
                # 读取分表，若已有结果则将状态对外展示为 success，并标记 has_result
                effective_status = r.status
                has_result = bool(r.result_json)
                try:
                    if effective_status in {"waiting", "running"}:
                        row = session.execute(
                            select(RCASimpleCorrelationRecord).where(
                                RCASimpleCorrelationRecord.job_id == r.job_id,
                                RCASimpleCorrelationRecord.record_type == "correlation",
                                RCASimpleCorrelationRecord.deleted_at.is_(None),
                            )
                        ).scalar_one_or_none()
                        if row and row.result_json:
                            effective_status = "success"
                            has_result = True
                except Exception:
                    pass
                items.append(
                    {
                        "id": r.job_id,
                        "status": effective_status,
                        "progress": r.progress,
                        "namespace": r.namespace,
                        "time_range": time_range,
                        "created_at": r.created_at.isoformat()
                        if r.created_at
                        else None,
                        "updated_at": r.updated_at.isoformat()
                        if r.updated_at
                        else None,
                        "has_result": has_result,
                        "has_error": bool(r.error),
                    }
                )
        return PaginatedListAPIResponse(
            code=0, message="ok", items=items, total=total
        ).model_dump()
    except Exception as e:
        logger.error(f"列出相关性分析任务失败: {e}")
        return PaginatedListAPIResponse(
            code=0, message="ok", items=[], total=0
        ).model_dump()


@router.get(
    "/rca/correlations/detail/{job_id}",
    summary="获取普通相关性分析任务详情（数据库）",
    description="通过job_id从数据库查询任务状态与结果（如成功，返回 cl_aiops_rca_correlations 的结果）",
    response_model=APIResponse,
)
async def get_correlation_detail(job_id: str):
    try:
        with session_scope() as session:
            rec = session.execute(
                select(RCAJobRecord).where(RCAJobRecord.job_id == job_id)
            ).scalar_one_or_none()
            if not rec or rec.deleted_at is not None:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            try:
                params_obj = json.loads(rec.params_json) if rec.params_json else {}
            except Exception:
                params_obj = {}
            if (params_obj or {}).get("job_type") != "correlation":
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            minimal_running = rec.status in {"waiting", "running"}

            # 读取结果表（即使 running 也尝试，若已有结果则直接返回）
            corr_data = None
            try:
                row = session.execute(
                    select(RCASimpleCorrelationRecord).where(
                        RCASimpleCorrelationRecord.job_id == job_id,
                        RCASimpleCorrelationRecord.record_type == "correlation",
                        RCASimpleCorrelationRecord.deleted_at.is_(None),
                    )
                ).scalar_one_or_none()
                if row and row.result_json:
                    corr_data = json.loads(row.result_json)
                    # 自愈：若有结果但job仍是running，更新为success，避免查询时一直显示running
                    if rec.status == "running":
                        try:
                            rec.status = "success"
                            rec.progress = 1.0
                            session.add(rec)
                        except Exception:
                            pass
            except Exception:
                corr_data = None

            # 若已检测到结果存在，则对外返回 success 状态
            effective_status = "success" if corr_data is not None else rec.status
            data = {
                "id": rec.job_id,
                "status": effective_status,
                "progress": rec.progress,
                "namespace": rec.namespace,
                "params": None
                if (minimal_running and corr_data is None)
                else (json.loads(rec.params_json) if rec.params_json else None),
                "result": (
                    corr_data
                    if corr_data is not None
                    else (
                        None
                        if minimal_running
                        else (json.loads(rec.result_json) if rec.result_json else None)
                    )
                ),
                "error": rec.error if not minimal_running else None,
                "created_at": rec.created_at.isoformat() if rec.created_at else None,
                "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
            }
        entity = RCAJobDetailEntity(data=data)
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"获取相关性分析任务详情失败: {e}")
        raise HTTPException(status_code=500, detail="get job failed") from e


@router.post(
    "/rca/cross-correlations/create",
    summary="创建跨时滞相关分析",
    description="对多指标进行跨时滞相关性分析（Cross-Correlation）。",
    response_model=APIResponse,
)
async def create_cross_correlation(req: AutoRCACrossCorrelationReq):
    """创建跨时滞相关分析（异步）"""
    try:
        if job_manager is None:
            raise HTTPException(status_code=503, detail="异步任务服务未就绪")

        # 限制由配置控制
        max_minutes = int(config.rca.max_time_range)
        if not validate_time_range(req.start_time, req.end_time, max_minutes):
            raise HTTPException(
                status_code=400,
                detail=f"无效的时间范围（超过{max_minutes}分钟或时间非法）",
            )

        metrics = req.metrics or config.rca.default_metrics
        max_lags = min(max(1, int(req.max_lags or 10)), 20)

        # 提交异步任务
        job_id = job_manager.submit_job(
            {
                "job_type": "cross_correlation",
                "start_time": req.start_time,
                "end_time": req.end_time,
                "metrics": metrics,
                "namespace": getattr(req, "namespace", None),
                "max_lags": max_lags,
            }
        )

        entity = RCAJobEntity(
            job_id=job_id,
            flags={
                "request_override": config.rca.request_override,
                "logs_enabled": config.logs.enabled,
                "tracing_enabled": False,
            },
        )
        # 入库：统一记录表写入 waiting 状态
        try:
            _persist_rca_record(
                record_type="cross_correlation",
                namespace=getattr(req, "namespace", None),
                start_time=req.start_time,
                end_time=req.end_time,
                metrics=metrics,
                params={"max_lags": max_lags},
                status="waiting",
                result=None,
                job_id=job_id,
            )
        except Exception:
            pass

        return APIResponse(
            code=0, message="任务已提交", data=entity.model_dump()
        ).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"提交跨时滞相关任务失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"提交跨时滞相关任务失败: {str(e)}"
        ) from e


# 备注：跨时滞相关任务的列表与详情接口在文件末尾保留单一实现，避免重复定义


@router.post(
    "/rca/timelines/create",
    summary="创建事件时间线（异步）",
    description="提交时间线生成任务，立即返回job_id，结果可通过列表/详情接口查询。",
    response_model=APIResponse,
)
async def create_timeline(req: AutoRCATimelineReq):
    """提交时间线异步任务，返回 job_id。"""
    try:
        if job_manager is None:
            raise HTTPException(status_code=503, detail="异步任务服务未就绪")

        # 验证时间范围
        start_time = req.start_time
        end_time = req.end_time
        max_minutes = int(config.rca.max_time_range)
        if not validate_time_range(start_time, end_time, max_minutes):
            raise HTTPException(
                status_code=400,
                detail=f"无效的时间范围（超过{max_minutes}分钟或时间非法）",
            )

        # 提交异步任务（任务类型：timeline）
        job_id = job_manager.submit_job(
            {
                "job_type": "timeline",
                "start_time": start_time,
                "end_time": end_time,
                "events": req.events,
                "namespace": getattr(req, "namespace", None),
            }
        )

        entity = RCAJobEntity(
            job_id=job_id,
            flags={
                "request_override": config.rca.request_override,
                "logs_enabled": config.logs.enabled,
                "tracing_enabled": False,
            },
        )

        # 统一记录表写一条 waiting 记录，便于立即在列表可见
        try:
            _persist_rca_record(
                record_type="timeline",
                start_time=start_time,
                end_time=end_time,
                metrics=None,
                params={"events_count": len(req.events or [])},
                status="waiting",
                result=None,
                job_id=job_id,
            )
        except Exception:
            pass

        return APIResponse(code=0, message="任务已提交", data=entity.model_dump()).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"提交时间线任务失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"提交失败: {str(e)}") from e


@router.get(
    "/rca/analyses/list",
    summary="列出RCA异步任务（数据库）",
    description="从数据库返回RCA异步任务列表（分页，可按状态/命名空间过滤）",
    response_model=PaginatedListAPIResponse,
)
async def list_rca_analyses(
    page: Optional[int] = Query(1, description="页码（从1开始）"),
    size: Optional[int] = Query(20, description="每页大小"),
    status: Optional[str] = Query(None, description="任务状态过滤"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
):
    try:
        with session_scope() as session:
            stmt = select(RCAJobRecord).where(RCAJobRecord.deleted_at.is_(None))
            if status:
                stmt = stmt.where(RCAJobRecord.status == status)
            if namespace:
                stmt = stmt.where(RCAJobRecord.namespace == namespace)
            total = (
                session.execute(
                    select(func.count()).select_from(stmt.subquery())
                ).scalar()
                or 0
            )
            page_num = max(1, int(page or 1))
            size_num = max(1, min(100, int(size or 20)))
            rows = (
                session.execute(
                    stmt.order_by(RCAJobRecord.id.desc())
                    .offset((page_num - 1) * size_num)
                    .limit(size_num)
                )
                .scalars()
                .all()
            )
            items = []
            for r in rows:
                try:
                    params_obj = json.loads(r.params_json) if r.params_json else {}
                except Exception:
                    params_obj = {}
                time_range = None
                try:
                    time_range = {
                        "start": params_obj.get("start_time"),
                        "end": params_obj.get("end_time"),
                    }
                except Exception:
                    time_range = None
                # 读取统一结果表，若已有结果则将状态展示为 success，并标记 has_result
                effective_status = r.status
                has_result = bool(r.result_json)
                try:
                    if effective_status in {"waiting", "running"}:
                        row = (
                            session.execute(
                                select(RCARecord)
                                .where(
                                    RCARecord.job_id == r.job_id,
                                    RCARecord.record_type == "analysis",
                                    RCARecord.deleted_at.is_(None),
                                )
                                .order_by(RCARecord.id.desc())
                            )
                            .scalars()
                            .first()
                        )
                        if row and row.result_json:
                            effective_status = "success"
                            has_result = True
                except Exception:
                    pass
                items.append(
                    {
                        "id": r.job_id,
                        "status": effective_status,
                        "progress": r.progress,
                        "namespace": r.namespace,
                        "time_range": time_range,
                        "created_at": r.created_at.isoformat()
                        if r.created_at
                        else None,
                        "updated_at": r.updated_at.isoformat()
                        if r.updated_at
                        else None,
                        "has_result": has_result,
                        "has_error": bool(r.error),
                    }
                )
        return PaginatedListAPIResponse(
            code=0, message="ok", items=items, total=total
        ).model_dump()
    except Exception as e:
        logger.error(f"列出RCA任务失败: {e}")
        return PaginatedListAPIResponse(
            code=0, message="ok", items=[], total=0
        ).model_dump()


@router.get(
    "/rca/analyses/detail/{job_id}",
    summary="获取RCA异步任务详情",
    description="通过job_id从数据库查询任务状态与结果",
    response_model=APIResponse,
)
async def get_rca_analysis_detail(job_id: str):
    try:
        with session_scope() as session:
            rec = session.execute(
                select(RCAJobRecord).where(RCAJobRecord.job_id == job_id)
            ).scalar_one_or_none()
            if not rec or rec.deleted_at is not None:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()
            # 运行中仅返回必要信息，避免误以为已完成
            minimal_running = rec.status in {"waiting", "running"}
            # 若 job 表无结果，尝试从统一记录表补偿读取
            result_fallback = None
            if not rec.result_json:
                try:
                    row = (
                        session.execute(
                            select(RCARecord)
                            .where(
                                RCARecord.job_id == job_id,
                                RCARecord.record_type == "analysis",
                                RCARecord.deleted_at.is_(None),
                            )
                            .order_by(RCARecord.id.desc())
                        )
                        .scalars()
                        .first()
                    )
                    if row and row.result_json:
                        result_fallback = json.loads(row.result_json)
                        # 自愈：若有结果但job仍是running，更新为success
                        if rec.status == "running":
                            try:
                                rec.status = "success"
                                rec.progress = 1.0
                                session.add(rec)
                            except Exception:
                                pass
                except Exception:
                    result_fallback = None
            effective_status = (
                "success" if (result_fallback or rec.result_json) else rec.status
            )
            data = {
                "id": rec.job_id,
                "status": effective_status,
                "progress": rec.progress,
                "namespace": rec.namespace,
                "params": None
                if minimal_running
                else (json.loads(rec.params_json) if rec.params_json else None),
                "result": None
                if minimal_running
                else (
                    (json.loads(rec.result_json) if rec.result_json else None)
                    or result_fallback
                ),
                "error": rec.error if not minimal_running else None,
                "created_at": rec.created_at.isoformat() if rec.created_at else None,
                "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
            }
        entity = RCAJobDetailEntity(data=data)
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"获取RCA任务详情失败: {e}")
        raise HTTPException(status_code=500, detail="get job failed") from e


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
                    "tracing_enabled": False,
                },
            },
        ).model_dump()

    except Exception as e:
        logger.error(f"RCA服务健康检查失败: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"RCA服务健康检查失败: {str(e)}"
        ) from e


@router.get(
    "/rca/cross-correlations/list",
    summary="列出跨时滞相关任务（数据库）",
    description="从数据库返回跨时滞相关任务列表（分页，可按状态/命名空间过滤）",
    response_model=PaginatedListAPIResponse,
)
async def list_cross_correlation_jobs(
    page: Optional[int] = Query(1, description="页码（从1开始）"),
    size: Optional[int] = Query(20, description="每页大小"),
    status: Optional[str] = Query(None, description="任务状态过滤"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
):
    try:
        with session_scope() as session:
            stmt = select(RCAJobRecord).where(RCAJobRecord.deleted_at.is_(None))
            # 仅筛选 cross_correlation 任务：保存在 params_json 中
            stmt = stmt.where(
                RCAJobRecord.params_json.like('%"job_type": "cross_correlation"%')
            )
            if status:
                stmt = stmt.where(RCAJobRecord.status == status)
            if namespace:
                stmt = stmt.where(RCAJobRecord.namespace == namespace)
            total = (
                session.execute(
                    select(func.count()).select_from(stmt.subquery())
                ).scalar()
                or 0
            )
            page_num = max(1, int(page or 1))
            size_num = max(1, min(100, int(size or 20)))
            rows = (
                session.execute(
                    stmt.order_by(RCAJobRecord.id.desc())
                    .offset((page_num - 1) * size_num)
                    .limit(size_num)
                )
                .scalars()
                .all()
            )
            items = []
            for r in rows:
                try:
                    params_obj = json.loads(r.params_json) if r.params_json else {}
                except Exception:
                    params_obj = {}
                time_range = None
                try:
                    time_range = {
                        "start": params_obj.get("start_time"),
                        "end": params_obj.get("end_time"),
                    }
                except Exception:
                    time_range = None
                # 读取分表，若已有结果则将状态对外展示为 success，并标记 has_result
                effective_status = r.status
                has_result = bool(r.result_json)
                try:
                    if effective_status in {"waiting", "running"}:
                        row = session.execute(
                            select(RCACorrelationRecord).where(
                                RCACorrelationRecord.job_id == r.job_id,
                                RCACorrelationRecord.record_type == "cross_correlation",
                                RCACorrelationRecord.deleted_at.is_(None),
                            )
                        ).scalar_one_or_none()
                        if row and row.result_json:
                            effective_status = "success"
                            has_result = True
                except Exception:
                    pass
                items.append(
                    {
                        "id": r.job_id,
                        "status": effective_status,
                        "progress": r.progress,
                        "namespace": r.namespace,
                        "time_range": time_range,
                        "created_at": r.created_at.isoformat()
                        if r.created_at
                        else None,
                        "updated_at": r.updated_at.isoformat()
                        if r.updated_at
                        else None,
                        "has_result": has_result,
                        "has_error": bool(r.error),
                    }
                )
        return PaginatedListAPIResponse(
            code=0, message="ok", items=items, total=total
        ).model_dump()
    except Exception as e:
        logger.error(f"列出跨时滞相关任务失败: {e}")
        return PaginatedListAPIResponse(
            code=0, message="ok", items=[], total=0
        ).model_dump()


@router.get(
    "/rca/cross-correlations/detail/{job_id}",
    summary="获取跨时滞相关任务详情（数据库）",
    description="通过job_id获取任务状态与结果（如成功，返回 cl_aiops_rca_cross_correlations 的结果）",
    response_model=APIResponse,
)
async def get_cross_correlation_detail(job_id: str):
    try:
        with session_scope() as session:
            rec = session.execute(
                select(RCAJobRecord).where(RCAJobRecord.job_id == job_id)
            ).scalar_one_or_none()
            if not rec or rec.deleted_at is not None:
                return APIResponse(
                    code=404, message="not found", data=None
                ).model_dump()

            minimal_running = rec.status in {"waiting", "running"}
            # 尝试读取结果表（即使 running 也尝试）
            cc_data = None
            try:
                cc_row = session.execute(
                    select(RCACorrelationRecord).where(
                        RCACorrelationRecord.job_id == job_id,
                        RCACorrelationRecord.record_type == "cross_correlation",
                        RCACorrelationRecord.deleted_at.is_(None),
                    )
                ).scalar_one_or_none()
                if cc_row and cc_row.result_json:
                    cc_data = json.loads(cc_row.result_json)
                    # 自愈：若有结果但job仍是running，更新为success
                    if rec.status == "running":
                        try:
                            rec.status = "success"
                            rec.progress = 1.0
                            session.add(rec)
                        except Exception:
                            pass
            except Exception:
                cc_data = None

            effective_status = "success" if cc_data is not None else rec.status
            data = {
                "id": rec.job_id,
                "status": effective_status,
                "progress": rec.progress,
                "namespace": rec.namespace,
                "params": None
                if (minimal_running and cc_data is None)
                else (json.loads(rec.params_json) if rec.params_json else None),
                "result": (
                    cc_data
                    if cc_data is not None
                    else (
                        None
                        if minimal_running
                        else (json.loads(rec.result_json) if rec.result_json else None)
                    )
                ),
                "error": rec.error if not minimal_running else None,
                "created_at": rec.created_at.isoformat() if rec.created_at else None,
                "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
            }
        entity = RCAJobDetailEntity(data=data)
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"获取跨时滞相关任务详情失败: {e}")
        raise HTTPException(status_code=500, detail="get job failed") from e


@router.delete(
    "/rca/cross-correlations/delete/{record_id}",
    summary="软删除跨时滞相关结果记录",
    response_model=APIResponse,
)
async def delete_cross_correlation_record(record_id: int):
    try:
        with session_scope() as session:
            rec = session.get(RCACorrelationRecord, record_id)
            if not rec:
                return APIResponse(code=404, message="not found", data=None).model_dump()
            # 仅允许删除跨时滞相关的记录
            if getattr(rec, "record_type", None) != "cross_correlation":
                return APIResponse(code=400, message="invalid record type", data=None).model_dump()
            rec.deleted_at = utcnow()
            session.add(rec)
        entity = DeletionResultEntity(id=record_id)
        return APIResponse(code=0, message="deleted", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"删除跨时滞相关记录失败: {str(e)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


@router.delete(
    "/rca/correlations/delete/{record_id}",
    summary="软删除普通相关性结果记录",
    response_model=APIResponse,
)
async def delete_simple_correlation_record(record_id: int):
    try:
        with session_scope() as session:
            rec = session.get(RCASimpleCorrelationRecord, record_id)
            if not rec:
                return APIResponse(code=404, message="not found", data=None).model_dump()
            # 仅允许删除普通相关性的记录
            if getattr(rec, "record_type", None) != "correlation":
                return APIResponse(code=400, message="invalid record type", data=None).model_dump()
            rec.deleted_at = utcnow()
            session.add(rec)
        entity = DeletionResultEntity(id=record_id)
        return APIResponse(code=0, message="deleted", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"删除普通相关性记录失败: {str(e)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


@router.get(
    "/rca/timelines/list",
    summary="列出时间线记录",
    response_model=PaginatedListAPIResponse,
)
async def list_timelines(
    page: Optional[int] = Query(1, description="页码（从1开始）"),
    size: Optional[int] = Query(20, description="每页大小"),
    status: Optional[str] = Query(None, description="状态过滤"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
):
    try:
        with session_scope() as session:
            stmt = select(RCARecord).where(
                RCARecord.deleted_at.is_(None), RCARecord.record_type == "timeline"
            )
            if status:
                stmt = stmt.where(RCARecord.status == status)
            if namespace:
                stmt = stmt.where(RCARecord.namespace == namespace)
            total = (
                session.execute(select(func.count()).select_from(stmt.subquery())).scalar()
                or 0
            )
            page_num = max(1, int(page or 1))
            size_num = max(1, min(100, int(size or 20)))
            rows = (
                session.execute(
                    stmt.order_by(RCARecord.id.desc())
                    .offset((page_num - 1) * size_num)
                    .limit(size_num)
                )
                .scalars()
                .all()
            )
            items = []
            for r in rows:
                try:
                    result_obj = json.loads(r.result_json) if r.result_json else None
                except Exception:
                    result_obj = None
                try:
                    params_obj = json.loads(r.params_json) if r.params_json else None
                except Exception:
                    params_obj = None
                items.append(
                    RCARecordEntity(
                        id=r.id,
                        start_time=r.start_time or "",
                        end_time=r.end_time or "",
                        metrics=r.metrics,
                        namespace=r.namespace,
                        status=r.status,
                        summary=r.summary,
                        created_at=r.created_at.isoformat() if r.created_at else None,
                        updated_at=r.updated_at.isoformat() if r.updated_at else None,
                        record_type=getattr(r, "record_type", None),
                        job_id=getattr(r, "job_id", None),
                        params=params_obj,
                        result=result_obj,
                        error=r.error,
                    ).model_dump()
                )
        return PaginatedListAPIResponse(code=0, message="ok", items=items, total=total).model_dump()
    except Exception as e:
        logger.error(f"列出时间线记录失败: {e}")
        return PaginatedListAPIResponse(code=0, message="ok", items=[], total=0).model_dump()


@router.get(
    "/rca/timelines/detail/{record_id}",
    summary="获取时间线记录详情（支持ID或job_id）",
    response_model=APIResponse,
)
async def get_timeline_detail(record_id: str):
    try:
        with session_scope() as session:
            r = None
            # 兼容：既支持数值型ID，也支持传入job_id
            try:
                if str(record_id).isdigit():
                    rid = int(record_id)
                    r = session.get(RCARecord, rid)
                else:
                    r = (
                        session.execute(
                            select(RCARecord)
                            .where(
                                RCARecord.job_id == str(record_id),
                                RCARecord.record_type == "timeline",
                                RCARecord.deleted_at.is_(None),
                            )
                            .order_by(RCARecord.id.desc())
                        )
                        .scalars()
                        .first()
                    )
            except Exception:
                r = None
            if not r or r.deleted_at is not None or getattr(r, "record_type", None) != "timeline":
                return APIResponse(code=404, message="not found", data=None).model_dump()
            try:
                result_obj = json.loads(r.result_json) if r.result_json else None
            except Exception:
                result_obj = None
            try:
                params_obj = json.loads(r.params_json) if r.params_json else None
            except Exception:
                params_obj = None
            entity = RCARecordEntity(
                id=r.id,
                start_time=r.start_time or "",
                end_time=r.end_time or "",
                metrics=r.metrics,
                namespace=r.namespace,
                status=r.status,
                summary=r.summary,
                created_at=r.created_at.isoformat() if r.created_at else None,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
                record_type=getattr(r, "record_type", None),
                job_id=getattr(r, "job_id", None),
                params=params_obj,
                result=result_obj,
                error=r.error,
            )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"获取时间线详情失败: {e}")
        raise HTTPException(status_code=500, detail="get timeline failed") from e


@router.delete(
    "/rca/timelines/delete/{record_id}",
    summary="软删除时间线记录",
    response_model=APIResponse,
)
async def delete_timeline_record(record_id: int):
    try:
        with session_scope() as session:
            r = session.get(RCARecord, record_id)
            if not r:
                return APIResponse(code=404, message="not found", data=None).model_dump()
            if getattr(r, "record_type", None) != "timeline":
                return APIResponse(code=400, message="invalid record type", data=None).model_dump()
            r.deleted_at = utcnow()
            session.add(r)
        entity = DeletionResultEntity(id=record_id)
        return APIResponse(code=0, message="deleted", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"删除时间线记录失败: {e}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()
