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
    data = [{"timestamp": "2026-10-22T10:00:00Z", "value": 42}]
    service._get_prometheus_data = AsyncMock(return_value=data)
    assert await service._fetch_historical_data(PredictionType.CPU, None, 24) == data
