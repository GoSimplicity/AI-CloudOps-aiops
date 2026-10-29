"""Evaluate recorded forecasts without using observations unavailable at issue time."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Sequence


@dataclass(frozen=True)
class ForecastObservation:
    issued_at: datetime
    target_at: datetime
    observed_at: datetime
    forecast: float
    actual: float


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) <= 1e100 and math.isfinite(value)


def _percentile(values: Sequence[float], quantile: float) -> float:
    """Nearest-rank empirical quantile; no distributional assumption."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def rolling_backtest(
    records: Sequence[ForecastObservation], *, window: int = 200, min_history: int = 20,
) -> dict:
    """Evaluate one metric/model/horizon series of already-recorded predictions.

    Residuals enter the calibration window only once their observed_at timestamp
    is no later than the forecast issue time. The inputs are supplied evidence,
    not proof that the model was trained without those observations.
    """
    if not 1 <= len(records) <= 10000:
        raise ValueError("回测记录数量须为 1 至 10000")
    if not 20 <= window <= 1000 or not 20 <= min_history <= window:
        raise ValueError("回测窗口或最小历史样本数无效")
    targets: set[datetime] = set()
    horizons: set[float] = set()
    for row in records:
        if any(value.tzinfo is None or value.utcoffset() is None for value in (row.issued_at, row.target_at, row.observed_at)):
            raise ValueError("回测时间必须包含时区")
        if row.issued_at >= row.target_at or row.observed_at < row.target_at:
            raise ValueError("预测须先于目标，观测不能早于目标")
        if not _finite(row.forecast) or not _finite(row.actual):
            raise ValueError("预测与观测必须为有限数值")
        if row.target_at in targets:
            raise ValueError("目标时间不能重复")
        targets.add(row.target_at)
        horizons.add((row.target_at - row.issued_at).total_seconds())
    if len(horizons) != 1:
        raise ValueError("不同预测跨度须分别回测")

    # Process issues in order and add only observations available at that time.
    issued = sorted(records, key=lambda row: row.issued_at)
    observed = sorted(records, key=lambda row: (row.observed_at, row.target_at))
    recent: deque[float] = deque(maxlen=window)
    next_observation = 0
    covered = eligible = 0
    for row in issued:
        while next_observation < len(observed) and observed[next_observation].observed_at <= row.issued_at:
            known = observed[next_observation]
            recent.append(known.actual - known.forecast)
            next_observation += 1
        if len(recent) >= min_history:
            low = row.forecast + _percentile(recent, 0.05)
            high = row.forecast + _percentile(recent, 0.95)
            eligible += 1
            covered += low <= row.actual <= high

    residuals = [row.actual - row.forecast for row in records]
    absolute = [abs(value) for value in residuals]
    total_actual = sum(abs(row.actual) for row in records)
    # Summing normalized squares avoids overflow for large, finite values.
    scale = max(absolute)
    rmse = scale * math.sqrt(sum((value / scale) ** 2 for value in absolute) / len(absolute)) if scale else 0.0
    if not math.isfinite(rmse) or not math.isfinite(total_actual) or not math.isfinite(sum(absolute)) or not math.isfinite(sum(residuals)):
        raise ValueError("回测数据超出数值范围")
    wape = sum(absolute) / total_actual if total_actual else None
    if wape is not None and not math.isfinite(wape):
        raise ValueError("回测数据超出数值范围")
    calibrated = [row.actual - row.forecast for row in observed[-window:]]
    enough = len(calibrated) >= min_history
    return {
        "sample_count": len(records),
        "horizon_seconds": next(iter(horizons)),
        "mae": sum(absolute) / len(records),
        "rmse": rmse,
        "bias": sum(residuals) / len(records),
        "wape": wape,
        "calibration": {
            "method": "rolling_empirical_residual_90_percent",
            "window": window,
            "minimum_history": min_history,
            "evaluated_count": eligible,
            "observed_coverage": covered / eligible if eligible else None,
            "residual_lower": _percentile(calibrated, 0.05) if enough else None,
            "residual_upper": _percentile(calibrated, 0.95) if enough else None,
            "reference_time": observed[-1].observed_at.isoformat(),
        },
        "evidence": "用户提供的预测及观测记录；未验证训练集与回测集是否隔离",
    }
