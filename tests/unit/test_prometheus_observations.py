"""只保留实际采样，不以零值或插值伪造历史"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from app.services.prometheus import PrometheusService


async def query(monkeypatch, values, step="1h", extra_series=None):
    series = [{"metric": {"instance": "node-a"}, "values": values}]
    if extra_series:
        series.append(extra_series)
    post = Mock(return_value=Mock(status_code=200, json=Mock(return_value={"status": "success", "data": {"result": series}})))
    monkeypatch.setattr(requests, "get", post)
    start = datetime(2026, 10, 23, tzinfo=timezone.utc)
    result = await PrometheusService().query_range("up", start, start, step=step)
    assert post.call_args.kwargs["params"]["step"] == step
    return result


async def test_hourly_observations_are_not_expanded_to_minutes(monkeypatch):
    result = await query(monkeypatch, [[0, "10"], [3600, "20"]])
    assert len(result) == 2
    assert result["value"].tolist() == [10, 20]
    assert result.index[1] - result.index[0] == pd.Timedelta(hours=1)


@pytest.mark.parametrize("step,seconds", [("15s", 15), ("5m", 300)])
async def test_observed_cadence_is_preserved(monkeypatch, step, seconds):
    result = await query(monkeypatch, [[0, "0"], [seconds, "1"]], step=step)
    assert len(result) == 2
    assert result["value"].tolist() == [0, 1]
    assert result.index[1] - result.index[0] == pd.Timedelta(seconds=seconds)


@pytest.mark.parametrize("invalid", ["bad", "NaN", "+Inf", "-Inf", None])
async def test_invalid_values_remain_missing_and_true_zero_is_preserved(monkeypatch, invalid):
    result = await query(monkeypatch, [[0, "0"], [3600, invalid], [7200, "7"]])
    assert len(result) == 3
    assert result["value"].iloc[0] == 0
    assert pd.isna(result["value"].iloc[1])
    assert result["value"].iloc[2] == 7


async def test_gaps_are_not_filled_or_resampled(monkeypatch):
    result = await query(monkeypatch, [[0, "0"], [7200, "7"]])
    assert len(result) == 2
    assert result.index[1] - result.index[0] == pd.Timedelta(hours=2)


async def test_multiple_series_keep_labels_and_values(monkeypatch):
    result = await query(monkeypatch, [[0, "2"]], extra_series={"metric": {"instance": "node-b"}, "values": [[0, "8"]]})
    assert len(result) == 2
    assert set(result["label_instance"]) == {"node-a", "node-b"}
    assert set(result["value"]) == {2, 8}


def test_rca_adapter_preserves_real_observations_and_serializes_valid_json():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.rca.metrics_collector import MetricsCollector

    frame = pd.DataFrame({"value": [0.0, float("nan"), 7.0, float("inf")], "label_instance": ["node-a"] * 4}, index=pd.date_range("2026-10-23", periods=4, freq="h"))
    app = FastAPI()

    @app.get("/metrics")
    def metrics():
        return MetricsCollector()._process_metric_data_optimized("up", frame)

    response = TestClient(app, raise_server_exceptions=False).get("/metrics")
    assert response.status_code == 200
    values = response.json()[0]["values"]
    assert [point["value"] for point in values] == [0.0, 7.0]
    assert values[0]["timestamp"] == "2026-10-23T00:00:00.000000Z"
    assert values[1]["timestamp"] == "2026-10-23T02:00:00.000000Z"
    assert "NaN" not in response.text and "Infinity" not in response.text


@pytest.mark.parametrize("status,expected_error", [(400, "HistoricalDataError"), (422, "HistoricalDataError"), (500, "ExternalServiceError")])
async def test_query_rejection_is_distinct_from_dependency_failure(monkeypatch, status, expected_error):
    from app.common import exceptions

    response = Mock(status_code=status)
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    monkeypatch.setattr(requests, "get", Mock(return_value=response))
    now = datetime.now(timezone.utc)
    with pytest.raises(getattr(exceptions, expected_error)):
        await PrometheusService().query_range("invalid", now, now)
