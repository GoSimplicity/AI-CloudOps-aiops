#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 错误处理中间件
"""

from datetime import datetime
import logging
import traceback
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.common.constants import HttpStatusCodes, ServiceConstants
from app.common.exceptions import AIOpsException
from app.config.settings import config
from app.models import BaseResponse

logger = logging.getLogger("aiops.error_handler")


async def custom_http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    """自定义HTTP异常处理器"""
    try:
        # 4xx 作为可预期客户端错误，使用 WARNING；其他仍为 ERROR
        is_client_error = (
            HttpStatusCodes.BAD_REQUEST
            <= exc.status_code
            < HttpStatusCodes.INTERNAL_SERVER_ERROR
        )

        log_method = logger.warning if is_client_error else logger.error
        log_method(
            f"HTTP异常处理器被触发 - 状态码: {exc.status_code}, 详情: {exc.detail}"
        )
        log_method(f"请求信息 - URL: {request.url}, Method: {request.method}")

        # 构建错误响应
        error_data = _build_error_data(request, exc.status_code, exc.detail)
        # api_response 转换的领域异常保留 error_code / details，前端据此区分错误类型
        error_code = getattr(exc, "error_code", None)
        if error_code:
            error_data["error_code"] = error_code
            error_details = getattr(exc, "error_details", None)
            if error_details and (exc.status_code < 500 or config.debug):
                error_data["details"] = error_details
        error_response = BaseResponse(
            code=exc.status_code,
            message=str(exc.detail) if exc.status_code < 500 or config.debug else ServiceConstants.INTERNAL_SERVER_ERROR_MESSAGE,
            data=error_data,
        )

        return JSONResponse(status_code=exc.status_code, content=error_response.dict())

    except Exception as e:
        logger.error(f"错误处理器本身出错: {str(e)}")
        logger.error(f"错误堆栈: {traceback.format_exc()}")

        return _create_fallback_response()


async def validation_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """验证异常处理器"""
    try:
        # 使用 WARNING 记录可预期的 400 校验错误
        # 提取原始错误列表以识别错误类型
        raw_errors = []
        try:
            if hasattr(exc, "errors"):
                raw_errors = exc.errors()  # type: ignore[attr-defined]
        except Exception:
            raw_errors = []

        # 规则：包含 extra_forbidden 或 missing/string_too_short 针对 assistant.query → 400；
        # 其他校验（如范围、类型）→ 422
        error_types = {e.get("type") for e in raw_errors if isinstance(e, dict)}
        has_extra = any(t and "extra_forbidden" in t for t in error_types)
        is_missing_or_empty = any(
            t in {"missing", "string_too_short"} for t in error_types
        )
        path_str = str(request.url.path)
        if path_str.endswith("/assistant/query") and (has_extra or is_missing_or_empty):
            status_code = HttpStatusCodes.BAD_REQUEST
        else:
            status_code = (
                HttpStatusCodes.BAD_REQUEST
                if has_extra
                else HttpStatusCodes.UNPROCESSABLE_ENTITY
            )

        details = _extract_validation_details(exc)

        logger.warning(f"验证异常: {details}")
        logger.warning(f"请求信息 - URL: {request.url}, Method: {request.method}")

        error_response = BaseResponse(
            code=status_code,
            message=ServiceConstants.VALIDATION_ERROR_MESSAGE,
            data=_build_error_data(request, status_code, details),
        )

        return JSONResponse(status_code=status_code, content=error_response.dict())

    except Exception as e:
        logger.error(f"验证异常处理器出错: {str(e)}")
        return _create_fallback_response(message="验证异常处理器错误", error=str(e))


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """通用异常处理器"""
    try:
        logger.error(f"未捕获的异常: {str(exc)}")
        logger.error(f"异常类型: {type(exc).__name__}")
        logger.error(f"请求信息 - URL: {request.url}, Method: {request.method}")
        logger.error(f"异常堆栈: {traceback.format_exc()}")

        error_data = _build_error_data(
            request, HttpStatusCodes.INTERNAL_SERVER_ERROR, str(exc)
        )
        if config.debug:
            error_data["type"] = type(exc).__name__

        error_response = BaseResponse(
            code=HttpStatusCodes.INTERNAL_SERVER_ERROR,
            message=ServiceConstants.INTERNAL_SERVER_ERROR_MESSAGE,
            data=error_data,
        )

        return JSONResponse(
            status_code=HttpStatusCodes.INTERNAL_SERVER_ERROR,
            content=error_response.dict(),
        )

    except Exception as handler_error:
        logger.critical(f"异常处理器本身出现严重错误: {str(handler_error)}")
        logger.critical(f"原异常: {str(exc)}")

        return _create_critical_fallback_response()


async def aiops_exception_handler(
    request: Request, exc: AIOpsException
) -> JSONResponse:
    """AIOps领域异常处理器"""
    try:
        from app.api.decorators import _get_http_status_for_aiops_exception

        status_code = _get_http_status_for_aiops_exception(exc)
        logger.warning(
            f"领域异常: {exc.error_code} - {exc.message} (status={status_code})"
        )
        logger.warning(f"请求信息 - URL: {request.url}, Method: {request.method}")

        # 构建包含 error_code 与 details 的标准错误数据
        error_data = _build_error_data(request, status_code, exc.details or exc.message)
        error_data["error_code"] = getattr(exc, "error_code", "AIOPS_ERROR")
        if getattr(exc, "details", None) and (status_code < 500 or config.debug):
            error_data["details"] = exc.details

        error_response = BaseResponse(
            code=status_code,
            message=exc.message if status_code < 500 or config.debug else ServiceConstants.INTERNAL_SERVER_ERROR_MESSAGE,
            data=error_data,
        )
        return JSONResponse(status_code=status_code, content=error_response.dict())

    except Exception as e:
        logger.error(f"AIOps异常处理器出错: {str(e)}")
        return _create_fallback_response(message="AIOps异常处理器错误", error=str(e))


def setup_error_handlers(app: FastAPI) -> None:
    """设置错误处理器"""
    try:
        # 注册HTTP异常处理器
        app.add_exception_handler(HTTPException, custom_http_exception_handler)
        app.add_exception_handler(StarletteHTTPException, custom_http_exception_handler)

        # 注册AIOps领域异常处理器
        app.add_exception_handler(AIOpsException, aiops_exception_handler)

        # 注册验证异常处理器
        app.add_exception_handler(ValidationError, validation_exception_handler)
        app.add_exception_handler(RequestValidationError, validation_exception_handler)

        # 注册通用异常处理器
        app.add_exception_handler(Exception, general_exception_handler)

        logger.info("错误处理器设置完成")

    except Exception as e:
        logger.error(f"错误处理器设置失败: {str(e)}")
        logger.error(f"错误堆栈: {traceback.format_exc()}")


def _extract_validation_details(exc: Exception) -> Any:
    """提取验证异常详情"""
    try:
        if isinstance(exc, RequestValidationError):
            errors = exc.errors()
            # 处理可能包含bytes的错误信息
            return _sanitize_error_details(errors)
        elif isinstance(exc, ValidationError):
            errors = exc.errors()
            return _sanitize_error_details(errors)
        else:
            return str(exc)
    except Exception as e:
        logger.warning(f"处理验证异常详情时出错: {e}")
        return "验证异常详情处理失败"


def _sanitize_error_details(errors):
    """清理错误详情，处理不可JSON序列化的对象"""
    try:
        import json

        def sanitize_value(obj):
            """递归清理对象中不可序列化的值"""
            if isinstance(obj, bytes):
                # 对于bytes对象，尝试解码或返回类型信息
                try:
                    if len(obj) > 200:  # 限制长度避免日志过长
                        return f"<bytes: {len(obj)} bytes, starts with: {obj[:100].decode('utf-8', errors='ignore')}...>"
                    return f"<bytes: {obj.decode('utf-8', errors='ignore')}>"
                except Exception:
                    return f"<bytes: {len(obj)} bytes>"
            elif isinstance(obj, dict):
                return {k: sanitize_value(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [sanitize_value(item) for item in obj]
            elif isinstance(obj, tuple):
                return tuple(sanitize_value(item) for item in obj)
            else:
                # 测试是否可以JSON序列化
                try:
                    json.dumps(obj)
                    return obj
                except (TypeError, ValueError):
                    return str(obj)

        return sanitize_value(errors)

    except Exception as e:
        logger.warning(f"清理错误详情时出错: {e}")
        return "错误详情清理失败"


def _build_error_data(
    request: Request, status_code: int, detail: Any
) -> Dict[str, Any]:
    """构建错误数据"""
    error_id = f"err_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    return {
        "error_id": error_id,
        "detail": detail if status_code < 500 or config.debug else "Internal server error",
        "timestamp": datetime.now().isoformat(),
        "path": str(request.url.path),
        "method": request.method,
        "status_code": status_code,
    }


def _create_fallback_response(
    message: str = ServiceConstants.INTERNAL_SERVER_ERROR_MESSAGE,
    error: str = "错误处理器异常",
) -> JSONResponse:
    """创建回退错误响应"""
    fallback_response = {
        "code": HttpStatusCodes.INTERNAL_SERVER_ERROR,
        "message": message,
        "data": {
            "timestamp": datetime.now().isoformat(),
            "error": error if config.debug else "Internal server error",
        },
    }
    return JSONResponse(
        status_code=HttpStatusCodes.INTERNAL_SERVER_ERROR,
        content=fallback_response,
    )


def _create_critical_fallback_response() -> JSONResponse:
    """创建严重错误的回退响应"""
    return JSONResponse(
        status_code=HttpStatusCodes.INTERNAL_SERVER_ERROR,
        content={
            "code": HttpStatusCodes.INTERNAL_SERVER_ERROR,
            "message": ServiceConstants.CRITICAL_ERROR_MESSAGE,
            "data": {
                "timestamp": datetime.now().isoformat(),
                "error": "异常处理器失败",
            },
        },
    )
