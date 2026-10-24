#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 异常类定义模块
"""

from typing import Any, Dict, Optional


class AIOpsException(Exception):
    """AIOps平台基础异常类"""

    def __init__(
        self,
        message: str,
        error_code: str = "AIOPS_ERROR",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        super().__init__(self.message)


class SessionAccessError(AIOpsException):
    """会话身份、归属或存在性验证失败"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message=message, error_code="SESSION_ACCESS_ERROR")
        self.status_code = status_code


class WorkflowApprovalError(AIOpsException):
    """工作流审批归属、状态或容量不允许当前动作"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message=message, error_code="WORKFLOW_APPROVAL_ERROR")
        self.status_code = status_code


class FeatureDisabledError(AIOpsException):
    """功能被配置开关关闭（默认关闭的高风险入口）"""

    def __init__(self, message: str, switch: str, status_code: int = 403) -> None:
        super().__init__(message=message, error_code="AUTOFIX_FEATURE_DISABLED", details={"switch": switch})
        self.status_code = status_code


class NamespaceNotAllowedError(AIOpsException):
    """目标命名空间不在 autofix.allowed_namespaces 白名单内或属于受限命名空间"""

    def __init__(self, message: str, namespace: str, status_code: int = 403) -> None:
        super().__init__(
            message=message,
            error_code="AUTOFIX_NAMESPACE_NOT_ALLOWED",
            details={"namespace": namespace, "switch": "autofix.allowed_namespaces"},
        )
        self.status_code = status_code


class ServiceUnavailableError(AIOpsException):
    """服务不可用异常"""

    def __init__(
        self, service_name: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        super().__init__(
            message=f"服务 {service_name} 当前不可用",
            error_code="SERVICE_UNAVAILABLE",
            details=details,
        )


class ValidationError(AIOpsException):
    """数据验证异常"""

    def __init__(
        self, field: str, message: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        super().__init__(
            message=f"字段 {field} 验证失败: {message}",
            error_code="VALIDATION_ERROR",
            details=details,
        )


class PredictionError(AIOpsException):
    """预测服务异常"""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            message=f"预测服务错误: {message}",
            error_code="PREDICTION_ERROR",
            details=details,
        )


class HistoricalDataError(PredictionError):
    """历史数据不足、间断或查询返回多个序列，无法执行预测"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = "HISTORICAL_DATA_ERROR"


class RCAError(AIOpsException):
    """根因分析异常"""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            message=f"根因分析错误: {message}", error_code="RCA_ERROR", details=details
        )


class AutoFixError(AIOpsException):
    """自动修复异常"""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            message=f"自动修复错误: {message}",
            error_code="AUTOFIX_ERROR",
            details=details,
        )


class AssistantError(AIOpsException):
    """智能助手异常"""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            message=f"智能助手错误: {message}",
            error_code="ASSISTANT_ERROR",
            details=details,
        )


class ExternalServiceError(AIOpsException):
    """外部服务调用异常"""

    def __init__(
        self, service_name: str, message: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        super().__init__(
            message=f"外部服务 {service_name} 调用失败: {message}",
            error_code="EXTERNAL_SERVICE_ERROR",
            details=details,
        )


class ConfigurationError(AIOpsException):
    """配置错误异常"""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            message=f"配置错误: {message}",
            error_code="CONFIGURATION_ERROR",
            details=details,
        )


class ResourceNotFoundError(AIOpsException):
    """资源未找到异常"""

    def __init__(
        self,
        resource_type: str,
        resource_id: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=f"{resource_type} '{resource_id}' 未找到",
            error_code="RESOURCE_NOT_FOUND",
            details=details,
        )


class RequestTimeoutError(AIOpsException):
    """请求超时异常"""

    def __init__(
        self, message: str = "请求超时", details: Optional[Dict[str, Any]] = None
    ) -> None:
        super().__init__(
            message=message,
            error_code="REQUEST_TIMEOUT",
            details=details,
        )
