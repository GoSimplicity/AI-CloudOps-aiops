"""预测输出时间契约：离线跨部署时区与夏令时验证"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from app.core.prediction import unified_predictor
from app.core.prediction.cost_analyzer import CostAnalyzer
from app.core.prediction.feature_extractor import FeatureExtractor
from app.core.prediction.scaling_advisor import ScalingAdvisor
from app.models import PredictionGranularity, PredictionType

FIXED_UTC = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)


@contextmanager
def system_timezone(zone):
    """只切换本测试进程时区，结束后恢复，不改机器配置"""
    original = os.environ.get('TZ')
    os.environ['TZ'] = zone
    time.tzset()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = original
        time.tzset()


def freeze_clock(monkeypatch, instant=FIXED_UTC):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.astimezone().replace(tzinfo=None)
            return instant.astimezone(tz)

    monkeypatch.setattr(unified_predictor, 'datetime', Clock)


class RecordingFeatures(FeatureExtractor):
    def __init__(self):
        super().__init__()
        self.timestamps = []

    async def extract_features(self, timestamp, **kwargs):
        self.timestamps.append(timestamp)
        return await super().extract_features(timestamp=timestamp, **kwargs)


class ZeroModel:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def predict(self, values):
        self.calls += 1
        if self.fail:
            raise RuntimeError('offline model unavailable')
        assert np.isfinite(values).all()
        return np.array([0.0])


async def make_predictor(path):
    model = None if path == 'rule' else ZeroModel(fail=path == 'failed_model')
    manager = SimpleNamespace(models_loaded=True, metadata={}, get_model=lambda _: model, get_scaler=lambda _: None)
    features = RecordingFeatures()
    predictor = unified_predictor.UnifiedPredictor(manager, features)
    await predictor.initialize()
    return predictor, features, model


@pytest.mark.parametrize('zone', ['UTC', 'Asia/Shanghai', 'America/New_York'])
@pytest.mark.parametrize('path', ['model', 'rule', 'failed_model'])
@pytest.mark.parametrize(
    'granularity,hours,count,step',
    [
        (PredictionGranularity.MINUTE, 1, 60, timedelta(minutes=1)),
        (PredictionGranularity.HOUR, 3, 3, timedelta(hours=1)),
        (PredictionGranularity.DAY, 48, 2, timedelta(days=1)),
    ],
)
async def test_generated_points_are_utc_with_exact_step_and_real_zero(monkeypatch, zone, path, granularity, hours, count, step):
    freeze_clock(monkeypatch)
    with system_timezone(zone):
        predictor, features, model = await make_predictor(path)
        points = await predictor.predict(PredictionType.CPU, 0.0, [], hours, granularity)
        assert len(points) == count
        for index, point in enumerate(points, start=1):
            assert point.timestamp.tzinfo is not None and point.timestamp.utcoffset() == timedelta(0)
            assert point.timestamp == FIXED_UTC + step * index
            assert point.predicted_value == point.confidence_lower == point.confidence_upper == 0.0
        if path == 'model':
            assert model.calls == count
            assert features.timestamps
            assert all(value.tzinfo is None for value in features.timestamps)
            assert features.timestamps[0] == (FIXED_UTC + step).astimezone().replace(tzinfo=None)


@pytest.mark.parametrize(
    'instant',
    [
        datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc),
        datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc),
    ],
)
@pytest.mark.parametrize('path', ['model', 'rule'])
async def test_dst_transition_never_duplicates_or_skips_elapsed_hour(monkeypatch, instant, path):
    freeze_clock(monkeypatch, instant)
    with system_timezone('America/New_York'):
        predictor, _, _ = await make_predictor(path)
        points = await predictor.predict(PredictionType.CPU, 0.0, [], 4, PredictionGranularity.HOUR)
        assert [point.timestamp for point in points] == [instant + timedelta(hours=index) for index in range(1, 5)]
        assert len({point.timestamp.timestamp() for point in points}) == 4


async def test_rule_retains_local_business_hour_factor(monkeypatch):
    freeze_clock(monkeypatch)
    with system_timezone('Asia/Shanghai'):
        predictor, _, _ = await make_predictor('rule')
        points = await predictor.predict(PredictionType.CPU, 50.0, [], 1, PredictionGranularity.HOUR)
        assert points[0].timestamp == datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)
        assert points[0].predicted_value == pytest.approx(60.6)


async def test_real_advisor_and_asgi_response_preserve_utc_trigger_and_zero(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import predict

    freeze_clock(monkeypatch)
    with system_timezone('Asia/Shanghai'):
        predictor, _, _ = await make_predictor('rule')
        points = await predictor.predict(PredictionType.CPU, 0.0, [], 3, PredictionGranularity.HOUR)
        recommendations = await ScalingAdvisor().generate_recommendations(points, PredictionType.CPU)
        assert recommendations and recommendations[0].trigger_time == points[0].timestamp
        assert CostAnalyzer()._find_recommendation_for_time(points[0].timestamp, recommendations) is recommendations[0]
        payload = {
            'prediction_type': 'cpu',
            'prediction_hours': 3,
            'granularity': 'hour',
            'current_value': 0.0,
            'predicted_data': [point.model_dump() for point in points],
            'scaling_recommendations': [item.model_dump() for item in recommendations],
        }
        service = SimpleNamespace(initialize=AsyncMock(), predict_cpu_utilization=AsyncMock(return_value=payload))
        monkeypatch.setattr(predict, 'get_prediction_service', AsyncMock(return_value=service))
        app = FastAPI()
        app.include_router(predict.router, prefix='/predict')
        response = TestClient(app).post('/predict/cpu', json={'current_cpu_percent': 0.0, 'prediction_hours': 3})
        assert response.status_code == 200
        data = response.json()['data']
        assert data['current_value'] == data['predicted_data'][0]['predicted_value'] == 0.0
        for item, key in [(point, 'timestamp') for point in data['predicted_data']] + [(item, 'trigger_time') for item in data['scaling_recommendations']]:
            timestamp = datetime.fromisoformat(item[key].replace('Z', '+00:00'))
            assert timestamp.utcoffset() == timedelta(0)
        assert data['predicted_data'][0]['timestamp'] == data['scaling_recommendations'][0]['trigger_time']


@pytest.mark.parametrize(
    'enhanced,legacy_key',
    [
        (False, 'prediction:cpu:495954812e10e3f3'),
        (True, 'prediction:cpu:111e50bfa877f452'),
    ],
)
async def test_legacy_naive_cache_isolated_and_new_utc_cache_hits(monkeypatch, enhanced, legacy_key):
    """旧版本真实参数对应的持久化键不能作为新时间契约响应返回"""
    from copy import deepcopy

    from app.services.prediction_service import PredictionService

    freeze_clock(monkeypatch)
    storage = {legacy_key: {'legacy_naive': True, 'predicted_data': [{'timestamp': '2026-10-23T09:00:00', 'predicted_value': 0.0}]}}
    writes = []

    def save(question, response_data, ttl):
        writes.append((question, ttl))
        storage[question] = deepcopy(response_data)

    predictor, _, _ = await make_predictor('rule')
    service = PredictionService()
    service._initialized = True
    service._cache_manager = SimpleNamespace(get=lambda key: deepcopy(storage.get(key)), set=save)
    service._predictor = predictor
    service._scaling_advisor = ScalingAdvisor()
    service._anomaly_detector = SimpleNamespace(detect_anomalies=AsyncMock(return_value=[]))
    service._fetch_historical_data = AsyncMock(return_value=[])

    async def local_enhanced(**kwargs):
        points = await predictor.predict(kwargs['prediction_type'], kwargs['current_value'], kwargs['historical_data'], kwargs['prediction_hours'], kwargs['granularity'])
        return {'prediction_type': 'cpu', 'current_value': 0.0, 'predicted_data': [point.model_dump() for point in points]}

    service._intelligent_predictor = SimpleNamespace(predict_with_ai_analysis=AsyncMock(side_effect=local_enhanced))

    async def call():
        if enhanced:
            return await service.predict_with_ai_analysis('cpu', 0.0, prediction_hours=3, enable_ai_insights=False)
        return await service.predict_cpu_utilization(0.0, prediction_hours=3)

    with system_timezone('Asia/Shanghai'):
        first = await call()
        assert first.get('legacy_naive') is not True
        assert writes and writes[0][0] != legacy_key
        assert writes[0][1] == (7200 if enhanced else 3600)
        first_point = first['predicted_data'][0]['timestamp']
        first_trigger = first['scaling_recommendations'][0]['trigger_time']
        assert first_point == first_trigger and first_point.utcoffset() == timedelta(0)
        assert first['predicted_data'][0]['predicted_value'] == 0.0
        second = await call()
        assert second['from_cache'] is True
        assert len(writes) == 1
        assert second['predicted_data'][0]['timestamp'] == first_point
        assert storage[legacy_key]['predicted_data'][0]['timestamp'] == '2026-10-23T09:00:00'
        service._fetch_historical_data.assert_awaited_once()


@pytest.mark.parametrize(
    'values,expected',
    [
        ([], 'stable'),
        ([0.0], 'stable'),
        ([10.0], 'stable'),
        ([0.0, 0.0], 'stable'),
        ([0.0, 0.0, 1.0], 'increasing'),
        ([0.0, 0.0, -1.0], 'decreasing'),
        ([10.0, 20.0], 'increasing'),
        ([20.0, 10.0], 'decreasing'),
    ],
)
def test_real_zero_and_single_point_trend_never_divide_by_zero(values, expected):
    from app.models import PredictionDataPoint
    from app.services.prediction_service import PredictionService

    points = [PredictionDataPoint(timestamp=FIXED_UTC + timedelta(hours=index), predicted_value=value) for index, value in enumerate(values)]
    assert PredictionService()._detect_trend(points) == expected
