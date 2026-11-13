"""真实历史与模型结构降级。

线性模型只是本地 joblib 可还原的替身，用来核对分支和送进模型的数字。
它不是生产模型。Prometheus 只使用显式替身，不连接真实监控。
"""

from datetime import datetime, timedelta, timezone
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LinearRegression

from app.api.middleware.error_handler import setup_error_handlers
from app.api.routes import predict
from app.common.exceptions import HistoricalDataError, PredictionError
from app.common.identity import INTERNAL_PRINCIPAL
from app.core.prediction.anomaly_detector import AnomalyDetector
from app.core.prediction.feature_extractor import FeatureExtractor
from app.core.prediction.scaling_advisor import ScalingAdvisor
from app.core.prediction.unified_predictor import UnifiedPredictor, prediction_provenance
from app.models import PredictionGranularity, PredictionType
from app.services.prediction_service import PredictionService

T = datetime(2026, 1, 8, 0, 0, tzinfo=timezone.utc)
HISTORY_REASON = "模型所需真实历史不足"
SCHEMA_REASON = "模型输入结构无法确定"
ADAPTED_REASON = "使用其他指标的模型"
UNAVAILABLE_REASON = "预测模型不可用"
PREDICT_FAILED_REASON = "模型预测失败"


class LocalLinearStandIn:
    """离线线性替身。fit 在内存里完成，joblib 可以还原，不访问外部服务。"""

    def __init__(self, width, fail=False):
        self.fail = fail
        self.calls = 0
        self.seen = []
        model = LinearRegression()
        identity = np.eye(width)
        model.fit(identity, identity[:, 0])
        self._model = model

    @property
    def n_features_in_(self):
        return int(self._model.n_features_in_)

    def predict(self, features):
        self.calls += 1
        values = np.asarray(features, dtype=float)
        self.seen.append(values.copy())
        if self.fail:
            raise RuntimeError("stand-in predict failed")
        return self._model.predict(values)


def series(span, value_at_delta):
    """span 是 T 到最早点的小时差。delta=0 为最新有限观测 T。"""
    rows = []
    for delta in range(span, -1, -1):
        moment = T - timedelta(hours=delta)
        rows.append({
            "timestamp": moment.isoformat().replace("+00:00", "Z"),
            "value": value_at_delta(delta),
        })
    return rows


def unique_value(delta):
    return float(1000 + delta)


def manager_for(prediction_type, model, metadata, scaler=None):
    return SimpleNamespace(
        models_loaded=True,
        metadata={prediction_type: metadata},
        get_model=lambda kind: model if kind == prediction_type else None,
        get_scaler=lambda kind: scaler if kind == prediction_type else None,
        get_metadata=lambda kind: metadata if kind == prediction_type else {},
    )


def predictor_for(prediction_type, model, metadata, scaler=None, extractor=None):
    predictor = UnifiedPredictor(
        manager_for(prediction_type, model, metadata, scaler),
        extractor or FeatureExtractor(),
    )
    predictor._initialized = True
    return predictor


async def predict_cpu(model, metadata, history, **kwargs):
    predictor = predictor_for(PredictionType.CPU, model, metadata, kwargs.get("scaler"))
    batch = await predictor.predict(
        PredictionType.CPU,
        kwargs.get("current", 40.0),
        history,
        kwargs.get("hours", 1),
        PredictionGranularity.HOUR,
        consider_pattern=kwargs.get("consider_pattern", True),
    )
    return predictor, batch


def rule_value(current, growth, point):
    local = point.timestamp.astimezone()
    if 9 <= local.hour <= 18 and local.weekday() < 5:
        factor = 1.2
    elif 0 <= local.hour < 6:
        factor = 0.6
    else:
        factor = 1.0
    return float(np.clip(current * (1 + growth) * factor, 0, 100))


def cache_key(contract, prediction_type, current_value, **extra):
    params = {
        "ai": extra.get("ai_enhanced", False),
        "current": round(current_value, 2),
        "data_contract": "observed-hourly-v1",
        "granularity": extra.get("granularity", "hour"),
        "hours": extra.get("prediction_hours", 24),
        "prediction_contract": contract,
        "principal": INTERNAL_PRINCIPAL,
        "query": extra.get("metric_query") or "default",
        "time_contract": "utc-v1",
        "type": prediction_type,
    }
    for name in (
        "consider_historical_pattern",
        "include_anomaly_detection",
        "include_confidence",
        "sensitivity",
        "target_utilization",
    ):
        if name in extra:
            params[name] = extra[name]
    payload = "|".join(f"{key}:{value}" for key, value in sorted(params.items()))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"prediction:{prediction_type}:{digest}"


