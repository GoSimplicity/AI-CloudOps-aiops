#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 错误处理工具
"""

import asyncio
from datetime import datetime
from functools import wraps
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

from app.common.constants import HttpStatusCodes, Messages


class AICloudOpsError(Exception):
    """基础异常类"""

    def __init__(
        self,
        message: str,
        error_code: str = "UNKNOWN",
        details: Optional[Dict[str, Any]] = None,
    ):
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        self.timestamp = datetime.now()
        super().__init__(self.message)


class ValidationError(AICloudOpsError):
    """输入验证错误"""

    def __init__(
        self, message: str, field: Optional[str] = None, value: Optional[Any] = None
    ):
        details = {}
        if field:
            details["field"] = field
        if value is not None:
            details["invalid_value"] = str(value)
        super().__init__(message, "VALIDATION_ERROR", details)


class ServiceError(AICloudOpsError):
    """服务层错误"""

    def __init__(self, message: str, service: str, operation: Optional[str] = None):
        details = {"service": service}
        if operation:
            details["operation"] = operation
        super().__init__(message, "SERVICE_ERROR", details)


class ConfigurationError(AICloudOpsError):
    """配置错误"""

    def __init__(self, message: str, config_key: Optional[str] = None):
        details = {}
        if config_key:
            details["config_key"] = config_key
        super().__init__(message, "CONFIGURATION_ERROR", details)


class ExternalServiceError(AICloudOpsError):
    """外部服务错误"""

    def __init__(self, message: str, service: str, status_code: Optional[int] = None):
        details = {"external_service": service}
        if status_code:
            details["status_code"] = status_code
        super().__init__(message, "EXTERNAL_SERVICE_ERROR", details)


class ErrorHandler:
    """统一错误处理器"""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)

    def log_and_return_error(
        self, error: Exception, context: str, include_traceback: bool = True
    ) -> Tuple[str, Dict[str, Any]]:
        """记录错误并返回通用错误信息。include_traceback 保留给旧调用方，不再输出堆栈或原文。"""

        error_id = f"error_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

        error_details = {
            "error_id": error_id,
            "error_type": type(error).__name__,
            "context": context,
            "timestamp": datetime.now().isoformat(),
        }

        if isinstance(error, AICloudOpsError):
            error_details.update(
                {"error_code": error.error_code, "details": error.details}
            )

        self.logger.error(
            "[%s] %s，异常类型: %s",
            error_id,
            context,
            type(error).__name__,
        )

        return "内部服务错误", error_details

    def handle_validation_error(
        self, error: Exception, context: str = ""
    ) -> Tuple[Dict[str, Any], int]:
        """处理验证错误"""
        message, details = self.log_and_return_error(
            error, f"Validation error: {context}", False
        )

        return {
            "code": HttpStatusCodes.BAD_REQUEST,
            "message": Messages.ERROR_MESSAGES.get("invalid_input", message),
            "data": {},
            "error_details": details,
        }, HttpStatusCodes.BAD_REQUEST

    def handle_service_error(
        self, error: Exception, context: str = ""
    ) -> Tuple[Dict[str, Any], int]:
        """处理服务错误"""
        message, details = self.log_and_return_error(error, f"Service error: {context}")

        return {
            "code": HttpStatusCodes.INTERNAL_SERVER_ERROR,
            "message": Messages.ERROR_MESSAGES.get("internal_error", message),
            "data": {},
            "error_details": details,
        }, HttpStatusCodes.INTERNAL_SERVER_ERROR

    def handle_not_found_error(
        self, resource: str, identifier: str = ""
    ) -> Tuple[Dict[str, Any], int]:
        """处理资源未找到错误"""
        message = f"{resource} not found"
        if identifier:
            message += f": {identifier}"

        self.logger.warning(message)

        return {
            "code": HttpStatusCodes.NOT_FOUND,
            "message": Messages.ERROR_MESSAGES.get("not_found", message),
            "data": {},
            "error_details": {
                "resource": resource,
                "identifier": identifier,
                "timestamp": datetime.now().isoformat(),
            },
        }, HttpStatusCodes.NOT_FOUND


def error_handler(
    logger: Optional[logging.Logger] = None,
    return_exceptions: bool = False,
    default_return_value: Any = None,
) -> Callable:
    """错误处理装饰器"""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            handler = ErrorHandler(logger)
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                if return_exceptions:
                    return default_return_value
                error_msg, details = handler.log_and_return_error(
                    e, f"Function {func.__name__}"
                )
                raise ServiceError(error_msg, func.__name__)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            handler = ErrorHandler(logger)
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if return_exceptions:
                    return default_return_value
                error_msg, details = handler.log_and_return_error(
                    e, f"Function {func.__name__}"
                )
                raise ServiceError(error_msg, func.__name__)

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator


def retry_on_exception(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    logger: Optional[logging.Logger] = None,
) -> Callable:
    """重试装饰器"""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            _logger = logger or logging.getLogger(func.__module__)
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        _logger.error(
                            "Function %s failed after %s retries，异常类型: %s",
                            func.__name__,
                            max_retries,
                            type(e).__name__,
                        )
                        raise

                    retry_delay = delay * (backoff_factor**attempt)
                    _logger.warning(
                        "Function %s failed (attempt %s/%s), retrying in %ss，异常类型: %s",
                        func.__name__,
                        attempt + 1,
                        max_retries + 1,
                        retry_delay,
                        type(e).__name__,
                    )
                    await asyncio.sleep(retry_delay)

            raise last_exception

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            import time

            _logger = logger or logging.getLogger(func.__module__)
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        _logger.error(
                            "Function %s failed after %s retries，异常类型: %s",
                            func.__name__,
                            max_retries,
                            type(e).__name__,
                        )
                        raise

                    retry_delay = delay * (backoff_factor**attempt)
                    _logger.warning(
                        "Function %s failed (attempt %s/%s), retrying in %ss，异常类型: %s",
                        func.__name__,
                        attempt + 1,
                        max_retries + 1,
                        retry_delay,
                        type(e).__name__,
                    )
                    time.sleep(retry_delay)

            raise last_exception

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator


def validate_required_fields(data: Dict[str, Any], required_fields: List[str]) -> None:
    """验证必填字段是否存在"""
    missing_fields = []

    for field in required_fields:
        if field not in data or data[field] is None:
            missing_fields.append(field)

    if missing_fields:
        raise ValidationError(
            f"缺少必填字段: {', '.join(missing_fields)}",
            field=", ".join(missing_fields),
        )


def validate_field_type(
    data: Dict[str, Any], field: str, expected_type: Type, required: bool = True
) -> None:
    """验证字段类型是否正确"""
    if field not in data or data[field] is None:
        if required:
            raise ValidationError(f"缺少必填字段: {field}", field=field)
        return

    if not isinstance(data[field], expected_type):
        raise ValidationError(
            f"字段 {field} 类型应为 {expected_type.__name__}，当前为 {type(data[field]).__name__}",
            field=field,
            value=data[field],
        )


def validate_field_range(
    data: Dict[str, Any],
    field: str,
    min_value: Optional[Union[int, float]] = None,
    max_value: Optional[Union[int, float]] = None,
) -> None:
    """验证数值字段是否在指定范围内"""
    if field not in data or data[field] is None:
        return

    value = data[field]
    if not isinstance(value, (int, float)):
        raise ValidationError(
            f"字段 {field} 应为数值类型，当前为 {type(value).__name__}",
            field=field,
            value=value,
        )

    if min_value is not None and value < min_value:
        raise ValidationError(
            f"字段 {field} 值应大于等于 {min_value}，当前为 {value}",
            field=field,
            value=value,
        )

    if max_value is not None and value > max_value:
        raise ValidationError(
            f"字段 {field} 值应小于等于 {max_value}，当前为 {value}",
            field=field,
            value=value,
        )


def safe_cast(value: Any, target_type: Type, default: Any = None) -> Any:
    """安全地转换值类型"""
    try:
        return target_type(value)
    except (ValueError, TypeError):
        return default


class ContextualLogger:
    """上下文感知日志记录器"""

    def __init__(self, logger: logging.Logger, context: Dict[str, Any]):
        self.logger = logger
        self.context = context

    def _format_message(self, message: str) -> str:
        """格式化日志消息"""
        context_str = " ".join(f"{k}={v}" for k, v in self.context.items())
        return f"{message} [{context_str}]"

    def debug(self, message: str, **kwargs: Any) -> None:
        """输出调试级别日志"""
        self.logger.debug(self._format_message(message), **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        """输出信息级别日志"""
        self.logger.info(self._format_message(message), **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        """输出警告级别日志"""
        self.logger.warning(self._format_message(message), **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        """输出错误级别日志"""
        self.logger.error(self._format_message(message), **kwargs)

    def critical(self, message: str, **kwargs: Any) -> None:
        """输出严重错误级别日志"""
        self.logger.critical(self._format_message(message), **kwargs)


def create_contextual_logger(
    logger: logging.Logger, **context: Any
) -> ContextualLogger:
    """创建上下文感知日志记录器"""
    return ContextualLogger(logger, context)
