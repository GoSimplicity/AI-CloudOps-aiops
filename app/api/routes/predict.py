#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
import asyncio
import logging
import json
import time
from collections import OrderedDict
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from app.core.prediction.model_loader import ModelLoader
from app.core.prediction.predictor import PredictionService
from app.db.base import session_scope
from app.db.models import PredictionRecord, utcnow
from app.models.entities import DeletionResultEntity, PredictionRecordEntity
from app.models.request_models import AutoTrendReq, PredictionRecordListReq
from app.models.response_models import APIResponse
from app.utils.time_utils import iso_utc_now

logger = logging.getLogger("aiops.predict")



router = APIRouter(tags=["prediction"])

# 初始化预测服务（进程级单例）
prediction_service = PredictionService()

# 趋势结果缓存（内存级简易实现）：
# 1) 仅保存最近 N 条，避免内存无限增长
# 2) 方便实现 /trend/list 与 /trend/detail/{id} 的查询
MAX_TREND_ITEMS = 100
_trend_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def _save_trend_result(result: Dict[str, Any]) -> str:
    """保存趋势结果并返回可查询的ID。

    采用时间戳+自增序列的方式生成ID，避免重复；使用有序字典维护LRU语义。
    """
    unique_id = f"trend-{int(time.time()*1000)}-{len(_trend_cache)+1}"
    # 按LRU更新：若存在则先删除再插入到末尾
    if unique_id in _trend_cache:
        _trend_cache.pop(unique_id, None)
    _trend_cache[unique_id] = result
    # 超限淘汰最旧项
    while len(_trend_cache) > MAX_TREND_ITEMS:
        _trend_cache.popitem(last=False)
    return unique_id


## =============================
## 记录类接口
## =============================


 


@router.get("/predict/record/list")
async def list_prediction_records(params: PredictionRecordListReq = Depends()):
    """列出预测记录（分页+过滤）。

    仅保留查询能力以满足运营查看场景；创建/更新接口已按规划移除。
    """
    try:
        with session_scope() as session:
            stmt = select(PredictionRecord).where(PredictionRecord.deleted_at.is_(None))
            if params.metric:
                stmt = stmt.where(PredictionRecord.metric == params.metric)
            if params.model_version:
                stmt = stmt.where(PredictionRecord.model_version == params.model_version)
            if params.prediction_type:
                stmt = stmt.where(PredictionRecord.prediction_type == params.prediction_type)
            total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0
            page = max(1, int(params.page or 1))
            size = max(1, min(100, int(params.size or 20)))
            rows = session.execute(
                stmt.order_by(PredictionRecord.id.desc()).offset((page - 1) * size).limit(size)
            ).scalars().all()
            items = [
                PredictionRecordEntity(
                    id=r.id,
                    current_qps=r.current_qps,
                    input_timestamp=r.input_timestamp,
                    use_prom=r.use_prom,
                    metric=r.metric,
                    selector=r.selector,
                    window=r.window,
                    instances=r.instances,
                    confidence=r.confidence,
                    model_version=r.model_version,
                    prediction_type=r.prediction_type,
                    features=None,
                    schedule_interval_minutes=r.schedule_interval_minutes,
                    created_at=r.created_at.isoformat() if r.created_at else None,
                    updated_at=r.updated_at.isoformat() if r.updated_at else None,
                ).model_dump()
                for r in rows
            ]
        return APIResponse(code=0, message="ok", data={"items": items, "total": total}).model_dump()
    except Exception as e:
        logger.error(f"list_prediction_records 失败: {e}")
        return APIResponse(code=0, message="ok", data={"items": [], "total": 0}).model_dump()


