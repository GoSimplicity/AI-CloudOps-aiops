#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.config.settings import config

# 备注：通用列表请求模型未被当前API使用，移除以减小冗余


class RCARequest(BaseModel):
    start_time: Optional[datetime] = Field(
        default=None, description="起始时间（ISO8601）。若为空，将按时间范围推导"
    )
    end_time: Optional[datetime] = Field(
        default=None, description="结束时间（ISO8601）。若为空，将按时间范围推导"
    )
    metrics: Optional[List[str]] = Field(
        default=None, description="指标列表（为空则使用平台默认指标）"
    )
    time_range_minutes: Optional[int] = Field(
        None, ge=1, le=config.rca.max_time_range, description="时间范围（分钟）"
    )
    include_logs: bool = Field(default=False, description="是否包含容器日志证据")
    # include_traces: bool = Field(
    #     default=False, description="是否包含Trace/OTel/Jaeger证据（暂时禁用）"
    # )
    namespace: Optional[str] = Field(
        default=None, description="目标命名空间，缺省为配置默认"
    )
    # service_name: Optional[str] = Field(default=None, description="Trace服务名过滤（暂时禁用）")

    @field_validator("start_time", "end_time", mode="before")
    def parse_datetime(cls, v):
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return datetime.fromisoformat(v)
        return v

    def __init__(self, **data):
        super().__init__(**data)

        # 如果没有提供时间范围，使用默认值
        if not self.start_time or not self.end_time:
            # 使用 UTC
            now = datetime.now(timezone.utc)
            if self.time_range_minutes:
                self.end_time = now
                self.start_time = self.end_time - timedelta(
                    minutes=self.time_range_minutes
                )
            else:
                self.end_time = now
                self.start_time = self.end_time - timedelta(
                    minutes=config.rca.default_time_range
                )

        # 如果没有提供指标，使用默认指标
        if not self.metrics:
            self.metrics = config.rca.default_metrics


class AutoFixRequest(BaseModel):
    deployment: str = Field(..., min_length=1, description="目标部署名称")
    namespace: str = Field(default="default", min_length=1, description="命名空间")
    event: str = Field(..., min_length=1, description="触发事件/问题描述")
    force: bool = Field(default=False, description="是否强制执行修复")
    auto_restart: bool = Field(default=True, description="修复后是否自动重启")


class PredictionRequest(BaseModel):
    current_qps: Optional[float] = Field(default=None, description="当前QPS值")
    timestamp: Optional[datetime] = Field(default=None, description="预测时间戳")
    include_confidence: bool = Field(default=True, description="是否包含置信度")
    # Prometheus 取数相关可选参数
    use_prom: bool = Field(default=False, description="是否从Prometheus读取当前QPS")
    metric: Optional[str] = Field(
        default=None, description="Prometheus指标名，如http_requests_total"
    )
    selector: Optional[str] = Field(
        default=None, description='Prometheus标签选择器，如job="svc"'
    )
    window: Optional[str] = Field(
        default="1m", description="Prometheus速率窗口，如1m/5m"
    )
    # 周期执行控制（单位：分钟）
    interval_minutes: Optional[int] = Field(
        default=None, ge=1, le=1440, description="预测任务的建议执行周期（分钟）"
    )

    @field_validator("current_qps")
    def validate_qps(cls, v):
        if v is not None and v < 0:
            raise ValueError("QPS不能为负数")
        return v


# === 统一 AutoXXXReq 请求模型（供所有接口使用） ===


# Assistant 请求
class AutoAssistantQueryReq(BaseModel):
    question: str
    session_id: Optional[str] = None
    mode: Optional[str] = "normal"


class AutoAssistantSessionReq(BaseModel):
    session_id: Optional[str] = None


class AutoAssistantDocumentReq(BaseModel):
    content: str
    title: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class AutoAssistantChatReq(BaseModel):
    query: str
    session_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    mode: Optional[int] = Field(
        default=1, ge=1, le=2, description="聊天模式：1=RAG，2=MCP"
    )


class AutoAssistantSearchReq(BaseModel):
    query: str


