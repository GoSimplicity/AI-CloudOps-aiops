#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: API请求模型 - 定义用于验证和解析传入API请求的Pydantic模型，包含适当的验证规则
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.config.settings import config


class ListRequest(BaseModel):
    """统一的列表请求模型"""
    
    page: int = Field(default=1, ge=1, description="页码（从1开始）")
    size: int = Field(default=20, ge=1, le=100, description="每页大小")
    search: Optional[str] = Field(default=None, description="搜索关键词")


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
    include_traces: bool = Field(default=False, description="是否包含Trace/OTel/Jaeger证据")
    namespace: Optional[str] = Field(default=None, description="目标命名空间，缺省为配置默认")
    service_name: Optional[str] = Field(default=None, description="Trace服务名过滤")

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
            # 使用北京时间（UTC+8）
            tz = timezone(timedelta(hours=8))
            now = datetime.now(tz)
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
    metric: Optional[str] = Field(default=None, description="Prometheus指标名，如http_requests_total")
    selector: Optional[str] = Field(default=None, description="Prometheus标签选择器，如job=\"svc\"")
    window: Optional[str] = Field(default="1m", description="Prometheus速率窗口，如1m/5m")
    # 周期执行控制（单位：分钟）
    interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440, description="预测任务的建议执行周期（分钟）")

    @field_validator("current_qps")
    def validate_qps(cls, v):
        if v is not None and v < 0:
            raise ValueError("QPS不能为负数")
        return v


class AssistantRequest(BaseModel):
    """智能小助手请求模型"""

    question: str = Field(..., min_length=1, description="用户提问")
    chat_history: Optional[List[Dict[str, str]]] = Field(
        default=None, description="对话历史记录"
    )
    use_web_search: bool = Field(default=False, description="是否使用网络搜索增强回答")
    max_context_docs: int = Field(
        default=4, ge=1, le=10, description="最大上下文文档数量"
    )
    session_id: Optional[str] = Field(
        default=None, description="会话ID，为空则创建新会话"
    )
    mode: str = Field(
        default="rag",
        description="运行模式: 'rag' 使用传统RAG功能, 'mcp' 使用MCP工具调用功能",
    )
