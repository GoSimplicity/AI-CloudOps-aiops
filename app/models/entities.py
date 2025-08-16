#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Pydantic 实体模型 - 强类型版本
"""

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Union

from pydantic import BaseModel, Field


# 健康状态
class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


# 预测类型
class PredictionType(str, Enum):
    QPS = "qps"
    CPU = "cpu"
    MEMORY = "memory"
    REPLICAS = "replicas"
    CUSTOM = "custom"


# 自动修复状态
class AutofixStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    IN_PROGRESS = "in_progress"
    ROLLED_BACK = "rolled_back"


# 工作流状态
class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# RCA 记录类型
class RCARecordType(str, Enum):
    ANOMALY_DETECTION = "anomaly_detection"
    CORRELATION_ANALYSIS = "correlation_analysis"
    TOPOLOGY_ANALYSIS = "topology_analysis"
    TIMELINE_ANALYSIS = "timeline_analysis"


# 助手模式
class AssistantMode(str, Enum):
    CHAT = "chat"
    SEARCH = "search"
    DOCUMENT = "document"


# 系统信息
class SystemInfo(BaseModel):
    cpu_usage: Optional[float] = None
    memory_usage: Optional[float] = None
    disk_usage: Optional[float] = None
    network_io: Optional[Dict[str, float]] = None
    load_average: Optional[List[float]] = None


# 组件状态
class ComponentStatus(BaseModel):
    name: str
    status: bool
    last_check: Optional[str] = None
    error_message: Optional[str] = None


# 指标值
class MetricValue(BaseModel):
    value: float
    timestamp: str
    labels: Optional[Dict[str, str]] = None


# 预测调度
class PredictionSchedule(BaseModel):
    interval_minutes: int
    enabled: bool
    last_run: Optional[str] = None
    next_run: Optional[str] = None


# 预测特征
class PredictionFeatures(BaseModel):
    cpu_usage: Optional[float] = None
    memory_usage: Optional[float] = None
    network_io: Optional[float] = None
    request_count: Optional[int] = None
    error_rate: Optional[float] = None
    response_time: Optional[float] = None


# 自动修复动作
class AutofixAction(BaseModel):
    action_type: str
    target: str
    parameters: Dict[str, Union[str, int, float, bool]]
    timestamp: str
    success: bool
    error_message: Optional[str] = None


# 问题详情
class IssueDetail(BaseModel):
    issue_type: str
    severity: str
    description: str
    affected_resources: List[str]
    suggested_fixes: List[str]
    timestamp: str


# 拓扑节点
class TopologyNode(BaseModel):
    id: str
    type: str
    name: str
    namespace: Optional[str] = None
    status: str
    labels: Optional[Dict[str, str]] = None
    metrics: Optional[Dict[str, float]] = None


# 拓扑边
class TopologyEdge(BaseModel):
    source: str
    target: str
    relationship: str
    weight: Optional[float] = None


# 拓扑图
class TopologyGraph(BaseModel):
    nodes: List[TopologyNode]
    edges: List[TopologyEdge]
    metadata: Optional[Dict[str, Union[str, int, float]]] = None


# 异常点
class AnomalyPoint(BaseModel):
    timestamp: str
    value: float
    expected_value: float
    deviation: float
    severity: str
    metric_name: str


# 相关性结果
class CorrelationResult(BaseModel):
    metric_name: str
    correlation_coefficient: float
    p_value: float
    significance: bool
    trend: str


# 时间线事件
class TimelineEvent(BaseModel):
    timestamp: str
    event_type: str
    description: str
    severity: str
    affected_components: List[str]
    metadata: Optional[Dict[str, Union[str, int, float]]] = None


# 代理状态
class AgentStatus(BaseModel):
    agent_id: str
    agent_type: str
    status: str
    last_heartbeat: str
    capabilities: List[str]
    current_task: Optional[str] = None
    performance_metrics: Optional[Dict[str, float]] = None


# 通知配置
class NotificationConfig(BaseModel):
    webhook_url: Optional[str] = None
    email_recipients: Optional[List[str]] = None
    slack_channel: Optional[str] = None
    enabled: bool = True


# 修复配置
class RemediationConfig(BaseModel):
    auto_fix_enabled: bool = True
    max_retries: int = 3
    rollback_threshold: float = 0.8
    notification_config: Optional[NotificationConfig] = None
    allowed_actions: List[str] = Field(default_factory=list)


# 操作结果
class OperationResultEntity(BaseModel):
    message: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    success: bool
    details: Optional[Dict[str, Union[str, int, float, bool, List[str]]]] = None


# 健康状态
class HealthEntity(BaseModel):
    status: HealthStatus
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    uptime: Optional[float] = None
    version: Optional[str] = None
    components: Optional[Dict[str, bool]] = None
    system: Optional[SystemInfo] = None


# 助手回答
class AssistantAnswerEntity(BaseModel):
    answer: str
    session_id: Optional[str] = None
    mode: Optional[AssistantMode] = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# 助手会话
class AssistantSessionEntity(BaseModel):
    session_id: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# 助手文档
class AssistantDocumentEntity(BaseModel):
    title: str
    content_length: int
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class AssistantChatEntity(BaseModel):
    response: str
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)


# 搜索结果
class SearchResult(BaseModel):
    title: str
    content: str
    relevance_score: float
    source: str
    metadata: Optional[Dict[str, Union[str, int, float]]] = None


# 助手搜索结果
class AssistantSearchResultsEntity(BaseModel):
    results: List[SearchResult]


# 预测结果
class PredictionEntity(BaseModel):
    instances: int = Field(ge=0)
    current_qps: float = Field(ge=0.0)
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    model_version: Optional[str] = None
    prediction_type: Optional[PredictionType] = None
    features: Optional[PredictionFeatures] = None
    schedule: Optional[PredictionSchedule] = None


# 趋势预测结果
class TrendPredictionResult(BaseModel):
    predicted_values: List[MetricValue]
    trend_direction: str
    confidence_interval: Optional[Dict[str, float]] = None
    model_accuracy: Optional[float] = None


# 趋势预测结果
class TrendPredictionEntity(BaseModel):
    result: TrendPredictionResult


# 模型信息
class ModelInfo(BaseModel):
    model_name: str
    version: str
    accuracy: float
    last_trained: str
    features_used: List[str]
    hyperparameters: Dict[str, Union[str, int, float]]


# 模型信息
class ModelInfoEntity(BaseModel):
    info: ModelInfo


# 副本建议
class ReplicaSuggestionEntity(BaseModel):
    predicted_replicas: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    average_qps: float = Field(ge=0.0)


# 自动修复结果
class AutoFixEntity(BaseModel):
    status: AutofixStatus
    result: str
    deployment: str
    namespace: str
    actions_taken: List[AutofixAction]
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    success: bool
    error_message: Optional[str] = None


# 自动修复诊断
class AutofixDiagnoseEntity(BaseModel):
    namespace: str
    issues: List[IssueDetail]


# 自动修复动作结果
class AutofixActionResultEntity(BaseModel):
    namespace: str
    deployment: str
    status: AutofixStatus


# 工作流
class WorkflowEntity(BaseModel):
    workflow_id: str
    status: WorkflowStatus


# 服务健康
class ServiceHealthEntity(BaseModel):
    healthy: bool
    components: Optional[Dict[str, bool]] = None
    remediation_config: Optional[RemediationConfig] = None
    timestamp: Optional[str] = None
    service: Optional[str] = None
    flags: Optional[Dict[str, Union[str, int, float, bool]]] = None


# 删除结果
class DeletionResultEntity(BaseModel):
    id: int


# 助手查询
class AssistantQueryEntity(BaseModel):
    id: int
    session_id: Optional[str] = None
    question: str
    answer: Optional[str] = None
    mode: Optional[AssistantMode] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# 助手会话
class AssistantSessionEntityDB(BaseModel):
    id: int
    session_id: str
    note: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# 助手文档


class AssistantDocumentEntityDB(BaseModel):
    id: int
    title: str
    content: Optional[str] = None
    metadata: Optional[Dict[str, Union[str, int, float, bool]]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# RCA 结果
class RCAResult(BaseModel):
    root_cause: str
    confidence: float
    evidence: List[str]
    affected_components: List[str]
    recommendations: List[str]


# RCA 结果实体
class RCAResultEntity(BaseModel):
    result: RCAResult


# RCA 作业
class RCAJobEntity(BaseModel):
    job_id: str
    flags: Optional[Dict[str, Union[str, int, float, bool]]] = None


# RCA 作业详情
class RCAJobDetail(BaseModel):
    job_id: str
    status: str
    progress: float
    start_time: str
    end_time: Optional[str] = None
    results: Optional[RCAResult] = None


# RCA 作业详情实体
class RCAJobDetailEntity(BaseModel):
    data: RCAJobDetail


# RCA 指标
class RCAMetricsEntity(BaseModel):
    default_metrics: List[str]
    available_metrics: List[str]
    flags: Optional[Dict[str, Union[str, int, float, bool]]] = None


# 拓扑快照
class TopologySnapshotEntity(BaseModel):
    namespace: Optional[str] = None
    counts: Dict[str, int]
    topology: TopologyGraph
    impact_scope: Optional[List[str]] = None
    flags: Optional[Dict[str, Union[str, int, float, bool]]] = None


# 异常检测结果
class AnomalyDetectionResult(BaseModel):
    anomalies: List[AnomalyPoint]
    detection_period: Dict[str, str]
    sensitivity: Optional[float] = Field(None, ge=0.0, le=1.0)


# 异常检测实体
class AnomalyDetectionEntity(BaseModel):
    anomalies: AnomalyDetectionResult
    detection_period: Dict[str, str]
    sensitivity: Optional[float] = Field(None, ge=0.0, le=1.0)


# 相关性分析结果
class CorrelationAnalysisResult(BaseModel):
    target_metric: Optional[str] = None
    correlations: List[CorrelationResult]
    analysis_period: Dict[str, str]


# 相关性分析实体
class CorrelationAnalysisEntity(BaseModel):
    target_metric: Optional[str] = None
    correlations: CorrelationAnalysisResult
    analysis_period: Dict[str, str]


# 时间线结果
class TimelineResult(BaseModel):
    timeline: List[TimelineEvent]
    period: Dict[str, str]


# 时间线实体
class TimelineEntity(BaseModel):
    timeline: TimelineResult
    period: Dict[str, str]


# 多代理指标
class MultiAgentMetricsEntity(BaseModel):
    total_workflows: int = Field(ge=0)
    successful_workflows: int = Field(ge=0)
    rolled_back: int = Field(ge=0)
    avg_success_rate: float = Field(ge=0.0, le=1.0)
    config: RemediationConfig
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# 协调器状态
class CoordinatorStatusEntity(BaseModel):
    healthy: bool
    components: Optional[Dict[str, bool]] = None
    timestamp: Optional[str] = None
    service: Optional[str] = None


# 通知发送结果
class NotificationSendResultEntity(BaseModel):
    sent: bool


# RCA 记录
class RCARecordEntity(BaseModel):
    id: int
    start_time: str
    end_time: str
    metrics: Optional[str] = None
    namespace: Optional[str] = None
    status: Optional[str] = None
    summary: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    record_type: Optional[RCARecordType] = None
    job_id: Optional[str] = None
    params: Optional[Dict[str, Union[str, int, float, bool]]] = None
    result: Optional[RCAResult] = None
    error: Optional[str] = None


# 预测记录
class PredictionRecordEntity(BaseModel):
    id: int
    current_qps: Optional[float] = Field(None, ge=0.0)
    input_timestamp: Optional[str] = None
    use_prom: Optional[bool] = None
    metric: Optional[str] = None
    selector: Optional[str] = None
    window: Optional[str] = None
    instances: Optional[int] = Field(None, ge=0)
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    model_version: Optional[str] = None
    prediction_type: Optional[PredictionType] = None
    features: Optional[PredictionFeatures] = None
    schedule_interval_minutes: Optional[int] = Field(None, ge=0)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# 自动修复记录
class AutoFixRecordEntity(BaseModel):
    id: int
    deployment: str
    namespace: str
    status: Optional[AutofixStatus] = None
    actions: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# 工作流记录
class WorkflowRecordEntity(BaseModel):
    id: int
    workflow_id: str
    status: WorkflowStatus
    namespace: Optional[str] = None
    target: Optional[str] = None
    details: Optional[Dict[str, Union[str, int, float, bool, List[str]]]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# 健康快照记录
class HealthSnapshotRecordEntity(BaseModel):
    id: int
    status: HealthStatus
    components: Optional[Dict[str, bool]] = None
    system: Optional[SystemInfo] = None
    version: Optional[str] = None
    uptime: Optional[float] = Field(None, ge=0.0)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# 多代理状态
class MultiAgentStatusEntity(BaseModel):
    agents: List[AgentStatus]
