"""异常检测器：空输入、各检测法和模式汇总。不访问外部服务。"""

from datetime import datetime, timedelta, timezone
import warnings

import pytest

from app.core.prediction.anomaly_detector import AnomalyDetector
from app.models import AnomalyPrediction, PredictionDataPoint


def points(values, start=None):
    origin = start or datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    return [
        PredictionDataPoint(timestamp=origin + timedelta(hours=index), predicted_value=float(value))
        for index, value in enumerate(values)
    ]


def anomaly(hour, score=0.9, kind="spike", impact="critical"):
    return AnomalyPrediction(
        timestamp=datetime(2026, 1, 5, hour, 0, tzinfo=timezone.utc),
        anomaly_score=score,
        anomaly_type=kind,
        impact_level=impact,
        predicted_value=100.0,
        expected_value=10.0,
    )


async def test_empty_predictions_have_no_anomalies():
    assert await AnomalyDetector().detect_anomalies([]) == []


def _spread_spike():
    baseline = [8.0 + (index % 5) for index in range(20)]
    return points(baseline + [100.0] + baseline)


async def test_default_detector_flags_a_middle_spike():
    series = _spread_spike()
    found = await AnomalyDetector().detect_anomalies(series)
    spikes = [item for item in found if item.predicted_value == 100.0]
    assert spikes
    assert all(0.0 <= item.anomaly_score <= 1.0 for item in found)
    assert all(item.impact_level in {"low", "medium", "high", "critical"} for item in found)
    assert any("spike" in item.anomaly_type for item in spikes)


@pytest.mark.parametrize("method", ["zscore", "iqr", "mad", "isolation", "not-a-method"])
async def test_named_methods_and_unknown_method_return_scored_anomalies(method):
    series = _spread_spike()
    found = await AnomalyDetector().detect_anomalies(series, sensitivity=0.9, method=method)
    assert any(item.predicted_value == 100.0 for item in found)
    assert all(0.0 <= item.anomaly_score <= 1.0 for item in found)


@pytest.mark.xfail(strict=True, reason="已知问题：四分位距为 0 时异常分数除以 0，见 PR 描述")
async def test_iqr_with_zero_quartile_width_does_not_divide_by_zero():
    series = points([10.0] * 20 + [100.0] + [10.0] * 20)
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        found = await AnomalyDetector().detect_anomalies(series, sensitivity=0.9, method="iqr")
    assert any(item.predicted_value == 100.0 for item in found)
    assert all(0.0 <= item.anomaly_score <= 1.0 for item in found)


async def test_flat_series_does_not_raise():
    found = await AnomalyDetector().detect_anomalies(points([5.0] * 12), sensitivity=0.8, method="zscore")
    assert found == []


async def test_dip_is_classified_separately_from_spike():
    series = points([50.0] * 12 + [0.0] + [50.0] * 12)
    found = await AnomalyDetector().detect_anomalies(series, sensitivity=0.95, method="zscore")
    dips = [item for item in found if item.predicted_value == 0.0]
    assert dips
    assert any("dip" in item.anomaly_type for item in dips)


async def test_invalid_prediction_values_are_not_swallowed():
    class Broken:
        predicted_value = object()
        timestamp = datetime(2026, 1, 5, tzinfo=timezone.utc)

    with pytest.raises((TypeError, ValueError)):
        await AnomalyDetector().detect_anomalies([Broken(), Broken(), Broken()])


def test_pattern_summary_covers_clusters_hours_and_severity():
    detector = AnomalyDetector()
    assert detector.analyze_anomaly_patterns([]) == {"total_anomalies": 0, "patterns": []}

    business = [anomaly(hour) for hour in (10, 11, 12, 14, 16)]
    summary = detector.analyze_anomaly_patterns(business)
    assert summary["total_anomalies"] == 5
    assert summary["type_distribution"]["spike"] == 5
    assert summary["impact_distribution"]["critical"] == 5
    assert summary["clusters"]
    assert summary["clusters"][0]["anomaly_count"] >= 2
    assert 0.0 <= summary["severity_score"] <= 1.0
    assert "business_hours_concentration" in summary["patterns"]
    assert "predominantly_spikes" in summary["patterns"]
    assert "high_severity_concentration" in summary["patterns"]

    overnight = [anomaly(hour, kind="dip", impact="low") for hour in (1, 2, 3, 23)]
    overnight_summary = detector.analyze_anomaly_patterns(overnight)
    assert "off_hours_concentration" in overnight_summary["patterns"]
    assert "predominantly_dips" in overnight_summary["patterns"]

    separated = [anomaly(1), anomaly(8)]
    assert detector.analyze_anomaly_patterns(separated)["clusters"] == []
