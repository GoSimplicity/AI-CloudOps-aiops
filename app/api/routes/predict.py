#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 负载预测API路由 - 提供QPS预测、实例数建议和负载趋势分析接口
"""

from fastapi import APIRouter, HTTPException, Body
from typing import Optional
import datetime
import logging
import asyncio
from app.core.prediction.predictor import PredictionService
from app.models.request_models import PredictionRequest
from app.models.response_models import PredictionResponse, APIResponse
from app.utils.validators import validate_qps

logger = logging.getLogger("aiops.predict")

router = APIRouter(tags=["prediction"])

# 初始化预测服务
prediction_service = PredictionService()

@router.post('/predict')
@router.get('/predict')
async def predict_instances(request_data: Optional[PredictionRequest] = Body(None)):
    """
    预测实例数接口
    """
    try:
        # 处理请求参数
        if request_data is None:
            # GET请求使用默认参数
            predict_request = PredictionRequest()
        else:
            predict_request = request_data

        # 验证QPS参数
        if predict_request.current_qps is not None:
            if not validate_qps(predict_request.current_qps):
                raise HTTPException(status_code=400, detail="QPS参数无效")

        logger.info(f"收到预测请求: QPS={predict_request.current_qps}, 时间={predict_request.timestamp}")

        # 调用预测服务
        try:
            prediction_result = await asyncio.to_thread(
                prediction_service.predict_instances,
                predict_request.current_qps,
                predict_request.timestamp
            )
        except Exception as predict_error:
            logger.error(f"预测服务调用失败: {str(predict_error)}")
            raise HTTPException(status_code=500, detail="预测失败，模型未加载或服务异常")

        # 检查预测结果
        if prediction_result is None:
            logger.error("预测服务返回空结果")
            raise HTTPException(status_code=500, detail="预测失败，模型未加载或服务异常")

        # 构建响应
        response = PredictionResponse(
            predicted_instances=prediction_result.get('predicted_instances', 0),
            confidence=prediction_result.get('confidence', 0.0),
            timestamp=predict_request.timestamp,
            qps=predict_request.current_qps,
            model_version=prediction_result.get('model_version', '1.0'),
            recommendation=prediction_result.get('recommendation', '建议维持当前实例数')
        )

        return APIResponse(code=0, message="预测成功", data=response.dict()).dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"预测请求处理失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"预测失败: {str(e)}")

@router.post('/predict/trend')
@router.get('/predict/trend')
async def predict_trend(hours_ahead: Optional[int] = 24, current_qps: Optional[float] = None):
    """
    负载趋势预测接口
    """
    try:
        # 验证参数
        if hours_ahead < 1 or hours_ahead > 168:
            raise HTTPException(status_code=400, detail="hours_ahead参数必须在1-168之间")

        if current_qps is not None and not validate_qps(current_qps):
            raise HTTPException(status_code=400, detail="QPS参数无效")

        logger.info(f"收到趋势预测请求: hours_ahead={hours_ahead}, current_qps={current_qps}")

        # 调用趋势预测服务
        try:
            result = await asyncio.to_thread(
                prediction_service.predict_trend,
                hours_ahead=hours_ahead,
                current_qps=current_qps
            )
        except Exception as predict_error:
            logger.error(f"趋势预测失败: {str(predict_error)}")
            raise HTTPException(status_code=500, detail="趋势预测失败")

        return APIResponse(code=0, message="趋势预测成功", data=result).dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"趋势预测请求处理失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"趋势预测失败: {str(e)}")

@router.post('/predict/models/reload')
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
                    "timestamp": datetime.datetime.utcnow().isoformat(),
                    "models_reloaded": True
                }
            ).dict()
        else:
            return APIResponse(
                code=500,
                message="模型重载失败",
                data={
                    "timestamp": datetime.datetime.utcnow().isoformat(),
                    "models_reloaded": False
                }
            ).dict()

    except Exception as e:
        logger.error(f"模型重载失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"模型重载失败: {str(e)}")

@router.get('/predict/info')
async def get_model_info():
    """
    获取预测模型信息接口
    """
    try:
        logger.info("收到模型信息请求")

        # 获取模型信息
        model_info = await asyncio.to_thread(prediction_service.get_model_info)

        return APIResponse(
            code=0,
            message="模型信息获取成功",
            data=model_info
        ).dict()

    except Exception as e:
        logger.error(f"获取模型信息失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取模型信息失败: {str(e)}")

@router.get('/predict/health')
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
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "service": "prediction"
            }
        ).dict()

    except Exception as e:
        logger.error(f"预测服务健康检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"预测服务健康检查失败: {str(e)}")