#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
按已加载模型声明的特征读取实测历史。

已核对：预测器原先读取 metadata["metadata"]["features"]。
实际元数据把 features 放在顶层，因此那段查找始终落空，接着套用预测器自己的默认清单，
再用常数补齐。特征提取器、预测器和管理器三份默认清单的顺序和列并不相同，不能拿来猜模型输入。
"""

from datetime import datetime, timedelta, timezone
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np

from app.common.exceptions import PredictionError

HISTORY_REASON = "模型所需真实历史不足"
SCHEMA_REASON = "模型输入结构无法确定"
UNAVAILABLE_REASON = "预测模型不可用"
PREDICT_FAILED_REASON = "模型预测失败"
ADAPTED_REASON = "使用其他指标的模型"

CODE_HISTORY = "MODEL_HISTORY_INSUFFICIENT"
CODE_SCHEMA = "MODEL_SCHEMA_UNKNOWN"
CODE_UNAVAILABLE = "MODEL_UNAVAILABLE"
CODE_PREDICT_FAILED = "MODEL_PREDICT_FAILED"
CODE_ADAPTED = "ADAPTED_MODEL"

FALLBACK_CODES = {
    UNAVAILABLE_REASON: CODE_UNAVAILABLE,
    PREDICT_FAILED_REASON: CODE_PREDICT_FAILED,
    ADAPTED_REASON: CODE_ADAPTED,
    HISTORY_REASON: CODE_HISTORY,
    SCHEMA_REASON: CODE_SCHEMA,
}

# 两个整点样本的最小查询跨度。结构不明时只用它，不按猜出来的周特征把窗口扩到 168 小时。
STRUCTURAL_QUERY_HOURS = 1

TIME_FEATURES = (
    "sin_time",
    "cos_time",
    "sin_day",
    "cos_day",
    "is_business_hour",
    "is_weekend",
    "is_holiday",
)

# 数值是相对最新有限观测 T 的小时差。6 小时窗口是 T 到 T-5，24 小时窗口是 T 到 T-23。
_ROLE_SPAN = {
    "lag_1h": 1,
    "lag_24h": 24,
    "lag_168h": 168,
    "avg_6h": 5,
    "std_6h": 5,
    "max_6h": 5,
    "min_6h": 5,
    "avg_24h": 23,
    "max_24h": 23,
    "growth_rate": 23,
    "change": 0,
    "trend": 0,
    "current": 0,
    "time": 0,
}

_SUFFIX_ROLE = {
    "_1h_ago": "lag_1h",
    "_1d_ago": "lag_24h",
    "_1w_ago": "lag_168h",
    "_change": "change",
    "_avg_6h": "avg_6h",
    "_std_6h": "std_6h",
    "_max_6h": "max_6h",
    "_min_6h": "min_6h",
    "_avg_24h": "avg_24h",
    "_max_24h": "max_24h",
    "_trend": "trend",
    "_growth_rate": "growth_rate",
}

_METRIC_PREFIXES = ("QPS", "CPU", "MEMORY", "DISK")


class MeasuredHistoryError(PredictionError):
    """实测特征无法形成。消息是稳定原因，不附带依赖原文。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def fallback_code_for(reason: Optional[str]) -> Optional[str]:
    if reason is None:
        return None
    return FALLBACK_CODES.get(reason)


def feature_role(name: str) -> Optional[str]:
    if name in TIME_FEATURES:
        return "time"
    for prefix in _METRIC_PREFIXES:
        if name == prefix:
            return "current"
        if name.startswith(prefix + "_"):
            return _SUFFIX_ROLE.get(name[len(prefix):])
    return None


def _declared_width(estimator: Any) -> Optional[int]:
    if estimator is None:
        return None
    width = getattr(estimator, "n_features_in_", None)
    if isinstance(width, bool) or not isinstance(width, (int, np.integer)):
        return None
    if int(width) <= 0:
        return None
    return int(width)


def resolve_model_schema(
    model: Any, metadata: Optional[Mapping[str, Any]], scaler: Any = None
) -> tuple[Optional[List[str]], Optional[str]]:
    """返回已声明且维度一致的特征名，或结构无法确定的原因。不回退到默认清单。"""
    if not isinstance(metadata, Mapping) or metadata.get("metadata_missing"):
        return None, SCHEMA_REASON
    features = metadata.get("features")
    if (
        not isinstance(features, list)
        or not features
        or any(not isinstance(name, str) or not name for name in features)
        or len(set(features)) != len(features)
        or any(feature_role(name) is None for name in features)
    ):
        return None, SCHEMA_REASON
    width = len(features)
    for estimator in (model, scaler):
        declared = _declared_width(estimator)
        if declared is not None and declared != width:
            return None, SCHEMA_REASON
    return list(features), None


