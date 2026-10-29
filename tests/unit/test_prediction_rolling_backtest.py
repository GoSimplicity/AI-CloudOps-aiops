from datetime import datetime, timedelta, timezone
import math

import pytest

from app.core.prediction.backtest import ForecastObservation, rolling_backtest

START = datetime(2025, 1, 1, tzinfo=timezone.utc)


def row(index, *, forecast=10.0, actual=11.0, observed_delay=0):
    issued = START + timedelta(hours=index)
    target = issued + timedelta(hours=1)
    return ForecastObservation(issued, target, target + timedelta(hours=observed_delay), forecast, actual)


def test_rolling_evaluation_uses_only_observations_available_at_issue_time():
    # The first twenty outcomes arrive after all forecasts were issued. They must
    # not calibrate earlier forecasts even though their targets are in the past.
    delayed = [row(i, observed_delay=100) for i in range(25)]
    result = rolling_backtest(delayed, window=20, min_history=20)
    assert result['sample_count'] == 25
    assert result['mae'] == result['rmse'] == result['bias'] == 1.0
    assert result['calibration']['evaluated_count'] == 0
    assert result['calibration']['observed_coverage'] is None
    assert '未验证' in result['evidence']


def test_rolling_calibration_is_based_on_prior_residuals_and_is_order_independent():
    records = [row(i) for i in range(25)]
    records[-1] = row(24, actual=15.0)
    result = rolling_backtest(list(reversed(records)), window=20, min_history=20)
    assert result['calibration']['evaluated_count'] == 5
    assert result['calibration']['observed_coverage'] == pytest.approx(0.8)
    assert result['horizon_seconds'] == 3600
    assert result['calibration']['residual_lower'] == 1.0
    assert result['calibration']['residual_upper'] == 1.0


def test_zero_actual_has_unknown_wape():
    result = rolling_backtest([row(0, forecast=0.0, actual=0.0)])
    assert result['wape'] is None
    assert result['rmse'] == 0


@pytest.mark.parametrize('invalid', [
    lambda: [row(0), row(0)],
    lambda: [ForecastObservation(START, START + timedelta(hours=1), START, 1, 1)],
    lambda: [ForecastObservation(START.replace(tzinfo=None), START, START, 1, 1)],
    lambda: [row(0, forecast=math.nan)],
    lambda: [row(0, actual=math.inf)],
    lambda: [row(0), ForecastObservation(START, START + timedelta(hours=2), START + timedelta(hours=2), 1, 1)],
])
def test_rejects_invalid_or_incomparable_evidence(invalid):
    with pytest.raises(ValueError):
        rolling_backtest(invalid())


def test_bounds_work_and_detects_overflow():
    with pytest.raises(ValueError):
        rolling_backtest([])
    with pytest.raises(ValueError):
        rolling_backtest([row(i) for i in range(10001)])
    with pytest.raises(ValueError):
        rolling_backtest([row(0, forecast=-1e308, actual=1e308)])
    with pytest.raises(ValueError):
        rolling_backtest([row(0)], window=19)


def test_asgi_backtest_returns_evidence_and_never_reflects_invalid_payload():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes.predict import router

    app = FastAPI()
    app.include_router(router, prefix='/predict')
    client = TestClient(app)
    payload = {'records': [{'issued_at': START.isoformat(),
                            'target_at': (START + timedelta(hours=1)).isoformat(),
                            'observed_at': (START + timedelta(hours=1)).isoformat(),
                            'forecast': 10, 'actual': 11}]}
    response = client.post('/predict/backtest', json=payload)
    assert response.status_code == 200
    assert response.json()['data']['mae'] == 1.0
    assert response.json()['data']['calibration']['observed_coverage'] is None
    payload['records'][0]['forecast'] = 'private-canary'
    response = client.post('/predict/backtest', json=payload)
    assert response.status_code == 400
    assert 'private-canary' not in response.text


@pytest.mark.asyncio
async def test_cancellation_keeps_capacity_until_worker_actually_stops(monkeypatch):
    import asyncio
    import threading

    from fastapi import HTTPException

    from app.api.routes import predict

    started = threading.Event()
    finish = threading.Event()

    def slow(*args, **kwargs):
        started.set()
        assert finish.wait(timeout=5)
        return {'sample_count': 1}

    monkeypatch.setattr(predict, '_backtest_slots', asyncio.Semaphore(1))
    monkeypatch.setattr(predict, 'rolling_backtest', slow)
    payload = {'records': [{'issued_at': START.isoformat(),
                            'target_at': (START + timedelta(hours=1)).isoformat(),
                            'observed_at': (START + timedelta(hours=1)).isoformat(),
                            'forecast': 1, 'actual': 1}]}
    first = asyncio.create_task(predict.backtest_prediction(payload))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(HTTPException) as error:
            await predict.backtest_prediction(payload)
        assert error.value.status_code == 503
    finally:
        finish.set()
    for _ in range(100):
        if predict._backtest_slots._value == 1:
            break
        await asyncio.sleep(0.01)
    assert predict._backtest_slots._value == 1
