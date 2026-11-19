#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 只根据实测预测序列判断周期，样本不够或去趋势后没有重复就不声称有季节性
"""

from datetime import datetime
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

_DAY_SECONDS = 86400.0
_WEEK_SECONDS = 7 * _DAY_SECONDS
_MONTH_SECONDS = 30 * _DAY_SECONDS
_MIN_CORRELATION = 0.6


def measured_pattern_report(predictions: Sequence[Any]) -> Dict[str, Any]:
    """返回周期判断。键与原模式分析一致，没有证据时全部为 false。"""

    seasonality = {
        "daily_pattern": False,
        "weekly_pattern": False,
        "monthly_pattern": False,
    }
    samples = _finite_samples(predictions)
    if len(samples) < 4:
        return {"has_periodicity": False, "seasonality": seasonality}

    samples.sort(key=lambda item: item[0])
    steps = [
        (samples[index][0] - samples[index - 1][0]).total_seconds()
        for index in range(1, len(samples))
    ]
    if any(step <= 0 for step in steps):
        return {"has_periodicity": False, "seasonality": seasonality}

    step_seconds = float(np.median(steps))
    if step_seconds <= 0 or any(step < step_seconds * 0.5 or step > step_seconds * 1.5 for step in steps):
        return {"has_periodicity": False, "seasonality": seasonality}

    residual = _detrend(np.array([value for _, value in samples], dtype=float))
    seasonality["daily_pattern"] = _has_cycle(residual, samples, step_seconds, _DAY_SECONDS)
    seasonality["weekly_pattern"] = _has_cycle(residual, samples, step_seconds, _WEEK_SECONDS)
    seasonality["monthly_pattern"] = _has_cycle(residual, samples, step_seconds, _MONTH_SECONDS)
    return {"has_periodicity": any(seasonality.values()), "seasonality": seasonality}


def _finite_samples(predictions: Sequence[Any]) -> List[Tuple[datetime, float]]:
    samples: List[Tuple[datetime, float]] = []
    for point in predictions:
        timestamp = getattr(point, "timestamp", None)
        value = _finite_number(getattr(point, "predicted_value", None))
        if not isinstance(timestamp, datetime) or value is None:
            continue
        samples.append((timestamp, value))
    return samples


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _detrend(values: np.ndarray) -> np.ndarray:
    if len(values) < 2:
        return values
    position = np.arange(len(values), dtype=float)
    slope, intercept = np.polyfit(position, values, 1)
    return values - (slope * position + intercept)


def _has_cycle(
    residual: np.ndarray,
    samples: Sequence[Tuple[datetime, float]],
    step_seconds: float,
    period_seconds: float,
) -> bool:
    # 采样比周期粗时，看不到这个周期，不能用更长的平滑相关代替。
    if step_seconds > period_seconds / 4:
        return False
    lag = int(round(period_seconds / step_seconds))
    if lag < 2 or len(residual) < lag * 2 + 1:
        return False
    span = (samples[-1][0] - samples[0][0]).total_seconds()
    if span < period_seconds * 2:
        return False
    correlation = _lag_correlation(residual, lag)
    return correlation is not None and correlation >= _MIN_CORRELATION


def _lag_correlation(values: np.ndarray, lag: int) -> Optional[float]:
    left = values[:-lag]
    right = values[lag:]
    if len(left) < 3 or float(np.std(left)) < 1e-8 or float(np.std(right)) < 1e-8:
        return None
    correlation = float(np.corrcoef(left, right)[0, 1])
    if not math.isfinite(correlation):
        return None
    return correlation
