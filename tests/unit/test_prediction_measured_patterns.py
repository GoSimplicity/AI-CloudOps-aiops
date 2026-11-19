"""预测模式、量化变化率和孤立森林必须来自实测序列。

不访问 Prometheus、Redis、Kubernetes 或模型服务。固定布尔和用 0 填缺测
都应当失败，修完后这些断言转为通过。
"""

from datetime import datetime, timedelta, timezone
import math

import pytest

from app.core.prediction.anomaly_detector import AnomalyDetector
from app.core.prediction.prediction_analyzer import PredictionAnalyzer
from app.core.prediction.prompt_templates import prompt_builder
from app.models import PredictionDataPoint, PredictionType
from app.services.prediction_service import PredictionService

ORIGIN = datetime(2026, 1, 5, tzinfo=timezone.utc)


def hourly(values, step=timedelta(hours=1)):
    return [
        PredictionDataPoint(timestamp=ORIGIN + step * index, predicted_value=float(value))
        for index, value in enumerate(values)
    ]


def test_short_series_does_not_claim_a_daily_pattern():
    report = PredictionService()._analyze_patterns(hourly([10.0, 12.0, 11.0]))

    assert report["has_periodicity"] is False
    assert report["seasonality"] == {
        "daily_pattern": False,
        "weekly_pattern": False,
        "monthly_pattern": False,
    }


def test_linear_ramp_is_not_reported_as_periodic():
    report = PredictionService()._analyze_patterns(hourly([float(index) for index in range(48)]))

    assert report["has_periodicity"] is False
    assert report["seasonality"]["daily_pattern"] is False
    assert report["seasonality"]["weekly_pattern"] is False
    assert report["seasonality"]["monthly_pattern"] is False
    assert report["volatility"] > 0


def test_flat_series_is_not_reported_as_periodic():
    report = PredictionService()._analyze_patterns(hourly([40.0] * 48))

    assert report["has_periodicity"] is False
    assert report["seasonality"]["daily_pattern"] is False


def test_duplicate_timestamps_are_not_a_daily_cycle():
    points = [
        PredictionDataPoint(timestamp=ORIGIN, predicted_value=float(index))
        for index in range(30)
    ]

    report = PredictionService()._analyze_patterns(points)

    assert report["has_periodicity"] is False
    assert report["seasonality"]["daily_pattern"] is False


def test_hourly_daily_cycle_is_recognized_and_longer_cycles_are_not():
    values = [40.0 + 15.0 * math.sin(2 * math.pi * index / 24) for index in range(72)]
    report = PredictionService()._analyze_patterns(hourly(values))

    assert report["seasonality"]["daily_pattern"] is True
    assert report["seasonality"]["weekly_pattern"] is False
    assert report["seasonality"]["monthly_pattern"] is False
    assert report["has_periodicity"] is True


def test_daily_samples_of_a_weekly_cycle_are_weekly_only():
    values = [10.0 + 5.0 * math.sin(2 * math.pi * index / 7) for index in range(21)]
    report = PredictionService()._analyze_patterns(hourly(values, step=timedelta(days=1)))

    assert report["seasonality"]["daily_pattern"] is False
    assert report["seasonality"]["weekly_pattern"] is True
    assert report["seasonality"]["monthly_pattern"] is False
    assert report["has_periodicity"] is True


def test_zero_baseline_change_is_not_reported_as_zero_percent():
    metrics = PredictionAnalyzer()._calculate_quantitative_metrics(
        {
            "predicted_data": [
                {"predicted_value": 0.0},
                {"predicted_value": 0.0},
                {"predicted_value": 10.0},
                {"predicted_value": 10.0},
            ]
        }
    )

    assert metrics["trend_analysis"]["direction"] == "increasing"
    assert metrics["trend_analysis"]["change_percentage"] is None
    assert metrics["value_statistics"]["mean"] == 5.0


def test_negative_baseline_keeps_the_real_change_percentage():
    metrics = PredictionAnalyzer()._calculate_quantitative_metrics(
        {
            "predicted_data": [
                {"predicted_value": -10.0},
                {"predicted_value": -10.0},
                {"predicted_value": -5.0},
                {"predicted_value": -5.0},
            ]
        }
    )

    assert metrics["trend_analysis"]["direction"] == "increasing"
    assert metrics["trend_analysis"]["change_percentage"] == pytest.approx(-50.0)


def test_missing_or_non_finite_predictions_are_not_filled_with_zero():
    metrics = PredictionAnalyzer()._calculate_quantitative_metrics(
        {
            "predicted_data": [
                {"predicted_value": 4.0},
                {},
                {"predicted_value": None},
                {"predicted_value": float("nan")},
                {"predicted_value": float("inf")},
                {"predicted_value": 6.0},
            ]
        }
    )

    assert metrics["prediction_count"] == 2
    assert metrics["value_statistics"]["mean"] == 5.0
    assert metrics["value_range"] == {"min": 4.0, "max": 6.0}


def test_no_finite_predictions_do_not_invent_a_zero_mean():
    metrics = PredictionAnalyzer()._calculate_quantitative_metrics(
        {"predicted_data": [{}, {"predicted_value": None}, {"predicted_value": float("nan")}]}
    )

    assert metrics["prediction_count"] == 0
    assert metrics["value_statistics"]["mean"] is None
    assert metrics["value_statistics"]["median"] is None
    assert metrics["value_range"] == {}
    assert "trend_analysis" not in metrics


def test_zero_confidence_is_a_measured_value():
    metrics = PredictionAnalyzer()._calculate_quantitative_metrics(
        {
            "predicted_data": [
                {"predicted_value": 1.0, "confidence_level": 0.0},
                {"predicted_value": 1.0, "confidence_level": 0.0},
            ]
        }
    )

    assert metrics["confidence_statistics"]["mean_confidence"] == 0.0
    assert metrics["confidence_statistics"]["min_confidence"] == 0.0

    prompt = prompt_builder.build_interpretation_prompt(
        PredictionType.CPU,
        {
            "predicted_data": [{"predicted_value": 1.0, "confidence_level": 0.0}],
            "anomaly_predictions": [],
            "prediction_summary": {"trend": "stable", "peak_time": None},
        },
    )
    assert "0.00" in prompt
    assert "暂无置信度信息" not in prompt


def _spread_spike():
    baseline = [8.0 + (index % 5) for index in range(20)]
    return hourly(baseline + [100.0] + baseline)


async def test_isolation_forest_does_not_delegate_to_mad(monkeypatch):
    detector = AnomalyDetector()
    calls = {"mad": 0}
    original = detector._mad_detection

    def spy(values, sensitivity):
        calls["mad"] += 1
        return original(values, sensitivity)

    monkeypatch.setattr(detector, "_mad_detection", spy)
    found = await detector.detect_anomalies(_spread_spike(), sensitivity=0.9, method="isolation")

    assert calls["mad"] == 0
    assert any(item.predicted_value == 100.0 for item in found)
    assert all(0.0 <= item.anomaly_score <= 1.0 for item in found)


async def test_isolation_forest_does_not_flag_a_flat_series():
    found = await AnomalyDetector().detect_anomalies(
        hourly([5.0] * 24), sensitivity=0.95, method="isolation"
    )
    assert found == []