def test_local_stand_in_round_trips_through_joblib_without_a_remote_model():
    stand_in = LocalLinearStandIn(2)
    blob = io.BytesIO()
    joblib.dump(stand_in, blob)
    blob.seek(0)
    restored = joblib.load(blob)
    output = restored.predict(np.asarray([[3.0, 9.0]]))
    assert restored.calls == 1
    assert output.shape == (1,)
    assert np.isfinite(output).all()


async def test_empty_direct_extract_does_not_scale_the_current_value():
    with pytest.raises(Exception, match=HISTORY_REASON):
        await FeatureExtractor().extract_features(
            T, 80.0, [], PredictionType.QPS
        )


async def test_direct_extract_keeps_real_zero_and_exact_lag_timestamp():
    history = series(24, lambda delta: 0.0 if delta == 1 else unique_value(delta))
    frame = await FeatureExtractor().extract_features(
        T,
        80.0,
        history,
        PredictionType.CPU,
        required_features=["CPU_1h_ago", "CPU_1d_ago", "CPU_change"],
    )
    assert frame.loc[0, "CPU_1h_ago"] == 0.0
    assert frame.loc[0, "CPU_1d_ago"] == unique_value(24)
    assert frame.loc[0, "CPU_change"] == pytest.approx((80.0 - unique_value(0)) / unique_value(0))


async def test_direct_extract_rejects_unknown_feature_name_and_partial_windows():
    extractor = FeatureExtractor()
    with pytest.raises(Exception, match=SCHEMA_REASON):
        await extractor.extract_features(
            T, 10.0, series(5, unique_value), PredictionType.CPU, required_features=["CPU_p99"]
        )
    with pytest.raises(Exception, match=HISTORY_REASON):
        await extractor.extract_features(
            T, 10.0, series(4, unique_value), PredictionType.CPU, required_features=["CPU_avg_6h"]
        )
    with pytest.raises(Exception, match=HISTORY_REASON):
        await extractor.extract_features(
            T, 10.0, series(22, unique_value), PredictionType.DISK, required_features=["DISK_avg_24h"]
        )


async def test_extract_exception_is_not_rewritten_as_constant_features(caplog):
    caplog.set_level("ERROR", logger="aiops.core.feature_extractor")
    extractor = FeatureExtractor()

    def explode(timestamp):
        raise RuntimeError("secret-feature-text")

    extractor._extract_time_features = explode
    with pytest.raises(PredictionError) as caught:
        await extractor.extract_features(T, 10.0, series(1, unique_value), PredictionType.QPS, required_features=["sin_time"])
    assert "secret-feature-text" not in str(caught.value)
    assert "secret-feature-text" not in caplog.text


async def test_six_hour_window_uses_timestamp_T_through_T_minus_5_even_if_rows_are_shuffled():
    history = series(7, unique_value)
    shuffled = list(reversed(history))
    frame = await FeatureExtractor().extract_features(
        T, 12.0, shuffled, PredictionType.CPU, required_features=["CPU_avg_6h", "CPU_std_6h", "CPU_max_6h", "CPU_min_6h"]
    )
    window = [unique_value(delta) for delta in range(5, -1, -1)]
    assert frame.loc[0, "CPU_avg_6h"] == pytest.approx(float(np.mean(window)))
    assert frame.loc[0, "CPU_std_6h"] == pytest.approx(float(np.std(window)))
    assert frame.loc[0, "CPU_max_6h"] == max(window)
    assert frame.loc[0, "CPU_min_6h"] == min(window)


async def test_zero_baseline_ratios_do_not_replace_the_denominator_with_one():
    history = series(23, lambda delta: 0.0)
    frame = await FeatureExtractor().extract_features(
        T,
        5.0,
        history,
        PredictionType.DISK,
        required_features=["DISK_change", "DISK_growth_rate", "DISK_max_24h"],
    )
    assert frame.loc[0, "DISK_change"] == 0.0
    assert frame.loc[0, "DISK_growth_rate"] == 0.0
    assert frame.loc[0, "DISK_max_24h"] == 0.0