# Autofix 请求
class AutoAutofixDiagnoseReq(BaseModel):
    namespace: Optional[str] = Field(default="default")
    deployment: Optional[str] = None


class AutoAutofixFixReq(BaseModel):
    namespace: str = Field(default="default")
    deployment: str
    # 兼容测试/工作流里可能传入的可选字段（不参与业务校验）
    issues: Optional[List[str]] = None
    auto_approve: Optional[bool] = None


class AutoAutofixWorkflowReq(BaseModel):
    workflow_type: Optional[str] = None
    namespace: Optional[str] = "default"
    target: Optional[str] = None


class AutoAutofixCreateReq(AutoFixRequest):
    pass


class AutoAutofixNotifyReq(BaseModel):
    webhook_url: Optional[str] = None
    message: Optional[str] = None


# Prediction 请求
class AutoPredictReq(PredictionRequest):
    pass


class AutoTrendReq(BaseModel):
    hours_ahead: int = 24
    current_qps: Optional[float] = None
    use_prom: bool = False
    metric: Optional[str] = None
    selector: Optional[str] = None
    window: Optional[str] = "1m"


# Multi-Agent 请求
class AutoMultiAgentRepairReq(BaseModel):
    deployment: str
    namespace: Optional[str] = "default"


class AutoMultiAgentRepairAllReq(BaseModel):
    namespace: Optional[str] = "default"


class AutoMultiAgentClusterReq(BaseModel):
    cluster_name: Optional[str] = "default"


class AutoMultiAgentExecuteReq(BaseModel):
    task_type: str
    priority: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None

    # 兼容返回任务ID/状态的上层逻辑，虽由服务端生成，但保留可选占位不影响校验
    # 注意：这些字段不会被接口作为输入使用，仅为了保持模型通用性（不在API中读取）。
    task_id: Optional[str] = None
    status: Optional[str] = None


# === 列表/查询类请求（用于覆盖GET查询参数的结构化模型，便于统一文档/使用） ===


# === 通用分页与过滤参数 ===


class PaginationReq(BaseModel):
    page: Optional[int] = 1
    size: Optional[int] = 20


# === Assistant 模块：DB 级 CRUD 请求模型 ===


class AssistantQueryCreateReq(BaseModel):
    session_id: Optional[str] = None
    question: str
    answer: Optional[str] = None
    mode: Optional[str] = None


class AssistantQueryUpdateReq(BaseModel):
    session_id: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    mode: Optional[str] = None


class AssistantSessionCreateReq(BaseModel):
    session_id: str
    note: Optional[str] = None


class AssistantSessionUpdateReq(BaseModel):
    session_id: Optional[str] = None
    note: Optional[str] = None


class AssistantDocumentCreateReq(BaseModel):
    title: str
    content: str
    metadata: Optional[Dict[str, Any]] = None


