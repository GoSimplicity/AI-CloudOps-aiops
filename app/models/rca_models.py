#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 根因分析模型定义
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class SeverityLevel(str, Enum):
    """严重程度级别"""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DataSourceType(str, Enum):
    """数据源类型"""

    METRICS = "metrics"
    EVENTS = "events"
    LOGS = "logs"


@dataclass
class CorrelationResult:
    """关联分析结果"""

    confidence: float  # 置信度 0-1
    correlation_type: str  # 关联类型
    evidence: List[str]  # 证据列表
    timeline: List[Dict[str, Any]]  # 时间线


@dataclass
class RootCause:
    """根因结果"""

    cause_type: str  # 根因类型
    description: str  # 描述
    confidence: float  # 置信度
    affected_components: List[str]  # 受影响组件
    evidence: Dict[str, Any]  # 证据
    recommendations: List[str]  # 建议


@dataclass
class MetricData:
    """指标数据模型"""

    name: str  # 指标名称
    values: List[Dict[str, Any]]  # 时间序列值 [{timestamp, value}]
    labels: Dict[str, str]  # 标签
    anomaly_score: float = 0.0  # 异常分数 (0-1)
    trend: str = "stable"  # 趋势: increasing, decreasing, stable

    def get_latest_value(self) -> Optional[float]:
        """获取最新值"""
        if self.values:
            return self.values[-1].get("value")
        return None

    def get_average_value(self) -> float:
        """计算平均值"""
        if not self.values:
            return 0.0
        values = [v.get("value", 0) for v in self.values]
        return sum(values) / len(values)


@dataclass
class EventData:
    """事件数据模型"""

    timestamp: datetime  # 事件时间
    type: str  # 事件类型 (Normal, Warning)
    reason: str  # 事件原因
    message: str  # 事件消息
    involved_object: Dict[str, str]  # 涉及的对象
    severity: SeverityLevel  # 严重程度
    count: int = 1  # 事件次数

    def is_critical(self) -> bool:
        """是否为关键事件"""
        return self.severity in [SeverityLevel.CRITICAL, SeverityLevel.HIGH]


@dataclass
class LogData:
    """日志数据模型"""

    timestamp: datetime  # 日志时间
    pod_name: str  # Pod名称
    container_name: str  # 容器名称
    level: str  # 日志级别
    message: str  # 日志消息
    error_type: Optional[str] = None  # 错误类型
    stack_trace: Optional[str] = None  # 堆栈跟踪

    def is_error(self) -> bool:
        """是否为错误日志"""
        return self.level in ["ERROR", "FATAL"]


@dataclass
class RootCauseAnalysis:
    """根因分析结果"""

    timestamp: datetime  # 分析时间
    namespace: str  # 命名空间
    root_causes: List[Any]  # 根因列表
    anomalies: Dict[str, Any] = field(default_factory=dict)  # 异常检测结果
    correlations: List[Any] = field(default_factory=list)  # 关联分析结果
    timeline: List[Dict[str, Any]] = field(default_factory=list)  # 事件时间线
    recommendations: List[str] = field(default_factory=list)  # 建议列表
    confidence_score: float = 0.0  # 置信度分数
    analysis_metadata: Dict[str, Any] = field(default_factory=dict)  # 元数据


# Pydantic模型（用于API）
class MetricDataResponse(BaseModel):
    """指标数据响应模型"""

    name: str
    values: List[Dict[str, Any]]
    labels: Dict[str, str]
    anomaly_score: float = Field(ge=0, le=1)
    trend: str

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "name": "container_memory_usage_bytes",
            "values": [{"timestamp": "2024-01-01T00:00:00Z", "value": 1024000}],
            "labels": {"pod": "app-pod-1", "container": "app"},
            "anomaly_score": 0.85,
            "trend": "increasing",
        }
    })


