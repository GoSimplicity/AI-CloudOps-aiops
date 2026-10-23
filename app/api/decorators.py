#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
装饰器工具模块。

提供以下常用装饰器：
- 统一 API 响应封装
- 可选的请求参数校验
- 带 request_id 的请求/响应日志记录
"""

import functools
import logging
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request

from app.models import BaseResponse

from ..common.constants import HttpStatusCodes
from ..common.exceptions import AIOpsException

try:
    from app.common.logger import get_logger

    logger = get_logger("aiops.api.decorators")
except Exception:
    logger = logging.getLogger("aiops.api.decorators")


def api_response(operation_name: str = "操作") -> Callable:
    """将路由处理结果统一封装为 BaseResponse 结构。"""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Dict[str, Any]:
            try:
                result = await func(*args, **kwargs)

                # Always wrap raw results in new BaseResponse format
                if isinstance(result, BaseResponse):
                    return result.dict()
                return BaseResponse(
                    code=0, message=f"{operation_name}成功", data=result
                ).dict()

            except HTTPException:
                raise
            except AIOpsException as e:
                logger.error(f"{operation_name}失败: {e.message}")
                raise HTTPException(
                    status_code=_get_http_status_for_aiops_exception(e),
                    detail=e.message,
                )
            except Exception as e:
                logger.error(f"{operation_name}发生未处理异常: {str(e)}", exc_info=True)
                raise HTTPException(
                    status_code=HttpStatusCodes.INTERNAL_SERVER_ERROR,
                    detail=f"{operation_name}失败: 服务器内部错误",
                )

        return wrapper

    return decorator


def validate_request(validator_func: Optional[Callable[..., bool]] = None) -> Callable:
    """在执行路由处理器前进行参数校验。"""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Dict[str, Any]:
            if validator_func:
                validation_result = validator_func(*args, **kwargs)
                if validation_result is not True:
                    raise HTTPException(
                        status_code=HttpStatusCodes.BAD_REQUEST,
                        detail=f"请求验证失败: {validation_result}",
                    )

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def log_api_call(log_request: bool = True, log_response: bool = False) -> Callable:
    """记录请求与（可选）响应信息。"""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Dict[str, Any]:
            request = None
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
                    break

            if log_request and request:
                extra = {"request_id": getattr(request.state, "request_id", "")}
                logger.info(
                    f"API调用: {request.method} {request.url.path} "
                    f"- 客户端: {request.client.host if request.client else 'unknown'}",
                    extra=extra,
                )

            result = await func(*args, **kwargs)

            if log_response:
                if isinstance(result, dict):
                    status = result.get("code", "unknown")
                    message = result.get("message", "")
                    extra = (
                        {"request_id": getattr(request.state, "request_id", "")}
                        if request
                        else {}
                    )
                    logger.info(
                        f"API响应: {func.__name__} - 状态: {status}, 消息: {message}",
                        extra=extra,
                    )

            return result

        return wrapper

    return decorator


def _get_http_status_for_aiops_exception(exc: AIOpsException) -> int:
    """将领域异常映射为对应的 HTTP 状态码。"""
    from ..common.exceptions import (
        AssistantError,
        AutoFixError,
        ConfigurationError,
        ExternalServiceError,
        HistoricalDataError,
        PredictionError,
        RCAError,
        RequestTimeoutError,
        ResourceNotFoundError,
        ServiceUnavailableError,
        SessionAccessError,
        ValidationError,
        WorkflowApprovalError,
    )

    if isinstance(exc, (SessionAccessError, WorkflowApprovalError)):
        return exc.status_code
    if isinstance(exc, HistoricalDataError):
        return HttpStatusCodes.UNPROCESSABLE_ENTITY
    if isinstance(exc, ServiceUnavailableError):
        return HttpStatusCodes.SERVICE_UNAVAILABLE
    if isinstance(exc, ValidationError):
        return HttpStatusCodes.BAD_REQUEST
    if isinstance(exc, ResourceNotFoundError):
        return HttpStatusCodes.NOT_FOUND
    if isinstance(exc, ExternalServiceError):
        return HttpStatusCodes.BAD_GATEWAY
    # 服务域错误统一映射为 502/500 视为后端/模型错误
    if isinstance(exc, (PredictionError, RCAError, AutoFixError, AssistantError)):
        return HttpStatusCodes.BAD_GATEWAY
    if isinstance(exc, RequestTimeoutError):
        return HttpStatusCodes.GATEWAY_TIMEOUT
    if isinstance(exc, ConfigurationError):
        return HttpStatusCodes.INTERNAL_SERVER_ERROR
    return HttpStatusCodes.INTERNAL_SERVER_ERROR