def query_hours_for_features(features: Optional[Sequence[str]]) -> int:
    """查询跨度等于模型特征所需的最大回看。没有可确定的特征时只取结构门禁的最小跨度。"""
    if not features:
        return STRUCTURAL_QUERY_HOURS
    span = 0
    for name in features:
        role = feature_role(name)
        if role is None:
            return STRUCTURAL_QUERY_HOURS
        span = max(span, _ROLE_SPAN[role])
    return max(span, STRUCTURAL_QUERY_HOURS)


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _observations(historical_data: Sequence[Mapping[str, Any]]) -> Dict[datetime, float]:
    observed: Dict[datetime, float] = {}
    for point in historical_data:
        if not isinstance(point, Mapping) or "timestamp" not in point or "value" not in point:
            raise MeasuredHistoryError(HISTORY_REASON)
        try:
            moment = _as_utc(point["timestamp"])
            value = float(point["value"])
        except (TypeError, ValueError, OverflowError):
            raise MeasuredHistoryError(HISTORY_REASON) from None
        if not math.isfinite(value) or moment in observed:
            raise MeasuredHistoryError(HISTORY_REASON)
        observed[moment] = value
    return observed


def _value_at(observed: Mapping[datetime, float], moment: datetime) -> float:
    if moment not in observed:
        raise MeasuredHistoryError(HISTORY_REASON)
    return observed[moment]


def _window(observed: Mapping[datetime, float], anchor: datetime, span: int) -> List[float]:
    return [_value_at(observed, anchor - timedelta(hours=delta)) for delta in range(span, -1, -1)]


def measured_feature_row(
    feature_names: Sequence[str],
    current_value: float,
    historical_data: Sequence[Mapping[str, Any]],
    time_features: Mapping[str, Any],
) -> Dict[str, float]:
    """按精确时间戳取实测值。缺测点、非有限值或未知列名时失败，不回填最早值或常数。"""
    if any(feature_role(name) is None for name in feature_names):
        raise MeasuredHistoryError(SCHEMA_REASON)
    try:
        current = float(current_value)
    except (TypeError, ValueError, OverflowError):
        raise MeasuredHistoryError(HISTORY_REASON) from None
    if not math.isfinite(current):
        raise MeasuredHistoryError(HISTORY_REASON)

    observed = _observations(historical_data or [])
    needs_anchor = any(feature_role(name) not in {"time", "current"} for name in feature_names)
    anchor = max(observed) if observed else None
    if needs_anchor and anchor is None:
        raise MeasuredHistoryError(HISTORY_REASON)

    row: Dict[str, float] = {}
    for name in feature_names:
        role = feature_role(name)
        if role == "time":
            if name not in time_features:
                raise MeasuredHistoryError(SCHEMA_REASON)
            try:
                value = float(time_features[name])
            except (TypeError, ValueError, OverflowError):
                raise MeasuredHistoryError(SCHEMA_REASON) from None
        elif role == "current":
            value = current
        elif role == "lag_1h":
            value = _value_at(observed, anchor - timedelta(hours=1))
        elif role == "lag_24h":
            value = _value_at(observed, anchor - timedelta(hours=24))
        elif role == "lag_168h":
            value = _value_at(observed, anchor - timedelta(hours=168))
        elif role == "avg_6h":
            value = float(np.mean(_window(observed, anchor, 5)))
        elif role == "std_6h":
            value = float(np.std(_window(observed, anchor, 5)))
        elif role == "max_6h":
            value = float(np.max(_window(observed, anchor, 5)))
        elif role == "min_6h":
            value = float(np.min(_window(observed, anchor, 5)))
        elif role == "avg_24h":
            value = float(np.mean(_window(observed, anchor, 23)))
        elif role == "max_24h":
            value = float(np.max(_window(observed, anchor, 23)))
        elif role == "growth_rate":
            window = _window(observed, anchor, 23)
            old_avg = float(np.mean(window[:12]))
            new_avg = float(np.mean(window[12:]))
            value = 0.0 if old_avg == 0.0 else (new_avg - old_avg) / old_avg
        elif role == "trend":
            if len(observed) < 2:
                raise MeasuredHistoryError(HISTORY_REASON)
            ordered = [observed[moment] for moment in sorted(observed)]
            value = float(np.polyfit(np.arange(len(ordered), dtype=float), np.asarray(ordered, dtype=float), 1)[0])
        elif role == "change":
            baseline = _value_at(observed, anchor)
            value = 0.0 if baseline == 0.0 else (current - baseline) / baseline
        else:
            raise MeasuredHistoryError(SCHEMA_REASON)
        if not math.isfinite(value):
            raise MeasuredHistoryError(HISTORY_REASON)
        row[name] = float(value)
    return row
