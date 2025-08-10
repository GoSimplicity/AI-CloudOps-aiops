#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 负载预测API路由 - 提供统一的预测接口与趋势分析接口（已合并简/繁接口）
"""

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException

from app.core.prediction.predictor import PredictionService, Predictor
from app.db.base import session_scope
from app.db.models import PredictionRecord
from app.models.request_models import PredictionRequest
from app.models.response_models import APIResponse, PredictionResponse
from app.utils.time_utils import iso_utc_now
from app.utils.validators import validate_qps

logger = logging.getLogger("aiops.predict")



router = APIRouter(tags=["prediction"])

# 初始化预测服务
prediction_service = PredictionService()


async def _predict_internal(
    *,
    current_qps: Optional[float],
    timestamp: Optional[datetime],
    use_prom: bool,
    metric: Optional[str],
    selector: Optional[str],
    window: str,
    interval_minutes: Optional[int] = None,
):
    """统一的预测内部处理逻辑"""
    # 参数校验
    if current_qps is not None and not validate_qps(current_qps):
        raise HTTPException(status_code=400, detail="QPS参数无效")

    # 从 Prometheus 获取 QPS（可选）
    effective_qps = current_qps
    if use_prom:
        if not metric:
            raise HTTPException(status_code=400, detail="use_prom为true时必须提供metric")
        prom_qps = await prediction_service.get_qps_from_prometheus(
            metric=metric,
            selector=selector,
            window=window or "1m",
            timestamp=timestamp,
        )
        if prom_qps is not None:
            effective_qps = prom_qps

    # 执行预测
    prediction_result = await prediction_service.predict_async(
        current_qps=effective_qps,
        timestamp=timestamp,
        metric=metric,
        selector=selector,
        window=window or "1m",
    )

    response = PredictionResponse(
        instances=prediction_result.get("instances", 0),
        current_qps=prediction_result.get("current_qps", effective_qps or 50.0),
        timestamp=prediction_result.get("timestamp"),
        confidence=prediction_result.get("confidence", 0.0),
        model_version=prediction_result.get("model_version", "1.0"),
        prediction_type=prediction_result.get("prediction_type"),
        features=prediction_result.get("features"),
        schedule={"interval_minutes": interval_minutes} if interval_minutes else None,
    )
    # 持久化预测请求与结果
    try:
        with session_scope() as session:
            session.add(
                PredictionRecord(
                    current_qps=effective_qps,
                    input_timestamp=(timestamp.isoformat() if timestamp else None),
                    use_prom=use_prom,
                    metric=metric,
                    selector=selector,
                    window=window,
                    instances=response.instances,
                    confidence=response.confidence,
                    model_version=response.model_version,
                    prediction_type=response.prediction_type,
                    features=str(response.features) if response.features else None,
                    schedule_interval_minutes=(interval_minutes if interval_minutes else None),
                )
            )
    except Exception:
        pass

    return APIResponse(code=0, message="预测成功", data=response.model_dump()).model_dump()


 


 


@router.post("/predict")
async def predict_post(request_data: Dict[str, Any] = Body(..., description="预测请求参数")):
    """统一预测接口（POST），兼容简化格式与原有格式"""
    try:
        # 优先识别原有/增强格式（基于current_qps/use_prom等字段）
        recognized_keys = {
            "current_qps", "timestamp", "include_confidence", "use_prom",
            "metric", "selector", "window", "interval_minutes"
        }
        if set(request_data.keys()) & recognized_keys:
            try:
                pr = PredictionRequest(**request_data)
            except Exception as ex:
                raise HTTPException(status_code=422, detail="无效的请求参数") from ex
            return await _predict_internal(
                current_qps=pr.current_qps,
                timestamp=pr.timestamp,
                use_prom=pr.use_prom,
                metric=pr.metric,
                selector=pr.selector,
                window=pr.window or "1m",
                interval_minutes=pr.interval_minutes,
            )

        # 简化格式：必须包含 namespace、deployment、duration_minutes
        if "namespace" in request_data or "deployment" in request_data:
            namespace = (request_data.get("namespace") or "").strip()
            deployment = (request_data.get("deployment") or "").strip()
            duration = request_data.get("duration_minutes")
            if not namespace or not deployment or not isinstance(duration, int) or duration <= 0:
                raise HTTPException(status_code=422, detail="无效的参数：需要 namespace、deployment、duration_minutes>0")
            # 使用Prometheus工作负载指标估算当前QPS（通过同步API，便于测试mock）
            try:
                from app.services.prometheus import PrometheusService
                selector = f'namespace="{namespace}",deployment="{deployment}"'
                promql = f"sum(rate(http_requests_total{{{selector}}}[1m]))"
                end_dt = datetime.now(timezone.utc)
                start_dt = end_dt - timedelta(minutes=duration)
                prom = PrometheusService()
                result = prom.query_range(
                    query=promql,
                    start=str(int(start_dt.timestamp())),
                    end=str(int(end_dt.timestamp())),
                    step="60"
                )
                values = []
                if isinstance(result, dict):
                    data = result.get("data", {})
                    series = data.get("result", [])
                    if isinstance(series, list) and series:
                        first = series[0]
                        seq = first.get("values", []) if isinstance(first, dict) else []
                        for item in seq:
                            try:
                                _, v = item
                                values.append(float(v))
                            except Exception:
                                continue
                avg_qps = float(sum(values) / len(values)) if values else 0.0
            except Exception:
                avg_qps = 0.0
            replicas = max(1, math.ceil(avg_qps / 30.0))
            return APIResponse(code=0, message="预测成功", data={
                "predicted_replicas": int(replicas),
                "confidence": 0.6 if avg_qps > 1 else 0.3,
                "average_qps": avg_qps
            }).model_dump()

        # 其他情况：参数无法识别
        raise HTTPException(status_code=422, detail="无效的请求参数")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"POST预测失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"预测失败: {str(e)}") from e


@router.get("/predict")
async def predict_get(namespace: str, deployment: str, duration_minutes: int):
    try:
        if not namespace or not deployment or duration_minutes <= 0:
            raise HTTPException(status_code=422, detail="无效的参数")
        res = Predictor().predict(namespace=namespace, deployment=deployment, duration_minutes=duration_minutes)
        return APIResponse(code=0, message="预测成功", data=res).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"GET预测失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"预测失败: {str(e)}") from e


@router.post("/models/reload")
async def reload_models():
    """
    重新加载预测模型接口
    """
    try:
        logger.info("收到模型重载请求")

        # 调用模型重载服务
        reload_result = await asyncio.to_thread(prediction_service.reload_models)

        if reload_result:
            return APIResponse(
                code=0,
                message="模型重载成功",
                data={
                    "timestamp": iso_utc_now(),
                    "models_reloaded": True,
                },
            ).model_dump()
        else:
            return APIResponse(
                code=500,
                message="模型重载失败",
                data={
                    "timestamp": iso_utc_now(),
                    "models_reloaded": False,
                },
            ).model_dump()

    except Exception as e:
        logger.error(f"模型重载失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"模型重载失败: {str(e)}") from e


@router.get("/models/info")
async def get_model_info():
    """
    获取预测模型信息接口
    """
    try:
        logger.info("收到模型信息请求")

        # 获取模型信息
        model_info = await asyncio.to_thread(prediction_service.get_model_info)

        return APIResponse(
            code=0, message="模型信息获取成功", data=model_info
        ).model_dump()

    except Exception as e:
        logger.error(f"获取模型信息失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取模型信息失败: {str(e)}") from e


@router.get("/predict/health")
async def predict_health():
    """
    预测服务健康检查接口
    """
    try:
        # 检查预测服务健康状态
        health_status = await asyncio.to_thread(prediction_service.is_healthy)

        return APIResponse(
            code=0,
            message="预测服务健康检查完成",
            data={
                "healthy": health_status,
                "timestamp": iso_utc_now(),
                "service": "prediction",
            },
        ).model_dump()

    except Exception as e:
        logger.error(f"预测服务健康检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"预测服务健康检查失败: {str(e)}") from e


 


@router.post("/predict/trend")
async def trend_post(request_data: dict = Body(..., description="趋势预测请求参数")):
    """趋势预测（POST）"""
    try:
        hours_ahead = request_data.get("hours_ahead", 24)
        current_qps = request_data.get("current_qps")
        use_prom = request_data.get("use_prom", False)
        metric = request_data.get("metric")
        selector = request_data.get("selector")
        window = request_data.get("window", "1m")

        if hours_ahead < 1 or hours_ahead > 168:
            raise HTTPException(status_code=400, detail="hours_ahead参数必须在1-168之间")
        if current_qps is not None and not validate_qps(current_qps):
            raise HTTPException(status_code=400, detail="QPS参数无效")

        if use_prom:
            if not metric:
                raise HTTPException(status_code=400, detail="use_prom为true时必须提供metric")
            prom_qps = await prediction_service.get_qps_from_prometheus(metric, selector, window)
            if prom_qps is not None:
                current_qps = prom_qps

        result = await prediction_service.predict_trend(
            hours_ahead=hours_ahead,
            current_qps=current_qps,
            metric=metric,
            selector=selector,
            window=window,
        )
        return APIResponse(code=0, message="趋势预测成功", data=result).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"POST趋势预测失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"趋势预测失败: {str(e)}") from e


@router.get("/predict/trend")
async def predict_trend_get(namespace: Optional[str] = None, deployment: Optional[str] = None, metric: Optional[str] = None, hours: int = 24):
    try:
        result = await prediction_service.predict_trend(
            hours_ahead=hours,
            current_qps=None,
            metric=metric,
            selector=None,
            window="1m",
        )
        return APIResponse(code=0, message="趋势预测成功", data=result).model_dump()
    except Exception as e:
        logger.error(f"趋势预测失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"趋势预测失败: {str(e)}") from e


@router.get("/predict/model/info")
async def predict_model_info():
    try:
        predictor = Predictor()
        info = predictor.get_model_info()
        return APIResponse(code=0, message="模型信息获取成功", data=info).model_dump()
    except Exception as e:
        logger.error(f"获取模型信息失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取模型信息失败: {str(e)}") from e


@router.get("/predict/model/validate")
async def validate_model():
    """模型验证与状态检查"""
    try:
        model_loaded = prediction_service.model_loader.model is not None
        scaler_loaded = prediction_service.model_loader.scaler is not None
        # 在后台线程执行较重的验证逻辑
        validated = await asyncio.to_thread(prediction_service.model_loader.validate_model)
        model_version = prediction_service.model_loader.model_metadata.get("version", "1.0")

        return APIResponse(
            code=0,
            message="模型验证完成",
            data={
                "model_loaded": model_loaded,
                "scaler_loaded": scaler_loaded,
                "validated": bool(validated),
                "model_version": model_version,
            },
        ).model_dump()
    except Exception as e:
        logger.error(f"模型验证失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"模型验证失败: {str(e)}") from e
