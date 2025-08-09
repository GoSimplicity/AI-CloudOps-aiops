#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: API响应模型 - 定义所有API端点的标准化响应结构，确保一致的格式和类型
"""

from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    """统一API响应格式 - 普通请求使用data字段"""

    code: int = 0
    message: str = ""
    data: Optional[T] = None


# 备注：分页信息由各API响应直接提供字段，独立模型未直接被引用，移除以减小冗余


class PaginatedListAPIResponse(BaseModel, Generic[T]):
    """分页列表API响应格式 - 包含分页信息"""

    code: int = 0
    message: str = ""
    items: Optional[List[T]] = None
    total: int = 0


class AnomalyInfo(BaseModel):
    count: int
    first_occurrence: str
    last_occurrence: str
    max_score: float
    avg_score: float
    detection_methods: Dict[str, Any]


class RootCauseCandidate(BaseModel):
    metric: str
    confidence: float
    first_occurrence: str
    anomaly_count: int
    related_metrics: List[tuple]
    description: Optional[str] = None


class RCAResponse(BaseModel):
    status: str
    anomalies: Dict[str, AnomalyInfo]
    correlations: Dict[str, List[tuple]]
    root_cause_candidates: List[RootCauseCandidate]
    analysis_time: str
    time_range: Dict[str, str]
    metrics_analyzed: List[str]
    summary: Optional[str] = None


class PredictionResponse(BaseModel):
    instances: int
    current_qps: float
    timestamp: str
    confidence: Optional[float] = None
    model_version: Optional[str] = None
    prediction_type: Optional[str] = None
    features: Optional[Dict[str, float]] = None
    schedule: Optional[Dict[str, Any]] = None


class AutoFixResponse(BaseModel):
    status: str
    result: str
    deployment: str
    namespace: str
    actions_taken: List[str]
    timestamp: str
    success: bool
    error_message: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    components: Dict[str, bool]
    timestamp: str
    version: Optional[str] = None
    uptime: Optional[float] = None


# 智能小助手响应直接使用通用 `APIResponse` 包裹，删除未用的专用模型