class AssistantDocumentUpdateReq(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class AssistantQueryListReq(PaginationReq):
    session_id: Optional[str] = None
    mode: Optional[str] = None
    q: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None


class AssistantSessionListReq(PaginationReq):
    session_id: Optional[str] = None


class AssistantDocumentListReq(PaginationReq):
    title: Optional[str] = None


# === Prediction 模块：DB 级 CRUD 请求模型 ===


class PredictionRecordCreateReq(BaseModel):
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


class PredictionRecordUpdateReq(BaseModel):
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


class PredictionRecordListReq(PaginationReq):
    metric: Optional[str] = None
    model_version: Optional[str] = None
    prediction_type: Optional[str] = None




class RCARecordListReq(PaginationReq):
    namespace: Optional[str] = None
    status: Optional[str] = None
    search: Optional[str] = None
    record_type: Optional[str] = None
    job_id: Optional[str] = None


# === Autofix 模块：DB 级 CRUD 请求模型 ===


class AutoFixRecordCreateReq(BaseModel):
    deployment: str
    namespace: str = "default"
    status: Optional[str] = "success"
    actions: Optional[str] = None
    error_message: Optional[str] = None


class AutoFixRecordUpdateReq(BaseModel):
    deployment: Optional[str] = None
    namespace: Optional[str] = None
    status: Optional[str] = None
    actions: Optional[str] = None
    error_message: Optional[str] = None


class AutoFixRecordListReq(PaginationReq):
    namespace: Optional[str] = None
    status: Optional[str] = None
    search: Optional[str] = None


# === Multi-Agent 模块：DB 级 CRUD 请求模型 ===


class WorkflowRecordCreateReq(BaseModel):
    workflow_id: str
    status: str
    namespace: Optional[str] = None
    target: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class WorkflowRecordUpdateReq(BaseModel):
    workflow_id: Optional[str] = None
    status: Optional[str] = None
    namespace: Optional[str] = None
    target: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class WorkflowRecordListReq(PaginationReq):
    namespace: Optional[str] = None
    status: Optional[str] = None
    search: Optional[str] = None


# === Health 模块：DB 级 CRUD 请求模型 ===


class HealthSnapshotCreateReq(BaseModel):
    status: str
    components: Optional[Dict[str, Any]] = None
    system: Optional[Dict[str, Any]] = None
    version: Optional[str] = None
    uptime: Optional[float] = None


class HealthSnapshotUpdateReq(BaseModel):
    status: Optional[str] = None
    components: Optional[Dict[str, Any]] = None
    system: Optional[Dict[str, Any]] = None
    version: Optional[str] = None
    uptime: Optional[float] = None


class HealthSnapshotListReq(PaginationReq):
    status: Optional[str] = None


# RCA 请求
class AutoRCAAnalyzeReq(RCARequest):
    pass


class AutoRCAJobReq(BaseModel):
    start_time: datetime
    end_time: datetime
    metrics: Optional[List[str]] = None
    namespace: Optional[str] = None

    @staticmethod
    def _parse_dt(v: Any) -> Any:
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except Exception:
                return datetime.fromisoformat(v)
        return v

    @classmethod
    def model_validate(cls, obj):  # type: ignore[override]
        if isinstance(obj, dict):
            for k in ("start_time", "end_time"):
                if k in obj:
                    obj[k] = cls._parse_dt(obj[k])
        return super().model_validate(obj)


class AutoRCAAnomalyReq(BaseModel):
    start_time: datetime
    end_time: datetime
    metrics: Optional[List[str]] = None
    sensitivity: Optional[float] = 0.8

    @field_validator("start_time", "end_time", mode="before")
    def _parse_dt(cls, v):
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except Exception:
                return datetime.fromisoformat(v)
        return v


class AutoRCACorrelationReq(BaseModel):
    start_time: datetime
    end_time: datetime
    target_metric: Optional[str] = None
    metrics: Optional[List[str]] = None
    namespace: Optional[str] = None

    @field_validator("start_time", "end_time", mode="before")
    def _parse_dt(cls, v):
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except Exception:
                return datetime.fromisoformat(v)
        return v

    @field_validator("metrics", mode="before")
    def _parse_metrics(cls, v):
        # 接受字符串（逗号分隔或JSON数组字符串）或列表
        if v is None:
            return v
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            text = v.strip()
            if not text:
                return None
            # 优先JSON
            try:
                import json as _json
                loaded = _json.loads(text)
                if isinstance(loaded, list):
                    return loaded
            except Exception:
                pass
            # 回退逗号分割
            return [p.strip().strip('\"\'') for p in text.split(',') if p.strip()]
        return v


class AutoRCACrossCorrelationReq(BaseModel):
    start_time: datetime
    end_time: datetime
    metrics: Optional[List[str]] = None
    max_lags: Optional[int] = 10
    namespace: Optional[str] = None

    @field_validator("start_time", "end_time", mode="before")
    def _parse_dt(cls, v):
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except Exception:
                return datetime.fromisoformat(v)
        return v


class AutoRCATimelineReq(BaseModel):
    start_time: datetime
    end_time: datetime
    events: Optional[List[Dict[str, Any]]] = None

    @field_validator("start_time", "end_time", mode="before")
    def _parse_dt(cls, v):
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except Exception:
                return datetime.fromisoformat(v)
        return v