@pytest.mark.parametrize(
    "features,span,called",
    [
        (["CPU_avg_6h"], 5, True),
        (["CPU_avg_6h"], 4, False),
        (["CPU_avg_24h"], 23, True),
        (["CPU_avg_24h"], 22, False),
        (["CPU_1d_ago"], 24, True),
        (["CPU_1d_ago"], 23, False),
        (["CPU_1w_ago"], 168, True),
        (["CPU_1w_ago"], 167, False),
    ],
)
async def test_model_runs_only_when_the_declared_window_is_complete(features, span, called):
    model = LocalLinearStandIn(len(features))
    _, batch = await predict_cpu(
        model,
        {"features": features, "model_version": "stand-in"},
        series(span, unique_value),
    )
    provenance = prediction_provenance(batch)
    if called:
        assert model.calls == 1
        assert provenance["method"].startswith("model:")
        assert provenance.get("fallback_code") is None
        assert provenance["fallback_mode"] is False
        column = model.seen[0][0, 0]
        if features == ["CPU_1d_ago"]:
            assert column == unique_value(24)
        elif features == ["CPU_1w_ago"]:
            assert column == unique_value(168)
        elif features == ["CPU_avg_24h"]:
            assert column == pytest.approx(float(np.mean([unique_value(delta) for delta in range(23, -1, -1)])))
        else:
            assert column == pytest.approx(float(np.mean([unique_value(delta) for delta in range(5, -1, -1)])))
    else:
        assert model.calls == 0
        assert provenance["method"] == "rule_based"
        assert provenance["fallback_reason"] == HISTORY_REASON
        assert provenance.get("fallback_code") == "MODEL_HISTORY_INSUFFICIENT"
        assert provenance["model_version"] is None
        assert provenance["model_accuracy"] is None
        assert batch[0].confidence_level is None
        assert batch[0].predicted_value == pytest.approx(rule_value(40.0, 0.01, batch[0]))


async def test_week_feature_reads_T_minus_168_not_the_previous_index_or_the_earliest_point():
    history = series(168, lambda delta: 0.0 if delta == 168 else 4.0)
    model = LocalLinearStandIn(1)
    _, batch = await predict_cpu(model, {"features": ["CPU_1w_ago"], "model_version": "stand-in"}, history)
    assert model.calls == 1
    assert model.seen[0][0, 0] == 0.0
    assert prediction_provenance(batch).get("fallback_code") is None


async def test_later_steps_do_not_reuse_predictions_as_measured_lags():
    history = series(1, lambda delta: 3.0 if delta == 1 else 9.0)
    model = LocalLinearStandIn(2)
    await predict_cpu(
        model,
        {"features": ["CPU", "CPU_1h_ago"], "model_version": "stand-in"},
        history,
        current=9.0,
        hours=2,
    )
    assert model.calls == 2
    assert model.seen[0][0, 1] == 3.0
    assert model.seen[1][0, 1] == 3.0
    assert model.seen[1][0, 0] != 3.0


async def test_metadata_without_week_feature_does_not_require_168_hours():
    model = LocalLinearStandIn(1)
    _, batch = await predict_cpu(
        model,
        {"features": ["CPU_avg_6h"], "model_version": "hours-only"},
        series(5, unique_value),
    )
    assert model.calls == 1
    assert prediction_provenance(batch)["method"].startswith("model:")
    assert prediction_provenance(batch).get("fallback_code") is None


@pytest.mark.parametrize(
    "metadata,width",
    [
        ({}, 1),
        ({"metadata_missing": True, "features": ["CPU", "CPU_1h_ago", "CPU_1d_ago", "CPU_1w_ago"]}, 4),
        ({"features": []}, 1),
        ({"features": ["CPU_p99"]}, 1),
        ({"features": ["CPU", "CPU"]}, 2),
        ({"features": "CPU"}, 1),
    ],
)
async def test_unknown_schema_is_rule_fallback_and_does_not_call_the_model(metadata, width):
    model = LocalLinearStandIn(width)
    _, batch = await predict_cpu(model, metadata, series(168, unique_value))
    provenance = prediction_provenance(batch)
    assert model.calls == 0
    assert provenance["method"] == "rule_based"
    assert provenance["fallback_reason"] == SCHEMA_REASON
    assert provenance.get("fallback_code") == "MODEL_SCHEMA_UNKNOWN"
    assert provenance["fallback_mode"] is True
    assert provenance["model_version"] is None
    assert provenance["model_accuracy"] is None


