#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps智能预测服务API接口
"""

from datetime import datetime
import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException

from app.api.decorators import api_response, log_api_call
from app.common.constants import ErrorMessages, HttpStatusCodes
from app.common.exceptions import (
    AIOpsException,
    PredictionError,
    ServiceUnavailableError,
    ValidationError as DomainValidationError,
)
from app.models import (
    BaseResponse,
    CpuPredictionRequest,
    DiskPredictionRequest,
    MemoryPredictionRequest,
    ModelInfoResponse,
    PredictionResponse,
    QpsPredictionRequest,
    ServiceInfoResponse,
    ServiceReadyResponse,
)
from app.services.factory import ServiceFactory
from app.services.prediction_service import PredictionService

try:
    from app.common.logger import get_logger

    logger = get_logger("aiops.api.predict")
except Exception:
    logger = logging.getLogger("aiops.api.predict")

router = APIRouter(tags=["prediction"])
prediction_service = None


async def get_prediction_service() -> PredictionService:
    global prediction_service
    if prediction_service is None:
        prediction_service = await ServiceFactory.get_service(
            "prediction", PredictionService
        )
    return prediction_service


@router.post(
    "/qps",
    summary="AI-CloudOps QPS负载预测与实例建议",
    response_model=BaseResponse,
    responses={
        400: {
            "description": "包含不支持的参数",
            "content": {
                "application/json": {
                    "examples": {
                        "extra_field": {
                            "summary": "包含未定义字段时返回400",
                            "value": {
                                "code": 400,
                                "message": "包含不支持的参数: prediction_type_invalid",
                                "data": {
                                    "status_code": 400,
                                    "path": "/api/v1/predict/qps",
                                    "method": "POST",
                                    "detail": "包含不支持的参数: prediction_type_invalid",
                                    "timestamp": "2025-01-01T00:00:00",
                                },
                            },
                        }
                    }
                }
            },
        },
        422: {
            "description": "请求参数格式或范围错误",
            "content": {
                "application/json": {
                    "examples": {
                        "invalid_range": {
                            "summary": "current_qps 不在允许范围",
                            "value": {
                                "code": 422,
                                "message": "1 validation error for QpsPredictionRequest\ncurrent_qps\n  Input should be greater than 0 (type=greater_than, input_value=-10, gt=0.0)",
                                "data": {
                                    "status_code": 422,
                                    "path": "/api/v1/predict/qps",
                                    "method": "POST",
                                    "detail": "current_qps 应为大于 0 的数值",
                                    "timestamp": "2025-01-01T00:00:00",
                                },
                            },
                        }
                    }
                }
            },
        },
    },
)
@api_response("AI-CloudOps QPS负载预测与实例建议")
@log_api_call(log_request=True)
async def predict_qps(
    payload: Dict[str, Any] = Body(
        ...,
        examples={
            "default": {
                "value": {
                    "current_qps": 123.45,
                    "prediction_hours": 24,
                    "granularity": "hour",
                    "include_confidence": True,
                    "include_anomaly_detection": True,
                    "consider_historical_pattern": True,
                    "target_utilization": 0.7,
                    "sensitivity": 0.8,
                }
            }
        },
    ),
) -> Dict[str, Any]:
    """QPS负载预测"""
    logger.info("收到QPS预测请求")
    await (await get_prediction_service()).initialize()

    try:
        # 额外参数校验：禁止未定义字段，返回400（测试期望）
        allowed_keys = set(QpsPredictionRequest.model_fields.keys())
        extra_keys = set((payload or {}).keys()) - allowed_keys
        if extra_keys:
            raise HTTPException(
                status_code=HttpStatusCodes.BAD_REQUEST,
                detail=f"包含不支持的参数: {', '.join(sorted(extra_keys))}",
            )

        # 使用Pydantic进行严格字段与数值校验
        try:
            req = QpsPredictionRequest(**payload)
            logger.debug(f"QPS预测请求参数验证通过: current_qps={req.current_qps}, prediction_hours={req.prediction_hours}")
        except Exception as e:
            # 对数值范围等验证错误返回422
            logger.warning(f"QPS预测请求参数验证失败: {str(e)}")
            raise HTTPException(
                status_code=HttpStatusCodes.UNPROCESSABLE_ENTITY, detail=str(e)
            )

        # 延后初始化，避免无效请求触发服务逻辑
        await (await get_prediction_service()).initialize()

        logger.debug(f"开始执行QPS预测，使用AI增强: {req.enable_ai_insights}")
        if req.enable_ai_insights:
            prediction_result = await (
                await get_prediction_service()
            ).predict_with_ai_analysis(
                prediction_type="qps",
                current_value=req.current_qps,
                metric_query=req.metric_query,
                prediction_hours=req.prediction_hours,
                granularity=req.granularity.value,
                resource_constraints=(
                    req.resource_constraints.dict()
                    if req.resource_constraints
                    else None
                ),
                enable_ai_insights=req.enable_ai_insights,
                report_style=req.ai_report_style,
                target_utilization=req.target_utilization,
                sensitivity=req.sensitivity,
            )
        else:
            prediction_result = await (await get_prediction_service()).predict_qps(
                current_qps=req.current_qps,
                metric_query=req.metric_query,
                prediction_hours=req.prediction_hours,
                granularity=req.granularity.value,
                resource_constraints=(
                    req.resource_constraints.dict()
                    if req.resource_constraints
                    else None
                ),
                include_confidence=req.include_confidence,
                include_anomaly_detection=req.include_anomaly_detection,
                consider_historical_pattern=req.consider_historical_pattern,
                target_utilization=req.target_utilization,
                sensitivity=req.sensitivity,
            )

        response = PredictionResponse(**prediction_result)
        return response.dict()

    except (AIOpsException, DomainValidationError) as e:
        raise e
    except HTTPException as he:
        # 透传显式的HTTP错误（如不支持的参数 -> 400，Pydantic校验 -> 422）
        raise he
    except Exception as e:
        logger.error(f"QPS预测失败: {str(e)}")
        raise PredictionError(ErrorMessages.PREDICTION_SERVICE_ERROR)


@router.post(
    "/cpu",
    summary="AI-CloudOps CPU使用率预测与资源建议",
    response_model=BaseResponse,
)
@api_response("AI-CloudOps CPU使用率预测与资源建议")
@log_api_call(log_request=True)
async def predict_cpu(
    request: CpuPredictionRequest = Body(
        ...,
        examples={
            "default": {
                "value": {
                    "current_cpu_percent": 57.2,
                    "prediction_hours": 24,
                    "granularity": "hour",
                    "include_confidence": True,
                    "include_anomaly_detection": True,
                    "consider_historical_pattern": True,
                    "target_utilization": 0.7,
                    "sensitivity": 0.8,
                }
            }
        },
    ),
) -> Dict[str, Any]:
    """CPU使用率预测"""
    await (await get_prediction_service()).initialize()

    try:
        if request.enable_ai_insights:
            prediction_result = await (
                await get_prediction_service()
            ).predict_with_ai_analysis(
                prediction_type="cpu",
                current_value=request.current_cpu_percent,
                metric_query=request.metric_query,
                prediction_hours=request.prediction_hours,
                granularity=request.granularity.value,
                resource_constraints=(
                    request.resource_constraints.dict()
                    if request.resource_constraints
                    else None
                ),
                enable_ai_insights=request.enable_ai_insights,
                report_style=request.ai_report_style,
                target_utilization=request.target_utilization,
                sensitivity=request.sensitivity,
            )
        else:
            prediction_result = await (
                await get_prediction_service()
            ).predict_cpu_utilization(
                current_cpu_percent=request.current_cpu_percent,
                metric_query=request.metric_query,
                prediction_hours=request.prediction_hours,
                granularity=request.granularity.value,
                resource_constraints=(
                    request.resource_constraints.dict()
                    if request.resource_constraints
                    else None
                ),
                include_confidence=request.include_confidence,
                include_anomaly_detection=request.include_anomaly_detection,
                consider_historical_pattern=request.consider_historical_pattern,
                target_utilization=request.target_utilization,
                sensitivity=request.sensitivity,
            )

        response = PredictionResponse(**prediction_result)
        return response.dict()

    except (AIOpsException, DomainValidationError) as e:
        raise e
    except Exception as e:
        logger.error(f"CPU预测失败: {str(e)}")
        raise PredictionError(ErrorMessages.PREDICTION_SERVICE_ERROR)


@router.post(
    "/memory",
    summary="AI-CloudOps 内存使用率预测与资源建议",
    response_model=BaseResponse,
)
@api_response("AI-CloudOps 内存使用率预测与资源建议")
@log_api_call(log_request=True)
async def predict_memory(
    request: MemoryPredictionRequest = Body(
        ...,
        examples={
            "default": {
                "value": {
                    "current_memory_percent": 68.1,
                    "prediction_hours": 24,
                    "granularity": "hour",
                    "include_confidence": True,
                    "include_anomaly_detection": True,
                    "consider_historical_pattern": True,
                    "target_utilization": 0.7,
                    "sensitivity": 0.8,
                }
            }
        },
    ),
) -> Dict[str, Any]:
    """内存使用率预测"""
    await (await get_prediction_service()).initialize()

    try:
        if request.enable_ai_insights:
            prediction_result = await (
                await get_prediction_service()
            ).predict_with_ai_analysis(
                prediction_type="memory",
                current_value=request.current_memory_percent,
                metric_query=request.metric_query,
                prediction_hours=request.prediction_hours,
                granularity=request.granularity.value,
                resource_constraints=(
                    request.resource_constraints.dict()
                    if request.resource_constraints
                    else None
                ),
                enable_ai_insights=request.enable_ai_insights,
                report_style=request.ai_report_style,
                target_utilization=request.target_utilization,
                sensitivity=request.sensitivity,
            )
        else:
            prediction_result = await (
                await get_prediction_service()
            ).predict_memory_utilization(
                current_memory_percent=request.current_memory_percent,
                metric_query=request.metric_query,
                prediction_hours=request.prediction_hours,
                granularity=request.granularity.value,
                resource_constraints=(
                    request.resource_constraints.dict()
                    if request.resource_constraints
                    else None
                ),
                include_confidence=request.include_confidence,
                include_anomaly_detection=request.include_anomaly_detection,
                consider_historical_pattern=request.consider_historical_pattern,
                target_utilization=request.target_utilization,
                sensitivity=request.sensitivity,
            )

        response = PredictionResponse(**prediction_result)
        return response.dict()

    except (AIOpsException, DomainValidationError) as e:
        raise e
    except Exception as e:
        logger.error(f"内存预测失败: {str(e)}")
        raise PredictionError(ErrorMessages.PREDICTION_SERVICE_ERROR)


@router.post(
    "/disk",
    summary="AI-CloudOps 磁盘使用率预测与存储建议",
    response_model=BaseResponse,
)
@api_response("AI-CloudOps 磁盘使用率预测与存储建议")
@log_api_call(log_request=True)
async def predict_disk(
    request: DiskPredictionRequest = Body(
        ...,
        examples={
            "default": {
                "value": {
                    "current_disk_percent": 75.4,
                    "prediction_hours": 24,
                    "granularity": "hour",
                    "include_confidence": True,
                    "include_anomaly_detection": True,
                    "consider_historical_pattern": True,
                    "target_utilization": 0.7,
                    "sensitivity": 0.8,
                }
            }
        },
    ),
) -> Dict[str, Any]:
    """磁盘使用率预测"""
    await (await get_prediction_service()).initialize()

    try:
        if request.enable_ai_insights:
            prediction_result = await (
                await get_prediction_service()
            ).predict_with_ai_analysis(
                prediction_type="disk",
                current_value=request.current_disk_percent,
                metric_query=request.metric_query,
                prediction_hours=request.prediction_hours,
                granularity=request.granularity.value,
                resource_constraints=(
                    request.resource_constraints.dict()
                    if request.resource_constraints
                    else None
                ),
                enable_ai_insights=request.enable_ai_insights,
                report_style=request.ai_report_style,
                target_utilization=request.target_utilization,
                sensitivity=request.sensitivity,
            )
        else:
            prediction_result = await (
                await get_prediction_service()
            ).predict_disk_utilization(
                current_disk_percent=request.current_disk_percent,
                metric_query=request.metric_query,
                prediction_hours=request.prediction_hours,
                granularity=request.granularity.value,
                resource_constraints=(
                    request.resource_constraints.dict()
                    if request.resource_constraints
                    else None
                ),
                include_confidence=request.include_confidence,
                include_anomaly_detection=request.include_anomaly_detection,
                consider_historical_pattern=request.consider_historical_pattern,
                target_utilization=request.target_utilization,
                sensitivity=request.sensitivity,
            )

        response = PredictionResponse(**prediction_result)
        return response.dict()

    except (AIOpsException, DomainValidationError) as e:
        raise e
    except Exception as e:
        logger.error(f"磁盘预测失败: {str(e)}")
        raise PredictionError(ErrorMessages.PREDICTION_SERVICE_ERROR)


@router.get(
    "/health",
    summary="AI-CloudOps 预测服务健康检查",
    response_model=BaseResponse,
)
@api_response("AI-CloudOps 预测服务健康检查")
async def prediction_health() -> Dict[str, Any]:
    """兼容前端健康检查入口，复用就绪检查逻辑。"""
    return await prediction_ready()


@router.get(
    "/ready",
    summary="AI-CloudOps 预测服务就绪检查",
    response_model=BaseResponse,
)
@api_response("AI-CloudOps 预测服务就绪检查")
async def prediction_ready() -> Dict[str, Any]:
    """检查服务就绪状态"""
    try:
        await (await get_prediction_service()).initialize()

        is_initialized = (await get_prediction_service()).is_initialized()
        is_healthy = await (await get_prediction_service()).health_check()
        is_ready = is_initialized and is_healthy

        if not is_ready:
            raise ServiceUnavailableError("prediction")

        response = ServiceReadyResponse(
            ready=True,
            service="prediction",
            timestamp=datetime.now().isoformat(),
            message="服务就绪",
            initialized=is_initialized,
            healthy=is_healthy,
            status="ready",
        )
        return response.dict()
    except (AIOpsException, DomainValidationError):
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"就绪检查失败: {str(e)}")
        raise ServiceUnavailableError("prediction", {"error": str(e)})


@router.get(
    "/info",
    summary="AI-CloudOps 预测服务信息",
    response_model=BaseResponse,
)
@api_response("AI-CloudOps 预测服务信息")
async def prediction_info() -> Dict[str, Any]:
    from app.common.constants import AppConstants
    from app.config.settings import config

    info = {
        "service": "智能预测服务",
        "version": AppConstants.APP_VERSION,
        "description": "基于机器学习的多维度资源预测和智能扩缩容建议服务",
        "supported_prediction_types": [
            {
                "type": "qps",
                "name": "QPS负载预测",
                "description": "基于QPS预测负载趋势并提供实例数建议",
                "endpoint": "/qps",
            },
            {
                "type": "cpu",
                "name": "CPU使用率预测",
                "description": "基于CPU使用率预测资源需求并提供扩容建议",
                "endpoint": "/cpu",
            },
            {
                "type": "memory",
                "name": "内存使用率预测",
                "description": "基于内存使用率预测资源需求并提供扩容建议",
                "endpoint": "/memory",
            },
            {
                "type": "disk",
                "name": "磁盘使用率预测",
                "description": "基于磁盘使用率预测存储需求并提供扩容建议",
                "endpoint": "/disk",
            },
        ],
        "capabilities": [
            "多类型资源预测",
            "自定义指标支持",
            "智能扩缩容建议",
            "异常预测与检测",
            "成本优化分析",
            "置信区间计算",
            "历史模式识别",
            "AI增强预测分析",
            "智能洞察生成",
            "专业报告生成",
        ],
        "supported_granularity": ["minute", "hour", "day"],
        "endpoints": {
            "qps_predict": "/qps",
            "cpu_predict": "/cpu",
            "memory_predict": "/memory",
            "disk_predict": "/disk",
            "ready": "/predict/ready",
            "info": "/predict/info",
            "models": "/predict/models",
        },
        "prediction_features": {
            "algorithms": ["时间序列分析", "机器学习回归", "历史模式识别"],
            "confidence_calculation": "支持置信区间计算",
            "anomaly_detection": "内置异常检测能力",
            "resource_optimization": "智能资源配置建议",
            "cost_analysis": "成本优化分析",
            "custom_metrics": "支持自定义Prometheus指标查询",
            "ai_enhancement": {
                "description": "可通过enable_ai_insights字段启用AI增强功能",
                "features": [
                    "历史数据上下文分析",
                    "预测结果智能解读",
                    "AI洞察生成",
                    "专业报告生成",
                ],
                "report_styles": ["professional", "executive", "technical", "concise"],
                "fallback_support": "AI功能不可用时自动降级到基础预测",
            },
        },
        "constraints": {
            "min_hours": config.prediction.min_prediction_hours,
            "max_hours": config.prediction.max_prediction_hours,
            "qps_range": "0.1-10000.0",
            "utilization_range": "0-100%",
            "timeout_seconds": 120,
            "default_prediction_hours": config.prediction.default_prediction_hours,
            "default_granularity": config.prediction.default_granularity,
            "default_target_utilization": config.prediction.default_target_utilization,
            "default_sensitivity": config.prediction.default_sensitivity,
        },
        "service_status": "available" if prediction_service else "unavailable",
    }

    response = ServiceInfoResponse(
        service=info["service"],
        version=info["version"],
        description=info["description"],
        capabilities=info["capabilities"],
        endpoints=info["endpoints"],
        constraints=info["constraints"],
        status=info["service_status"],
    )

    data = response.dict()
    # 将 required 字段放在顶层数据对象内，api_response将包裹
    data["supported_prediction_types"] = info["supported_prediction_types"]
    return data


@router.get(
    "/models",
    summary="AI-CloudOps 模型信息",
    response_model=BaseResponse,
)
@api_response("AI-CloudOps 模型信息")
async def model_info() -> Dict[str, Any]:
    """获取预测服务中加载的模型详细信息"""
    try:
        await (await get_prediction_service()).initialize()
        model_details = await (await get_prediction_service()).get_model_info()

        response = ModelInfoResponse(
            models=model_details.get("models", []),
            total_models=model_details.get("total_models", 0),
            loaded_models=model_details.get("loaded_models", 0),
            status=model_details.get("status", "healthy"),
            timestamp=datetime.now().isoformat(),
        )
        data = response.dict()
        # 兼容测试期待字段
        data["models_loaded"] = data.get("loaded_models", 0)
        return data
    except (AIOpsException, DomainValidationError) as e:
        raise e
    except Exception as e:
        logger.error(f"获取模型信息失败: {str(e)}")
        raise PredictionError("获取模型信息失败")


__all__ = ["router"]
