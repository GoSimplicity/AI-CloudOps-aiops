"""AI 增强预测不可用或失败时，预测服务降级到基础预测，并由规则引擎出数。"""

from unittest.mock import AsyncMock, Mock

import pytest

from app.common.exceptions import HistoricalDataError, ValidationError
from app.services.prediction_service import PredictionService


def _history():
    return [
        {"timestamp": "2026-01-01T00:00:00+00:00", "value": 100.0},
        {"timestamp": "2026-01-01T01:00:00+00:00", "value": 110.0},
    ]


@pytest.fixture
def offline_cache(monkeypatch):
    from app.core.cache import redis_cache_manager

    monkeypatch.setattr(redis_cache_manager, "RedisCacheManager", Mock(side_effect=RuntimeError("offline cache unavailable")))


async def _service():
    service = PredictionService()
    service._get_prometheus_data = AsyncMock(return_value=_history())
    await service.initialize()
    return service


def _values(result):
    return [point["predicted_value"] for point in result["predicted_data"]]


async def test_missing_ai_component_uses_rule_prediction_for_each_resource(offline_cache):
    service = await _service()
    service._intelligent_predictor = None
    cases = (
        ("qps", 100.0),
        ("cpu", 40.0),
        ("memory", 55.0),
        ("disk", 70.0),
    )
    for prediction_type, current in cases:
        result = await service.predict_with_ai_analysis(
            prediction_type=prediction_type,
            current_value=current,
            prediction_hours=2,
            granularity="hour",
        )
        assert result["ai_enhanced"] is False
        assert result["ai_fallback_reason"] == "AI增强组件不可用"
        assert result["ai_insights"] == [
            "当前使用基础预测模式",
            "AI增强功能暂时不可用",
            "预测结果基于传统算法生成",
        ]
        assert result["fallback_mode"] is True
        assert str(result["method"]).startswith("rule")
        values = _values(result)
        assert len(values) == 2
        assert all(value >= 0 for value in values)
        if prediction_type == "qps":
            assert all(value <= 100000 for value in values)
        else:
            assert all(value <= 100 for value in values)


async def test_ai_exception_still_returns_rule_prediction_without_the_error_text(offline_cache):
    service = await _service()
    service._intelligent_predictor = Mock()
    service._intelligent_predictor.predict_with_ai_analysis = AsyncMock(
        side_effect=RuntimeError("downstream-failure-marker")
    )
    result = await service.predict_with_ai_analysis("cpu", 35.0, prediction_hours=1, granularity="hour")
    assert result["ai_enhanced"] is False
    assert result["prediction_type"].value == "cpu"
    assert _values(result)
    assert "downstream-failure-marker" not in str(result)


@pytest.mark.xfail(strict=True, reason="已知问题：增强预测抛错时 ai_fallback_reason 仍写成组件不可用，见 PR 描述")
async def test_ai_exception_reports_analysis_failure(offline_cache):
    service = await _service()
    service._intelligent_predictor = Mock()
    service._intelligent_predictor.predict_with_ai_analysis = AsyncMock(side_effect=RuntimeError("downstream-failure-marker"))
    result = await service.predict_with_ai_analysis("cpu", 35.0, prediction_hours=1, granularity="hour")
    assert result["ai_fallback_reason"] == "AI分析失败"


async def test_validation_and_historical_data_errors_are_not_converted_to_fallback(offline_cache):
    service = await _service()
    service._intelligent_predictor = None
    with pytest.raises(ValidationError):
        await service.predict_with_ai_analysis("qps", -1)
    with pytest.raises(ValidationError):
        await service.predict_with_ai_analysis("cpu", 101, prediction_hours=1)
    service._intelligent_predictor = object()
    service._fetch_historical_data = AsyncMock(side_effect=HistoricalDataError("no samples"))
    with pytest.raises(HistoricalDataError):
        await service.predict_with_ai_analysis("memory", 20.0, prediction_hours=1)
