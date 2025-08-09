#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 负载预测API路由 - 提供QPS预测、实例数建议和负载趋势分析接口
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from app.core.prediction.predictor import PredictionService
from app.models.request_models import PredictionRequest
from app.models.response_models import (APIResponse, PaginatedListAPIResponse,
                                        PredictionResponse)
from app.utils.validators import validate_qps

logger = logging.getLogger("aiops.predict")

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))

router = APIRouter(tags=["prediction"])

# 初始化预测服务
prediction_service = PredictionService()


@router.get("/predictions/list")
async def list_predictions(
    current_qps: Optional[float] = Query(None, description="当前QPS值", ge=0),
    timestamp: Optional[str] = Query(None, description="预测时间戳 (ISO格式)")
):
    """
    获取预测列表 (GET) - 使用查询参数
    
    Args:
        current_qps: 当前QPS值，默认为50.0
        timestamp: 预测时间戳，默认为当前时间
    
    Returns:
        预测结果，包含建议的实例数量
    """
    try:
        # 验证QPS参数
        if current_qps is not None and not validate_qps(current_qps):
            raise HTTPException(status_code=400, detail="QPS参数无效")

        # 解析时间戳
        parsed_timestamp = None
        if timestamp:
            try:
                parsed_timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            except ValueError:
                raise HTTPException(status_code=400, detail="时间戳格式无效，请使用ISO格式")

        logger.info(f"收到简单预测请求: QPS={current_qps}, 时间={timestamp}")

        # 调用预测服务
        prediction_result = await prediction_service.predict(
            current_qps=current_qps,
            timestamp=parsed_timestamp,
        )

        # 构建响应
        response = PredictionResponse(
            instances=prediction_result.get("instances", 0),
            current_qps=prediction_result.get("current_qps", current_qps or 50.0),
            timestamp=prediction_result.get("timestamp"),
            confidence=prediction_result.get("confidence", 0.0),
            model_version=prediction_result.get("model_version", "1.0"),
            prediction_type=prediction_result.get("prediction_type"),
            features=prediction_result.get("features"),
        )

        # 列表接口返回 items: []
        return PaginatedListAPIResponse(
            code=0,
            message="预测成功",
            items=[response.model_dump()],
            total=1,
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"简单预测请求处理失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"预测失败: {str(e)}")


 


@router.get("/predictions")
async def get_prediction(
    current_qps: Optional[float] = Query(None, description="当前QPS值", ge=0),
    timestamp: Optional[str] = Query(None, description="预测时间戳 (ISO格式)"),
    use_prom: bool = Query(False, description="是否从Prometheus读取QPS"),
    metric: Optional[str] = Query(None, description="Prometheus指标名，如http_requests_total"),
    selector: Optional[str] = Query(None, description="Prometheus标签选择器，如job=\"svc\""),
    window: str = Query("1m", description="Prometheus速率窗口，如1m/5m"),
):
    """RESTful 简单预测（GET）"""
    try:
        # 参数校验
        if current_qps is not None and not validate_qps(current_qps):
            raise HTTPException(status_code=400, detail="QPS参数无效")

        parsed_timestamp = None
        if timestamp:
            try:
                parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=400, detail="时间戳格式无效，请使用ISO格式")

        # 从Prometheus拉取QPS（可选）
        if use_prom:
            if not metric:
                raise HTTPException(status_code=400, detail="use_prom为true时必须提供metric")
            prom_qps = await prediction_service.get_qps_from_prometheus(
                metric=metric,
                selector=selector,
                window=window,
                timestamp=parsed_timestamp,
            )
            if prom_qps is not None:
                current_qps = prom_qps

        # 执行预测
        prediction_result = await prediction_service.predict(
            current_qps=current_qps, timestamp=parsed_timestamp
        )

        response = PredictionResponse(
            instances=prediction_result.get("instances", 0),
            current_qps=prediction_result.get("current_qps", current_qps or 50.0),
            timestamp=prediction_result.get("timestamp"),
            confidence=prediction_result.get("confidence", 0.0),
            model_version=prediction_result.get("model_version", "1.0"),
            prediction_type=prediction_result.get("prediction_type"),
            features=prediction_result.get("features"),
        )

        return APIResponse(code=0, message="预测成功", data=response.model_dump()).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"RESTful GET预测失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"预测失败: {str(e)}")


@router.post("/predictions")
async def create_prediction_rest(request_data: PredictionRequest):
    """RESTful 复杂预测（POST）"""
    try:
        if request_data.current_qps is not None:
            if not validate_qps(request_data.current_qps):
                raise HTTPException(status_code=400, detail="QPS参数无效")

        prediction_result = await prediction_service.predict(
            current_qps=request_data.current_qps,
            timestamp=request_data.timestamp,
        )

        response = PredictionResponse(
            instances=prediction_result.get("instances", 0),
            current_qps=prediction_result.get("current_qps", request_data.current_qps or 50.0),
            timestamp=prediction_result.get(
                "timestamp", (request_data.timestamp or datetime.now(BEIJING_TZ)).isoformat()
            ),
            confidence=prediction_result.get("confidence", 0.0),
            model_version=prediction_result.get("model_version", "1.0"),
            prediction_type=prediction_result.get("prediction_type"),
            features=prediction_result.get("features"),
        )

        return APIResponse(code=0, message="预测成功", data=response.model_dump()).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"RESTful POST预测失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"预测失败: {str(e)}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"复杂预测请求处理失败: {str(e)}")
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


@router.get("/predictions/health")
async def predictions_health():
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


@router.get("/predictions/trends")
async def get_trend_predictions(
    hours_ahead: int = Query(24, description="预测未来小时数", ge=1, le=168),
    current_qps: Optional[float] = Query(None, description="当前QPS值", ge=0),
    use_prom: bool = Query(False, description="是否从Prometheus读取QPS"),
    metric: Optional[str] = Query(None, description="Prometheus指标名"),
    selector: Optional[str] = Query(None, description="Prometheus标签选择器"),
    window: str = Query("1m", description="Prometheus速率窗口"),
):
    """RESTful 简单趋势预测（GET）"""
    try:
        if current_qps is not None and not validate_qps(current_qps):
            raise HTTPException(status_code=400, detail="QPS参数无效")

        if use_prom:
            if not metric:
                raise HTTPException(status_code=400, detail="use_prom为true时必须提供metric")
            prom_qps = await prediction_service.get_qps_from_prometheus(metric, selector, window)
            if prom_qps is not None:
                current_qps = prom_qps

        result = await prediction_service.predict_trend(hours_ahead=hours_ahead, current_qps=current_qps)
        return APIResponse(code=0, message="趋势预测成功", data=result).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"RESTful GET趋势预测失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"趋势预测失败: {str(e)}")


@router.post("/predictions/trends")
async def create_trend_predictions(request_data: dict = Body(..., description="趋势预测请求参数")):
    """RESTful 复杂趋势预测（POST）"""
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

        result = await prediction_service.predict_trend(hours_ahead=hours_ahead, current_qps=current_qps)
        return APIResponse(code=0, message="趋势预测成功", data=result).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"RESTful POST趋势预测失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"趋势预测失败: {str(e)}")


 
