#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
定义 cl_aiops_ 前缀的数据表模型：
- cl_aiops_queries: 问答请求与结果快照
- cl_aiops_rca_analyses: RCA 分析记录
- cl_aiops_autofix_jobs: 自动修复任务记录
- cl_aiops_predictions: 预测请求与结果记录
- cl_aiops_notifications: 通知发送记录
- cl_aiops_sessions: 助手会话记录
- cl_aiops_documents: 文档记录

每表包含 id, created_at, updated_at, deleted_at 基础字段。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def utcnow() -> datetime:
    # 统一使用时区感知的 UTC 存储
    return datetime.now(timezone.utc)


class TimestampMixin:
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False, index=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class QueryRecord(Base, TimestampMixin):
    __tablename__ = "cl_aiops_queries"

    session_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mode: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


class RCAAnalysisRecord(Base, TimestampMixin):
    __tablename__ = "cl_aiops_rca_analyses"

    start_time: Mapped[str] = mapped_column(String(64), nullable=False)
    end_time: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    namespace: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    service_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ok")
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AutoFixJobRecord(Base, TimestampMixin):
    __tablename__ = "cl_aiops_autofix_jobs"

    deployment: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="success")
    actions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class PredictionRecord(Base, TimestampMixin):
    __tablename__ = "cl_aiops_predictions"

    # 输入参数
    current_qps: Mapped[Optional[float]] = mapped_column(nullable=True)
    input_timestamp: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    use_prom: Mapped[Optional[bool]] = mapped_column(nullable=True)
    metric: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    selector: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    window: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # 输出结果
    instances: Mapped[Optional[int]] = mapped_column(nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(nullable=True)
    model_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    prediction_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    features: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON字符串
    schedule_interval_minutes: Mapped[Optional[int]] = mapped_column(nullable=True)


class NotificationRecord(Base, TimestampMixin):
    __tablename__ = "cl_aiops_notifications"

    channel: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AssistantSession(Base, TimestampMixin):
    __tablename__ = "cl_aiops_sessions"

    session_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class DocumentRecord(Base, TimestampMixin):
    __tablename__ = "cl_aiops_documents"

    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