@router.get("/predict/record/detail/{record_id}")
async def get_prediction_record(record_id: int):
    """获取预测记录详情。"""
    try:
        with session_scope() as session:
            r = session.get(PredictionRecord, record_id)
            if not r or r.deleted_at is not None:
                return APIResponse(code=404, message="not found", data=None).model_dump()
            entity = PredictionRecordEntity(
                id=r.id,
                current_qps=r.current_qps,
                input_timestamp=r.input_timestamp,
                use_prom=r.use_prom,
                metric=r.metric,
                selector=r.selector,
                window=r.window,
                instances=r.instances,
                confidence=r.confidence,
                model_version=r.model_version,
                prediction_type=r.prediction_type,
                features=None,
                schedule_interval_minutes=r.schedule_interval_minutes,
                created_at=r.created_at.isoformat() if r.created_at else None,
                updated_at=r.updated_at.isoformat() if r.updated_at else None,
            )
        return APIResponse(code=0, message="ok", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"get_prediction_record 失败: {e}")
        raise HTTPException(status_code=500, detail="get record failed") from e


@router.delete("/predict/record/delete/{record_id}")
async def delete_prediction_record(record_id: int):
    try:
        with session_scope() as session:
            r = session.get(PredictionRecord, record_id)
            if not r:
                return APIResponse(code=404, message="not found", data=None).model_dump()
            # 统一使用数据库模型提供的时区感知 UTC 时间，避免类型不一致
            r.deleted_at = utcnow()
            session.add(r)
        entity = DeletionResultEntity(id=record_id)
        return APIResponse(code=0, message="deleted", data=entity.model_dump()).model_dump()
    except Exception as e:
        logger.error(f"delete_prediction_record 失败: {e}")
        raise HTTPException(status_code=500, detail="delete record failed") from e

## =============================
## 模型类接口
## =============================