class EventDataResponse(BaseModel):
    """事件数据响应模型"""

    timestamp: datetime
    type: str
    reason: str
    message: str
    involved_object: Dict[str, str]
    severity: SeverityLevel
    count: int = 1

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "timestamp": "2024-01-01T00:00:00Z",
            "type": "Warning",
            "reason": "OOMKilled",
            "message": "Container was killed due to OOM",
            "involved_object": {
                "kind": "Pod",
                "name": "app-pod-1",
                "namespace": "default",
            },
            "severity": "critical",
            "count": 1,
        }
    })


class LogDataResponse(BaseModel):
    """日志数据响应模型"""

    timestamp: datetime
    pod_name: str
    container_name: str
    level: str
    message: str
    error_type: Optional[str] = None
    stack_trace: Optional[str] = None

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "timestamp": "2024-01-01T00:00:00Z",
            "pod_name": "app-pod-1",
            "container_name": "app",
            "level": "ERROR",
            "message": "Connection refused to database",
            "error_type": "Connection Error",
            "stack_trace": "at connect()...",
        }
    })


# API请求模型
class RCAAnalyzeRequest(BaseModel):
    """根因分析请求模型"""

    namespace: str = Field(..., description="Kubernetes命名空间")
    time_window_hours: float = Field(
        1.0, ge=0.1, le=24, description="分析时间窗口（小时）"
    )
    metrics: Optional[List[str]] = Field(
        None, description="要分析的Prometheus指标列表，为空则使用默认指标"
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "namespace": "production",
            "time_window_hours": 2.0,
            "metrics": [
                "container_cpu_usage_seconds_total",
                "container_memory_working_set_bytes",
                "kube_pod_container_status_restarts_total",
            ],
        }
    })


class RCAMetricsDataRequest(BaseModel):
    """指标数据查询请求模型"""

    namespace: str = Field(..., description="Kubernetes命名空间")
    start_time: Optional[str] = Field(None, description="开始时间 (ISO格式)")
    end_time: Optional[str] = Field(None, description="结束时间 (ISO格式)")
    metrics: Optional[str] = Field(None, description="逗号分隔的指标名称")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "namespace": "production",
            "start_time": "2024-01-01T00:00:00Z",
            "end_time": "2024-01-01T01:00:00Z",
            "metrics": "container_cpu_usage_seconds_total,container_memory_usage_bytes",
        }
    })


class RCAEventsDataRequest(BaseModel):
    """事件数据查询请求模型"""

    namespace: str = Field(..., description="Kubernetes命名空间")
    start_time: Optional[str] = Field(None, description="开始时间 (ISO格式)")
    end_time: Optional[str] = Field(None, description="结束时间 (ISO格式)")
    severity: Optional[str] = Field(None, description="严重程度过滤")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "namespace": "production",
            "start_time": "2024-01-01T00:00:00Z",
            "end_time": "2024-01-01T01:00:00Z",
            "severity": "critical",
        }
    })


class RCALogsDataRequest(BaseModel):
    """日志数据查询请求模型"""

    namespace: str = Field(..., description="Kubernetes命名空间")
    start_time: Optional[str] = Field(None, description="开始时间 (ISO格式)")
    end_time: Optional[str] = Field(None, description="结束时间 (ISO格式)")
    pod_name: Optional[str] = Field(None, description="Pod名称")
    error_only: bool = Field(True, description="只返回错误日志")
    max_lines: int = Field(100, le=1000, description="最大日志行数")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "namespace": "production",
            "start_time": "2024-01-01T00:00:00Z",
            "end_time": "2024-01-01T01:00:00Z",
            "pod_name": "app-pod-1",
            "error_only": True,
            "max_lines": 100,
        }
    })


class RCAQuickDiagnosisRequest(BaseModel):
    """快速诊断请求模型"""

    namespace: str = Field(..., description="Kubernetes命名空间")


class RCAEventPatternsRequest(BaseModel):
    """事件模式请求模型"""

    namespace: str = Field(..., description="Kubernetes命名空间")
    hours: float = Field(1.0, ge=0.1, le=24, description="分析时间范围（小时）")


