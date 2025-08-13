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
import traceback
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.common.exceptions import AppError
from app.models.response_models import APIResponse
from app.utils.time_utils import iso_utc_now

logger = logging.getLogger("aiops.error_handler")

UTC_TZ = timezone.utc


def _safe_get_request_info(request: Request):
    """安全地获取请求信息，避免在错误处理过程中再次出错"""
    try:
        return {
            "url": str(request.url) if request else "unknown",
            "path": request.url.path if request else "unknown",
            "method": request.method if request else "unknown",
            "content_type": (
                request.headers.get("content-type", "unknown") if request else "unknown"
            ),
        }
    except Exception as e:
        logger.error(f"无法获取请求信息: {e}")
        return {
            "url": "error",
            "path": "error",
            "method": "error",
            "content_type": "error",
        }


def _create_error_response(code: int, message: str, extra_data: dict = None):
    """创建统一的错误响应（与APIResponse结构一致）"""
    try:
        data = {"timestamp": iso_utc_now()}

        if extra_data:
            data.update(extra_data)

        response = APIResponse(code=code, message=message, data=data)

        return JSONResponse(status_code=code, content=response.model_dump())

    except Exception as e:
        logger.error(f"创建错误响应时出错: {e}")
        # 返回最简单的错误响应，保持键名一致
        return JSONResponse(
            status_code=500,
            content=APIResponse(
                code=500,
                message="创建错误响应时发生内部错误",
                data={"timestamp": iso_utc_now()},
            ).model_dump(),
        )


async def http_exception_handler(request: Request, exc: HTTPException):
    """处理HTTP异常"""
    try:
        request_info = _safe_get_request_info(request)
        logger.warning(f"HTTP {exc.status_code}: {request_info['url']}")

        return _create_error_response(
            code=exc.status_code,
            message=exc.detail,
            extra_data={"path": request_info["path"]},
        )
    except Exception as handler_error:
        logger.error(f"HTTP异常处理器出错: {handler_error}")
        return _create_error_response(500, "处理HTTP异常时发生错误")


async def validation_exception_handler(request: Request, exc: Exception):
    """处理验证异常"""
    try:
        request_info = _safe_get_request_info(request)
        logger.warning(f"Validation error: {request_info['url']}")

        # 处理Pydantic验证错误
        if hasattr(exc, "errors"):
            errors = exc.errors()
            error_messages = []
            for error in errors:
                location = " -> ".join(str(loc) for loc in error.get("loc", []))
                message = error.get("msg", "Unknown validation error")
                error_messages.append(f"{location}: {message}")

            return _create_error_response(
                code=422,
                message="请求参数验证失败",
                extra_data={
                    "path": request_info["path"],
                    "validation_errors": error_messages,
                },
            )
        else:
            return _create_error_response(
                code=422,
                message=f"请求参数验证失败: {str(exc)}",
                extra_data={"path": request_info["path"]},
            )

    except Exception as handler_error:
        logger.error(f"验证异常处理器出错: {handler_error}")
        return _create_error_response(422, "处理验证异常时发生错误")


async def general_exception_handler(request: Request, exc: Exception):
    """处理一般异常"""
    try:
        error_id = datetime.now(UTC_TZ).strftime("%Y%m%d_%H%M%S")
        request_info = _safe_get_request_info(request)

        logger.error(f"Unexpected error [{error_id}]: {request_info['url']}")
        logger.error(f"Error type: {type(exc).__name__}")
        logger.error(f"Error message: {str(exc)}")
        logger.error(f"Traceback: {traceback.format_exc()}")

        # 获取配置信息，避免在错误处理中再次出错
        try:
            from app.config.settings import config

            is_debug = config.debug
        except Exception as config_error:
            logger.error(f"无法获取配置信息: {config_error}")
            is_debug = False

        # 在开发模式下返回详细错误信息
        if is_debug:
            extra_data = {
                "type": type(exc).__name__,
                "error_id": error_id,
                "path": request_info["path"],
                "traceback": traceback.format_exc().split("\n"),
            }
            return _create_error_response(
                500, f"意外错误（调试模式）: {str(exc)}", extra_data=extra_data
            )
        else:
            extra_data = {"error_id": error_id, "path": request_info["path"]}
            return _create_error_response(
                500, "服务器遇到意外错误，请联系管理员", extra_data=extra_data
            )

    except Exception as handler_error:
        logger.error(f"通用错误处理器出错: {handler_error}")
        error_id = datetime.now(UTC_TZ).strftime("%Y%m%d_%H%M%S")
        return _create_error_response(
            500, "服务器遇到严重错误", extra_data={"error_id": error_id}
        )


async def app_error_handler(request: Request, exc: AppError):
    """处理业务异常（应用内统一异常）。"""
    try:
        request_info = _safe_get_request_info(request)
        logger.warning(
            f"AppError {exc.status_code} at {request_info['path']}: {exc.message}"
        )
        return _create_error_response(
            code=exc.status_code, message=exc.message, extra_data={"path": request_info["path"]}
        )
    except Exception as handler_error:
        logger.error(f"业务异常处理器出错: {handler_error}")
        return _create_error_response(500, "处理业务异常时发生错误")


def setup_error_handlers(app):
    """设置错误处理器"""
    try:
        # 添加HTTP异常处理器
        app.add_exception_handler(HTTPException, http_exception_handler)
        # 添加业务异常处理器
        app.add_exception_handler(AppError, app_error_handler)

        # 添加验证异常处理器
        try:
            from fastapi.exceptions import RequestValidationError

            app.add_exception_handler(
                RequestValidationError, validation_exception_handler
            )
        except ImportError:
            # 如果没有RequestValidationError，使用pydantic的ValidationError
            try:
                from pydantic import ValidationError

                app.add_exception_handler(ValidationError, validation_exception_handler)
            except ImportError:
                logger.warning("无法导入验证异常类")

        # 添加通用异常处理器
        app.add_exception_handler(Exception, general_exception_handler)

        logger.info("错误处理器设置完成")

    except Exception as e:
        logger.error(f"错误处理器设置失败: {str(e)}")
