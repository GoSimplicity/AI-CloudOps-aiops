#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 统一错误处理工具
"""

import asyncio
import logging
import time
from datetime import datetime
from functools import wraps
from typing import Any, Dict, Optional, Tuple, Type, Union



class AICloudOpsError(Exception):
    """AI-CloudOps 基础异常类"""

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
            details["value"] = str(value)
        super().__init__(message, "VALIDATION_ERROR", details)


class ServiceError(AICloudOpsError):
    """服务错误"""

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
        self, error: Exception, context: str
    ) -> Tuple[str, Dict[str, Any]]:
        """记录错误并返回格式化的错误信息"""
        error_id = f"error_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        
        error_details = {
            "error_id": error_id,
            "error_type": type(error).__name__,
            "context": context,
            "timestamp": datetime.now().isoformat(),
        }

        # 如果是自定义异常，添加额外信息
        if isinstance(error, AICloudOpsError):
            error_details.update(
                {"error_code": error.error_code, "details": error.details}
            )

        # 记录日志
        log_message = f"[{error_id}] {context}: {str(error)}"
        self.logger.error(log_message)

        return str(error), error_details

    # 统一的错误响应由 API 中间件处理；保留基础的日志封装以供内部服务使用


def retry_on_exception(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
):
    """重试装饰器"""
    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        raise
                    
                    wait_time = delay * (backoff_factor ** attempt)
                    await asyncio.sleep(wait_time)
            
            if last_exception:
                raise last_exception
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        raise
                    
                    wait_time = delay * (backoff_factor ** attempt)
                    time.sleep(wait_time)
            
            if last_exception:
                raise last_exception
        
        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper
    
    return decorator


def validate_field_range(
    value: Union[int, float],
    field_name: str,
    min_value: Optional[Union[int, float]] = None,
    max_value: Optional[Union[int, float]] = None,
) -> None:
    """验证字段范围"""
    if min_value is not None and value < min_value:
        raise ValidationError(f"字段 {field_name} 值 {value} 小于最小值 {min_value}")
    
    if max_value is not None and value > max_value:
        raise ValidationError(f"字段 {field_name} 值 {value} 大于最大值 {max_value}")
    