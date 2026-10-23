from unittest.mock import AsyncMock

import pytest

from app.common.exceptions import PredictionError
from app.models import PredictionType
from app.services.prediction_service import PredictionService


@pytest.mark.parametrize("data", [None, []])
async def test_missing_metrics_never_become_synthetic_history(data):
    service = PredictionService()
    service._get_prometheus_data = AsyncMock(return_value=data)
    with pytest.raises(PredictionError, match="No historical metrics"):
        await service._fetch_historical_data(PredictionType.CPU, None, 24)


async def test_failed_metrics_never_become_synthetic_history():
    service = PredictionService()
    service._get_prometheus_data = AsyncMock(side_effect=ConnectionError("offline"))
    with pytest.raises(PredictionError, match="unavailable"):
        await service._fetch_historical_data(PredictionType.CPU, None, 24)


async def test_preserves_observed_history():
    service = PredictionService()
    data = [
        {"timestamp": "2026-10-22T10:00:00Z", "value": 0},
        {"timestamp": "2026-10-22T11:00:00Z", "value": 42},
    ]
    service._get_prometheus_data = AsyncMock(return_value=data)
    assert await service._fetch_historical_data(PredictionType.CPU, None, 24) == data


@pytest.mark.parametrize("values", [[float("nan")], [float("inf")], [float("-inf")], [None]])
def test_conversion_never_turns_invalid_values_into_zero(values):
    import pandas as pd

    df = pd.DataFrame({"value": values}, index=pd.to_datetime(["2026-10-22T10:00:00Z"]))
    assert PredictionService()._convert_prometheus_data(df) == []


async def test_single_observation_is_not_sufficient_for_prediction():
    service = PredictionService()
    service._get_prometheus_data = AsyncMock(return_value=[{"timestamp": "2026-10-22T10:00:00Z", "value": 0}])
    with pytest.raises(PredictionError, match="Insufficient"):
        await service._fetch_historical_data(PredictionType.CPU, None, 24)


async def test_gap_in_hourly_history_prevents_prediction():
    service = PredictionService()
    service._get_prometheus_data = AsyncMock(return_value=[
        {"timestamp": "2026-10-22T10:00:00Z", "value": 0},
        {"timestamp": "2026-10-22T12:00:00Z", "value": 3},
    ])
    with pytest.raises(PredictionError, match="hourly"):
        await service._fetch_historical_data(PredictionType.CPU, None, 24)


def test_prediction_rejects_ambiguous_multiple_series():
    import pandas as pd

    df = pd.DataFrame({"value": [2, 8]}, index=pd.to_datetime(["2026-10-22T10:00:00Z"] * 2))
    with pytest.raises(PredictionError, match="aggregate"):
        PredictionService()._convert_prometheus_data(df)


@pytest.mark.parametrize("data,status,reason", [
    ([], 422, "No historical metrics"),
    ([{"timestamp": "2026-10-22T10:00:00Z", "value": 0}], 422, "Insufficient"),
    ([{"timestamp": "2026-10-22T10:00:00Z", "value": 0}, {"timestamp": "2026-10-22T12:00:00Z", "value": 3}], 422, "hourly"),
    ([{"timestamp": "2026-10-22T10:00:00Z", "value": float("nan")}, {"timestamp": "2026-10-22T11:00:00Z", "value": 3}], 422, "finite"),
    (ConnectionError("offline dependency"), 502, "unavailable"),
])
def test_prediction_api_reports_data_rejection_with_valid_json(monkeypatch, data, status, reason):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.middleware.error_handler import setup_error_handlers
    from app.api.routes import predict

    service = PredictionService()
    service._initialized = True
    service._get_from_cache = AsyncMock(return_value=None)
    service._predictor = AsyncMock()
    if isinstance(data, Exception):
        service._get_prometheus_data = AsyncMock(side_effect=data)
    else:
        service._get_prometheus_data = AsyncMock(return_value=data)
    monkeypatch.setattr(predict, "get_prediction_service", AsyncMock(return_value=service))
    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(predict.router, prefix="/predict")
    response = TestClient(app, raise_server_exceptions=False).post("/predict/qps", json={"current_qps": 10})
    assert response.status_code == status
    body = response.json()
    assert body["code"] == status
    if status == 422:
        assert reason in body["message"]
    assert "NaN" not in response.text and "Infinity" not in response.text
    service._predictor.predict.assert_not_awaited()


async def test_failed_upstream_health_is_not_classified_as_missing_data(monkeypatch):
    from app.common.exceptions import HistoricalDataError
    from app.services.prometheus import PrometheusService

    monkeypatch.setattr(PrometheusService, "health_check", AsyncMock(return_value=False))
    with pytest.raises(PredictionError) as exc:
        await PredictionService()._fetch_historical_data(PredictionType.CPU, None, 24)
    assert not isinstance(exc.value, HistoricalDataError)