async def test_dimension_conflict_between_metadata_and_estimator_does_not_guess():
    model = LocalLinearStandIn(4)
    scaler_model = LocalLinearStandIn(1)
    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler().fit(np.ones((6, 3)))
    for chosen, chosen_scaler in ((model, None), (scaler_model, scaler)):
        chosen.calls = 0
        _, batch = await predict_cpu(
            chosen,
            {"features": ["sin_time"], "model_version": "conflict"},
            series(1, unique_value),
            scaler=chosen_scaler,
        )
        provenance = prediction_provenance(batch)
        assert chosen.calls == 0
        assert provenance.get("fallback_code") == "MODEL_SCHEMA_UNKNOWN"
        assert provenance["method"] == "rule_based"


@pytest.mark.parametrize(
    "metadata,history,fail,code,reason,method_prefix",
    [
        ({"metadata_missing": True, "features": ["CPU_1w_ago"], "adapted_for": "cpu", "model_version": "qps"}, 1, False, "MODEL_SCHEMA_UNKNOWN", SCHEMA_REASON, "rule_based"),
        ({"features": ["CPU_1w_ago"], "adapted_for": "cpu", "model_version": "qps"}, 48, False, "MODEL_HISTORY_INSUFFICIENT", HISTORY_REASON, "rule_based"),
        ({"features": ["sin_time"], "adapted_from": "QPS model", "model_version": "qps"}, 1, True, "MODEL_PREDICT_FAILED", PREDICT_FAILED_REASON, "rule_based"),
        ({"features": ["sin_time"], "adapted_for": "cpu", "model_version": "qps"}, 1, False, "ADAPTED_MODEL", ADAPTED_REASON, "model:"),
        ({}, 1, False, "MODEL_UNAVAILABLE", UNAVAILABLE_REASON, "rule_based"),
    ],
)
async def test_fallback_reason_priority_does_not_hide_history_or_schema_behind_borrow(metadata, history, fail, code, reason, method_prefix):
    model = None if code == "MODEL_UNAVAILABLE" else LocalLinearStandIn(1, fail=fail)
    _, batch = await predict_cpu(model, metadata, series(history, unique_value))
    provenance = prediction_provenance(batch)
    assert provenance["fallback_reason"] == reason
    assert provenance.get("fallback_code") == code
    assert provenance["fallback_mode"] is True
    assert provenance["method"] == method_prefix or provenance["method"].startswith(method_prefix)
    if code == "ADAPTED_MODEL":
        assert model.calls == 1
        assert provenance["model_version"] == "qps"
        assert provenance["model_accuracy"] is None
    else:
        assert provenance["method"] == "rule_based"
        assert provenance["model_accuracy"] is None
        assert (model.calls if model is not None else 0) == (1 if code == "MODEL_PREDICT_FAILED" else 0)


async def test_non_finite_model_output_is_rule_fallback_not_model_success():
    class NanModel(LocalLinearStandIn):
        def predict(self, features):
            self.calls += 1
            return np.asarray([float("nan")])

    model = NanModel(1)
    _, batch = await predict_cpu(model, {"features": ["sin_time"], "model_version": "nan"}, series(1, unique_value))
    provenance = prediction_provenance(batch)
    assert model.calls == 1
    assert provenance["method"] == "rule_based"
    assert provenance.get("fallback_code") == "MODEL_PREDICT_FAILED"
    assert np.isfinite(batch[0].predicted_value)


def _service(prediction_type, metadata, model):
    service = PredictionService()
    service._initialized = True
    service._model_manager = manager_for(prediction_type, model, metadata)
    service._predictor = predictor_for(prediction_type, model, metadata)
    service._scaling_advisor = ScalingAdvisor()
    service._anomaly_detector = AnomalyDetector()
    service._get_from_cache = AsyncMock(return_value=None)
    service._save_to_cache = AsyncMock()
    return service


