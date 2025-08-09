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
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Body, HTTPException

from app.core.prediction.predictor import PredictionService
from app.models.request_models import PredictionRequest
from app.models.response_models import APIResponse, PredictionResponse
from app.utils.validators import validate_qps

logger = logging.getLogger("aiops.predict")

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))

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
    prediction_result = await prediction_service.predict(
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
    return APIResponse(code=0, message="预测成功", data=response.model_dump()).model_dump()


 


 


@router.post("/predict")
async def predict_post(request_data: PredictionRequest):
    """统一预测接口（POST）"""
    try:
        return await _predict_internal(
            current_qps=request_data.current_qps,
            timestamp=request_data.timestamp,
            use_prom=getattr(request_data, "use_prom", False),
            metric=getattr(request_data, "metric", None),
            selector=getattr(request_data, "selector", None),
            window=getattr(request_data, "window", "1m") or "1m",
            interval_minutes=getattr(request_data, "interval_minutes", None),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"POST预测失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"预测失败: {str(e)}")


 


 


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
                    "timestamp": datetime.now(BEIJING_TZ).isoformat(),
                    "models_reloaded": True,
                },
            ).model_dump()
        else:
            return APIResponse(
                code=500,
                message="模型重载失败",
                data={
                    "timestamp": datetime.now(BEIJING_TZ).isoformat(),
                    "models_reloaded": False,
                },
            ).model_dump()

    except Exception as e:
        logger.error(f"模型重载失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"模型重载失败: {str(e)}")


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
        raise HTTPException(status_code=500, detail=f"获取模型信息失败: {str(e)}")


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
                "timestamp": datetime.now(BEIJING_TZ).isoformat(),
                "service": "prediction",
            },
        ).model_dump()

    except Exception as e:
        logger.error(f"预测服务健康检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"预测服务健康检查失败: {str(e)}")


 


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
        raise HTTPException(status_code=500, detail=f"趋势预测失败: {str(e)}")


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
        raise HTTPException(status_code=500, detail=f"模型验证失败: {str(e)}")
