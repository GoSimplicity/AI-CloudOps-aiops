#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from unittest.mock import AsyncMock

import pytest

from app.services.prediction_service import PredictionService


@pytest.mark.asyncio
async def test_predict_service_initialization():
    service = PredictionService()
    await service.initialize()
    assert service.is_initialized() is True


@pytest.mark.asyncio
async def test_predict_qps_basic_shape():
    service = PredictionService()
    service._get_prometheus_data = AsyncMock(return_value=[
        {"timestamp": "2026-10-22T10:00:00", "value": 100.0},
        {"timestamp": "2026-10-22T11:00:00", "value": 105.0},
    ])
    await service.initialize()
    result = await service.predict_qps(current_qps=100.0, prediction_hours=6)
    assert isinstance(result, dict)
    assert "prediction_type" in result
    assert "predicted_data" in result