class _PrometheusProbe:
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    async def health_check(self):
        return True

    async def query_range(self, query, start_time, end_time, step="1m"):
        self.calls.append({"query": query, "start": start_time, "end": end_time, "step": step})
        return self.frame


def _patch_prometheus(monkeypatch, probe):
    async def health_check(self):
        return await probe.health_check()

    async def query_range(self, query, start_time, end_time, step="1m"):
        return await probe.query_range(query, start_time, end_time, step)

    monkeypatch.setattr("app.services.prometheus.PrometheusService.health_check", health_check)
    monkeypatch.setattr("app.services.prometheus.PrometheusService.query_range", query_range)


@pytest.mark.parametrize(
    "prediction_type,features,expected_hours",
    [
        (PredictionType.QPS, ["QPS_1w_ago"], 168),
        (PredictionType.CPU, ["CPU_avg_6h"], 5),
        (PredictionType.MEMORY, ["MEMORY_1d_ago"], 24),
        (PredictionType.DISK, ["DISK_avg_24h"], 23),
        (PredictionType.CPU, ["sin_time"], 1),
    ],
)
async def test_history_query_span_matches_declared_features_and_hourly_step(monkeypatch, prediction_type, features, expected_hours):
    model = LocalLinearStandIn(len(features))
    service = _service(prediction_type, {"features": features, "model_version": "stand-in"}, model)
    probe = _PrometheusProbe(frame=None)
    _patch_prometheus(monkeypatch, probe)
    with pytest.raises(HistoricalDataError):
        await _call_basic(service, prediction_type)
    assert len(probe.calls) == 1
    call = probe.calls[0]
    assert call["step"] == "1h"
    assert call["end"] - call["start"] == timedelta(hours=expected_hours)


async def test_unknown_schema_does_not_widen_the_query_to_a_guessed_week(monkeypatch):
    model = LocalLinearStandIn(4)
    metadata = {
        "metadata_missing": True,
        "features": ["CPU", "CPU_1h_ago", "CPU_1d_ago", "CPU_1w_ago"],
    }
    service = _service(PredictionType.CPU, metadata, model)
    probe = _PrometheusProbe(frame=None)
    _patch_prometheus(monkeypatch, probe)
    with pytest.raises(HistoricalDataError):
        await service.predict_cpu_utilization(20.0, prediction_hours=1)
    assert probe.calls[0]["end"] - probe.calls[0]["start"] == timedelta(hours=1)
    assert probe.calls[0]["step"] == "1h"


async def test_ai_and_multi_dimension_queries_use_the_same_feature_span(monkeypatch):
    model = LocalLinearStandIn(1)
    metadata = {"features": ["CPU_1h_ago"], "model_version": "stand-in"}
    service = _service(PredictionType.CPU, metadata, model)
    service._intelligent_predictor = SimpleNamespace()
    probe = _PrometheusProbe(frame=None)
    _patch_prometheus(monkeypatch, probe)
    with pytest.raises(HistoricalDataError):
        await service.predict_with_ai_analysis("cpu", 20.0, prediction_hours=1, enable_ai_insights=False)
    assert probe.calls[-1]["end"] - probe.calls[-1]["start"] == timedelta(hours=1)
    with pytest.raises(HistoricalDataError):
        await service.predict_multi_dimension_with_correlation(
            [{"prediction_type": "cpu", "current_value": 20.0, "prediction_hours": 1}]
        )
    assert probe.calls[-1]["end"] - probe.calls[-1]["start"] == timedelta(hours=1)


def _hourly_frame(span):
    index = pd.date_range(T - timedelta(hours=span), T, freq="1h")
    return pd.DataFrame({"value": [unique_value(span - offset) for offset in range(len(index))]}, index=index)


