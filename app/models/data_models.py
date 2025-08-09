#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 核心数据模型 - 定义系统中使用的指标、异常、相关性和智能体状态等内部数据结构
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd


@dataclass
class MetricData:
    name: str
    values: pd.DataFrame
    labels: Dict[str, str]
    unit: Optional[str] = None
    source: Optional[str] = None


@dataclass
class AnomalyResult:
    metric: str
    anomaly_points: List[datetime]
    anomaly_scores: List[float]
    detection_methods: Dict[str, Any]
    severity: str = "medium"  # low, medium, high


@dataclass
class CorrelationResult:
    metric_pairs: List[tuple]
    correlation_matrix: pd.DataFrame
    significant_correlations: Dict[str, List[tuple]]
    method: str = "pearson"


@dataclass
class AgentState:
    messages: List[Dict[str, Any]]
    current_step: str
    context: Dict[str, Any]
    next_action: Optional[str] = None
    iteration_count: int = 0
    max_iterations: int = 10


@dataclass
class PredictionFeatures:
    qps: float
    sin_time: float
    cos_time: float
    hour: int
    day_of_week: int
    timestamp: datetime
