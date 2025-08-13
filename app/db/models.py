#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
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


class WorkflowRecord(Base, TimestampMixin):
    __tablename__ = "cl_aiops_workflows"

    workflow_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    namespace: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    target: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON 字符串


class HealthSnapshotRecord(Base, TimestampMixin):
    __tablename__ = "cl_aiops_health_snapshots"

    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    components: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON 字符串
    system: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON 字符串
    version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    uptime: Mapped[Optional[float]] = mapped_column(nullable=True)

