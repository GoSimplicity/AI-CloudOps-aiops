#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


# 通用实体
class OperationResultEntity(BaseModel):
    message: Optional[str] = None
    timestamp: Optional[str] = None
    success: Optional[bool] = None
    details: Optional[Dict[str, Any]] = None


class HealthEntity(BaseModel):
    status: str
    timestamp: str
    uptime: Optional[float] = None
    version: Optional[str] = None
    components: Optional[Dict[str, bool]] = None
    system: Optional[Dict[str, Any]] = None


# Assistant 实体
class AssistantAnswerEntity(BaseModel):
    answer: Any
    session_id: Optional[str] = None
    mode: Optional[str] = None
    timestamp: str


class AssistantSessionEntity(BaseModel):
    session_id: str
    timestamp: str


class AssistantDocumentEntity(BaseModel):
    title: str
    content_length: int
    timestamp: str


class AssistantChatEntity(BaseModel):
    response: str
    confidence: Optional[float] = None


class AssistantSearchResultsEntity(BaseModel):
    results: List[Dict[str, Any]]


# Prediction 实体
class PredictionEntity(BaseModel):
    instances: int
    current_qps: float
    timestamp: str
    confidence: Optional[float] = None
    model_version: Optional[str] = None
    prediction_type: Optional[str] = None
    features: Optional[Dict[str, float]] = None
    schedule: Optional[Dict[str, Any]] = None


class TrendPredictionEntity(BaseModel):
    result: Dict[str, Any]


class ModelInfoEntity(BaseModel):
    info: Dict[str, Any]


class ReplicaSuggestionEntity(BaseModel):
    predicted_replicas: int
    confidence: float
    average_qps: float


# Autofix 实体
class AutoFixEntity(BaseModel):
    status: str
    result: str
    deployment: str
    namespace: str
    actions_taken: List[str]
    timestamp: str
    success: bool
    error_message: Optional[str] = None


class AutofixDiagnoseEntity(BaseModel):
    namespace: str
    issues: List[Dict[str, Any]]


class AutofixActionResultEntity(BaseModel):
    namespace: str
    deployment: str
    status: str


class WorkflowEntity(BaseModel):
    workflow_id: str
    status: str


class ServiceHealthEntity(BaseModel):
    healthy: bool
    components: Optional[Dict[str, bool]] = None
    remediation_config: Optional[Dict[str, Any]] = None
    timestamp: Optional[str] = None
    service: Optional[str] = None
    flags: Optional[Dict[str, Any]] = None


# Autofix 历史与记录详情


class DeletionResultEntity(BaseModel):
    id: int


# === Assistant 模块：DB 级 CRUD 实体 ===


class AssistantQueryEntity(BaseModel):
    id: int
    session_id: Optional[str] = None
    question: str
    answer: Optional[str] = None
    mode: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AssistantSessionEntityDB(BaseModel):
    id: int
    session_id: str
    note: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AssistantDocumentEntityDB(BaseModel):
    id: int
    title: str
    content: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# RCA 实体（结构弹性，以便兼容不同分析结果）
class RCAResultEntity(BaseModel):
    result: Dict[str, Any]


class RCAJobEntity(BaseModel):
    job_id: str
    flags: Optional[Dict[str, Any]] = None


class RCAJobDetailEntity(BaseModel):
    data: Dict[str, Any]


class RCAMetricsEntity(BaseModel):
    default_metrics: List[str]
    available_metrics: List[str]
    flags: Optional[Dict[str, Any]] = None


class TopologySnapshotEntity(BaseModel):
    namespace: Optional[str] = None
    counts: Dict[str, int]
    topology: Dict[str, Any]
    impact_scope: Optional[List[Any]] = None
    flags: Optional[Dict[str, Any]] = None


class AnomalyDetectionEntity(BaseModel):
    anomalies: Dict[str, Any]
    detection_period: Dict[str, str]
    sensitivity: Optional[float] = None


class CorrelationAnalysisEntity(BaseModel):
    target_metric: Optional[str] = None
    correlations: Dict[str, Any]
    analysis_period: Dict[str, str]


class TimelineEntity(BaseModel):
    timeline: List[Dict[str, Any]]
    period: Dict[str, str]


# Multi-Agent 实体
class MultiAgentMetricsEntity(BaseModel):
    total_workflows: int
    successful_workflows: int
    rolled_back: int
    avg_success_rate: float
    config: Dict[str, Any]
    timestamp: str


class CoordinatorStatusEntity(BaseModel):
    healthy: bool
    components: Optional[Dict[str, Any]] = None
    timestamp: Optional[str] = None
    service: Optional[str] = None


class NotificationSendResultEntity(BaseModel):
    sent: bool


# === RCA 模块：DB 级 CRUD 实体 ===


class RCARecordEntity(BaseModel):
    id: int
    start_time: str
    end_time: str
    metrics: Optional[str] = None
    namespace: Optional[str] = None
    # service_name: Optional[str] = None  # 暂时禁用 Trace 相关字段
    status: Optional[str] = None
    summary: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# === Prediction 模块：DB 级 CRUD 实体 ===


class PredictionRecordEntity(BaseModel):
    id: int
    current_qps: Optional[float] = None
    input_timestamp: Optional[str] = None
    use_prom: Optional[bool] = None
    metric: Optional[str] = None
    selector: Optional[str] = None
    window: Optional[str] = None
    instances: Optional[int] = None
    confidence: Optional[float] = None
    model_version: Optional[str] = None
    prediction_type: Optional[str] = None
    features: Optional[Dict[str, Any]] = None
    schedule_interval_minutes: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# === Autofix 模块：DB 级 CRUD 实体 ===


class AutoFixRecordEntity(BaseModel):
    id: int
    deployment: str
    namespace: str
    status: Optional[str] = None
    actions: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# === Multi-Agent 模块：DB 级 CRUD 实体 ===


class WorkflowRecordEntity(BaseModel):
    id: int
    workflow_id: str
    status: str
    namespace: Optional[str] = None
    target: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# === Health 模块：DB 级 CRUD 实体 ===


class HealthSnapshotRecordEntity(BaseModel):
    id: int
    status: str
    components: Optional[Dict[str, Any]] = None
    system: Optional[Dict[str, Any]] = None
    version: Optional[str] = None
    uptime: Optional[float] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class MultiAgentStatusEntity(BaseModel):
    agents: List[Dict[str, Any]]