async def test_short_consecutive_history_returns_rule_prediction_without_calling_model(monkeypatch):
    model = LocalLinearStandIn(1)
    service = _service(
        PredictionType.QPS,
        {"features": ["QPS_1w_ago"], "model_version": "should-not-leak", "backtest": {"accuracy": 0.91, "sample_count": 20}},
        model,
    )
    probe = _PrometheusProbe(_hourly_frame(47))
    _patch_prometheus(monkeypatch, probe)
    monkeypatch.setattr(predict, "get_prediction_service", AsyncMock(return_value=service))
    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(predict.router, prefix="/predict")
    response = TestClient(app).post("/predict/qps", json={"current_qps": 10, "prediction_hours": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    assert probe.calls[0]["end"] - probe.calls[0]["start"] == timedelta(hours=168)
    assert probe.calls[0]["step"] == "1h"
    assert model.calls == 0
    assert data["method"] == "rule_based"
    assert data["fallback_mode"] is True
    assert data["fallback_reason"] == HISTORY_REASON
    assert data.get("fallback_code") == "MODEL_HISTORY_INSUFFICIENT"
    assert data["model_version"] is None
    assert data["model_accuracy"] is None
    point = data["predicted_data"][0]
    assert point["confidence_level"] is None
    assert point["confidence_lower"] is None
    assert point["confidence_upper"] is None
    assert point["predicted_value"] != 10 * 0.85


@pytest.mark.parametrize(
    "payload,status,reason",
    [
        (None, 422, "No historical metrics"),
        ([], 422, "No historical metrics"),
        ([{"timestamp": "2026-01-08T00:00:00Z", "value": 0}], 422, "Insufficient"),
        ([{"timestamp": "2026-01-08T00:00:00Z", "value": 1}, {"timestamp": "2026-01-08T02:00:00Z", "value": 2}], 422, "hourly"),
        ([{"timestamp": "2026-01-08T00:00:00Z"}, {"timestamp": "2026-01-08T01:00:00Z", "value": 2}], 422, "invalid"),
        ([{"timestamp": "2026-01-08T00:00:00Z", "value": float("nan")}, {"timestamp": "2026-01-08T01:00:00Z", "value": 2}], 422, "finite"),
    ],
)
def test_structural_gate_still_rejects_short_gapped_missing_and_non_finite_samples(monkeypatch, payload, status, reason):
    service = PredictionService()
    service._initialized = True
    service._get_from_cache = AsyncMock(return_value=None)
    service._predictor = AsyncMock()
    service._get_prometheus_data = AsyncMock(return_value=payload)
    monkeypatch.setattr(predict, "get_prediction_service", AsyncMock(return_value=service))
    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(predict.router, prefix="/predict")
    response = TestClient(app, raise_server_exceptions=False).post("/predict/qps", json={"current_qps": 10})
    assert response.status_code == status
    assert response.json()["code"] == status
    assert reason in response.json()["message"]
    service._predictor.predict.assert_not_awaited()


def test_multiple_series_remain_a_historical_data_error():
    frame = pd.DataFrame({"value": [1.0, 2.0]}, index=pd.to_datetime(["2026-01-08T00:00:00Z", "2026-01-08T00:00:00Z"]))
    with pytest.raises(HistoricalDataError, match="aggregate"):
        PredictionService()._convert_prometheus_data(frame)


def test_missing_value_column_stays_empty_instead_of_a_zero_series():
    frame = pd.DataFrame({"other": [1.0]}, index=pd.to_datetime(["2026-01-08T00:00:00Z"]))
    assert PredictionService()._convert_prometheus_data(frame) == []


def test_prediction_cache_key_changes_with_the_history_schema_contract():
    service = PredictionService()
    kwargs = {
        "prediction_hours": 2,
        "granularity": "hour",
        "target_utilization": 0.7,
        "sensitivity": 0.8,
        "include_confidence": True,
        "include_anomaly_detection": True,
        "consider_historical_pattern": True,
    }
    current = service._generate_prediction_cache_key("cpu", 20.0, **kwargs)
    assert current == cache_key("history-schema-v3", "cpu", 20.0, **kwargs)
    assert current != cache_key("provenance-v2", "cpu", 20.0, **kwargs)
    assert current == service._generate_prediction_cache_key("cpu", 20.0, **kwargs)


async def _call_basic(service, prediction_type):
    if prediction_type == PredictionType.QPS:
        await service.predict_qps(10.0, prediction_hours=1)
    elif prediction_type == PredictionType.CPU:
        await service.predict_cpu_utilization(20.0, prediction_hours=1)
    elif prediction_type == PredictionType.MEMORY:
        await service.predict_memory_utilization(20.0, prediction_hours=1)
    else:
        await service.predict_disk_utilization(20.0, prediction_hours=1)
