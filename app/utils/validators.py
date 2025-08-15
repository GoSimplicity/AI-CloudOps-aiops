#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 输入校验工具
"""

import re
from datetime import datetime, timezone
from typing import List


def validate_time_range(
    start_time: datetime, end_time: datetime, max_range_minutes: int = 1440
) -> bool:
    """验证时间范围"""
    if start_time >= end_time:
        return False
    time_diff = (end_time - start_time).total_seconds() / 60
    if time_diff > max_range_minutes:
        return False
    # 检查是否是未来时间（使用 UTC）
    now = datetime.now(timezone.utc)
    if start_time > now or end_time > now:
        return False
    return True


def validate_metric_name(metric_name: str) -> bool:
    """验证指标名称格式"""
    if not metric_name or not isinstance(metric_name, str):
        return False
    # 指标名称应该符合Prometheus命名规范
    pattern = r"^[a-zA-Z_:][a-zA-Z0-9_:]*$"
    return bool(re.match(pattern, metric_name))


def validate_deployment_name(deployment_name: str) -> bool:
    """验证Kubernetes Deployment名称"""
    if not deployment_name or not isinstance(deployment_name, str):
        return False
    # Kubernetes资源名称规范
    pattern = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
    return bool(re.match(pattern, deployment_name)) and len(deployment_name) <= 253


def validate_namespace(namespace: str) -> bool:
    """验证Kubernetes命名空间"""
    if not namespace or not isinstance(namespace, str):
        return False
    # Kubernetes命名空间规范
    pattern = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
    return bool(re.match(pattern, namespace)) and len(namespace) <= 63


def validate_qps(qps: float) -> bool:
    """验证QPS值"""
    return isinstance(qps, (int, float)) and qps >= 0


# 置信度校验在具体模型层处理，移除通用函数以简化接口


def validate_metric_list(metrics: List[str]) -> bool:
    """验证指标列表"""
    if not metrics or not isinstance(metrics, list):
        return False
    return all(validate_metric_name(metric) for metric in metrics)


def sanitize_input(text: str, max_length: int = 1000) -> str:
    """清理输入文本"""
    if not isinstance(text, str):
        return ""
    # 移除危险字符
    sanitized = re.sub(r'[<>&"\'`]', "", text)
    # 限制长度
    return sanitized[:max_length]