@router.post("/predict/model/refresh")
async def refresh_model():
    """刷新（重新加载）预测模型。

    为什么：按规划提供标准化的模型刷新入口，便于运维在热更新模型文件后无重启生效。
    """
    try:
        ok = await asyncio.to_thread(prediction_service.reload_models)
        return APIResponse(
            code=0 if ok else 500,
            message="模型重载成功" if ok else "模型重载失败",
            data={"timestamp": iso_utc_now(), "models_reloaded": bool(ok)},
        ).model_dump()
    except Exception as e:
        logger.error(f"模型重载失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"模型重载失败: {str(e)}") from e


@router.get("/predict/model/list")
async def list_models():
    """列出可用模型与当前加载状态。

    简化实现：基于 `ModelLoader.models` 注册表提供可用类型，同时返回当前加载器元数据。
    """
    try:
        loader: ModelLoader = prediction_service.model_loader
        available = [{"id": k, "type": v.get("type", k)} for k, v in loader.models.items()]
        info = loader.get_model_info()
        return APIResponse(code=0, message="ok", data={"available": available, "current": info}).model_dump()
    except Exception as e:
        logger.error(f"列出模型失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"列出模型失败: {str(e)}") from e


@router.get("/predict/model/detail/{model_id}")
async def model_detail(model_id: str):
    """获取指定模型的详情（占位实现）。

    为什么：按规划提供模型详情入口；由于未引入多模型切换，这里返回默认配置与注册表信息。
    """
    try:
        loader: ModelLoader = prediction_service.model_loader
        if model_id not in loader.models:
            return APIResponse(code=404, message="model not found", data=None).model_dump()
        default_cfg = loader.get_default_config(model_id)
        return APIResponse(
            code=0,
            message="ok",
            data={
                "id": model_id,
                "registered": loader.models.get(model_id),
                "default_config": default_cfg,
                "loader_info": loader.get_model_info(),
            },
        ).model_dump()
    except Exception as e:
        logger.error(f"获取模型详情失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取模型详情失败: {str(e)}") from e


## =============================
## 服务控制
## =============================


@router.post("/predict/reinitialize")
async def reinitialize_service():
    """重新初始化预测服务。

    为什么：提供与刷新不同层级的重置能力（包括内部状态），但为简单与可维护，复用 reload 实现。
    """
    try:
        ok = await asyncio.to_thread(prediction_service.reload_models)
        return APIResponse(
            code=0 if ok else 500,
            message="服务已重新初始化" if ok else "服务重新初始化失败",
            data={"timestamp": iso_utc_now(), "model_loaded": bool(ok)},
        ).model_dump()
    except Exception as e:
        logger.error(f"服务重新初始化失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"服务重新初始化失败: {str(e)}") from e


## =============================
## 健康检查
## =============================


@router.get("/predict/health")
async def predict_health():
    """预测服务健康检查。"""
    try:
        health_status = await asyncio.to_thread(prediction_service.is_healthy)
        return APIResponse(
            code=0,
            message="预测服务健康检查完成",
            data={"healthy": bool(health_status), "timestamp": iso_utc_now(), "service": "prediction"},
        ).model_dump()
    except Exception as e:
        logger.error(f"预测服务健康检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"预测服务健康检查失败: {str(e)}") from e


## =============================
## 趋势类接口
## =============================


@router.get("/predict/trend/list")
async def trend_list(params: AutoTrendReq = Depends()):
    """获取趋势预测列表（按请求参数即时生成并缓存）。

    设计动机：
    - 规划仅要求 list/detail，无需持久化；
    - 为便于 detail 查询，这里将本次生成结果以ID缓存至内存。
    """
    try:
        # 参数校验：hours_ahead 必须在 1-168 范围
        try:
            hours_ahead_raw = int(params.hours_ahead)
        except Exception:
            raise HTTPException(status_code=422, detail="hours_ahead参数无效")
        if hours_ahead_raw < 1 or hours_ahead_raw > 168:
            raise HTTPException(status_code=400, detail="hours_ahead参数必须在1-168之间")
        hours_ahead = hours_ahead_raw
        window = params.window or "1m"

        current_qps = params.current_qps
        if params.use_prom:
            if not params.metric:
                raise HTTPException(status_code=400, detail="use_prom为true时必须提供metric")
            prom_qps = await prediction_service.get_qps_from_prometheus(params.metric, params.selector, window)
            if prom_qps is not None:
                current_qps = prom_qps

        result = await prediction_service.predict_trend(
            hours_ahead=hours_ahead,
            current_qps=current_qps,
            metric=params.metric,
            selector=params.selector,
            window=window,
        )
        # 将本次趋势预测结果写入 cl_aiops_predictions 表（落库）
        try:
            from typing import Optional as _Optional

            with session_scope() as session:
                trend_points = result.get("trend_predictions") or []
                last_point = trend_points[-1] if isinstance(trend_points, list) and trend_points else None

                instances_val: _Optional[int] = None
                confidence_val: _Optional[float] = None
                if isinstance(last_point, dict):
                    pi = last_point.get("predicted_instances")
                    cf = last_point.get("confidence")
                    try:
                        instances_val = int(pi) if isinstance(pi, (int, float)) else None
                    except Exception:
                        instances_val = None
                    try:
                        confidence_val = float(cf) if isinstance(cf, (int, float)) else None
                    except Exception:
                        confidence_val = None

                session.add(
                    PredictionRecord(
                        current_qps=result.get("current_qps"),
                        input_timestamp=iso_utc_now(),
                        use_prom=bool(params.use_prom),
                        metric=params.metric,
                        selector=params.selector,
                        window=window,
                        instances=instances_val,
                        confidence=confidence_val,
                        model_version=prediction_service.model_loader.model_metadata.get("version", "1.0"),
                        prediction_type="trend",
                        features=json.dumps(result, ensure_ascii=False),
                        schedule_interval_minutes=None,
                    )
                )
        except Exception:
            # 数据库不可用时不影响接口可用性
            pass
        trend_id = _save_trend_result(result)

        items = [{
            "id": trend_id,
            "hours_ahead": result.get("hours_ahead"),
            "points": len(result.get("trend_predictions", []) or []),
            "analysis": result.get("trend_analysis"),
            "timestamp": result.get("timestamp"),
        }]
        return APIResponse(code=0, message="ok", data={"items": items, "total": len(items)}).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取趋势列表失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取趋势列表失败: {str(e)}") from e


@router.get("/predict/trend/detail/{trend_id}")
async def trend_detail(trend_id: str):
    """获取趋势预测详情。"""
    try:
        if trend_id not in _trend_cache:
            return APIResponse(code=404, message="trend not found", data=None).model_dump()
        result = _trend_cache[trend_id]
        return APIResponse(code=0, message="ok", data=result).model_dump()
    except Exception as e:
        logger.error(f"获取趋势详情失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取趋势详情失败: {str(e)}") from e