class RCAErrorSummaryRequest(BaseModel):
    """错误摘要请求模型"""

    namespace: str = Field(..., description="Kubernetes命名空间")
    hours: float = Field(1.0, ge=0.1, le=24, description="分析时间范围（小时）")


class RCAClearNamespaceCacheRequest(BaseModel):
    """清理命名空间缓存请求模型"""

    namespace: str = Field(..., description="Kubernetes命名空间")


class RCAClearOperationCacheRequest(BaseModel):
    """清理操作缓存请求模型"""

    operation: str = Field(..., description="操作类型")


# API响应模型
class RCAAnalysisResponse(BaseModel):
    """根因分析响应模型"""

    namespace: str
    analysis_id: str
    timestamp: str
    time_window_hours: float
    root_causes: List[Dict[str, Any]] = []
    anomalies: Dict[str, Any] = {}
    correlations: List[Dict[str, Any]] = []
    recommendations: List[str] = []
    confidence_score: float = 0.0
    status: str = "completed"
    collection: Dict[str, Any] = Field(default_factory=dict)


class RCADataResponse(BaseModel):
    """数据查询通用响应模型"""

    namespace: str
    items: List[Dict[str, Any]]
    total: int
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    query_params: Dict[str, Any] = {}
    timestamp: str

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "namespace": "production",
            "items": [
                {
                    "name": "cpu_usage",
                    "values": [{"timestamp": "2024-01-01T00:00:00Z", "value": 0.5}],
                }
            ],
            "total": 1,
            "start_time": "2024-01-01T00:00:00Z",
            "end_time": "2024-01-01T01:00:00Z",
            "query_params": {"metrics": "cpu_usage"},
            "timestamp": "2024-01-01T01:00:00Z",
        }
    })


class RCACacheStatsResponse(BaseModel):
    """缓存统计响应模型"""

    available: bool
    healthy: Optional[bool] = None
    cache_prefix: Optional[str] = None
    default_ttl: Optional[int] = None
    hit_rate: Optional[float] = None
    total_keys: Optional[int] = None
    memory_usage: Optional[str] = None
    timestamp: str
    message: Optional[str] = None

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "available": True,
            "healthy": True,
            "cache_prefix": "rca_cache:",
            "default_ttl": 1800,
            "hit_rate": 0.85,
            "total_keys": 150,
            "memory_usage": "2.5MB",
            "timestamp": "2024-01-01T01:00:00Z",
            "message": "缓存运行正常",
        }
    })


class RCAClearCacheResponse(BaseModel):
    """清理缓存响应模型"""

    success: bool
    message: str
    cleared_count: int
    operation: Optional[str] = None
    namespace: Optional[str] = None
    timestamp: str

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "success": True,
            "message": "成功清理 50 个缓存项",
            "cleared_count": 50,
            "timestamp": "2024-01-01T01:00:00Z",
        }
    })


class RCAHealthResponse(BaseModel):
    """RCA健康检查响应模型"""

    status: str
    prometheus_connected: bool
    kubernetes_connected: bool
    redis_connected: bool
    last_check_time: str
    version: Optional[str] = None


class QuickDiagnosisResponse(BaseModel):
    """快速诊断响应模型"""

    namespace: str
    status: str
    critical_issues: List[Dict[str, Any]]
    warnings: List[Dict[str, Any]]
    recommendations: List[str]
    timestamp: str
    analysis_duration: float
    collection: Dict[str, Any] = Field(default_factory=dict)


class EventPatternsResponse(BaseModel):
    """事件模式响应模型"""

    namespace: str
    time_range_hours: float
    patterns: List[Dict[str, Any]]
    trending_events: List[str]
    anomalous_events: List[str]
    timestamp: str


class ErrorSummaryResponse(BaseModel):
    """错误摘要响应模型"""

    namespace: str
    time_range_hours: float
    total_errors: int
    error_categories: Dict[str, int]
    top_errors: List[Dict[str, Any]]
    error_timeline: List[Dict[str, Any]]
    timestamp: str
