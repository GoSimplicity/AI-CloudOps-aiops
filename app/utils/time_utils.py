#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 时间工具
"""

from __future__ import annotations

from datetime import datetime, timezone

# 统一的UTC时区常量，避免重复定义
UTC_TZ = timezone.utc


def utc_now() -> datetime:
    """获取当前UTC时间（时区感知）。"""
    return datetime.now(UTC_TZ)


def iso_utc_now() -> str:
    """获取当前UTC时间的ISO8601字符串（带Z后缀）。"""
    # 使用 timespec="seconds" 保持一致性，外层日志使用Z后缀
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_aware_utc(dt: datetime) -> datetime:
    """将传入时间安全转换为时区感知的UTC时间。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(UTC_TZ)
