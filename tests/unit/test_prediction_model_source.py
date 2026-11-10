"""预测模型来源：目录可配置，缺失或损坏时告警并走规则兜底。"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import joblib
import numpy as np
import pandas as pd
import pytest

from app.api.middleware.error_handler import setup_error_handlers
from app.api.routes import predict
from app.config.settings import config
from app.core.prediction import model_manager as model_manager_module
from app.core.prediction.model_manager import ModelManager, collect_prediction_model_status
from app.core.prediction.unified_predictor import UnifiedPredictor, prediction_provenance
from app.models import PredictionGranularity, PredictionType
from app.services.prediction_service import PredictionService

EXPECTED_TYPES = ("qps", "cpu", "memory", "disk")
PROVIDE_HINTS = ("PREDICTION_MODEL_BASE_PATH", "挂载", "不会自动训练")


class ConstantPredictionModel:
    """可被 joblib 还原、且不检查特征宽度的本地模型。"""

    def predict(self, features):
        values = np.asarray(features)
        if values.size == 0:
            raise ValueError("empty features")
        return np.asarray([42.0])


def _write_model(directory, prediction_type: PredictionType):
    path = directory / f"{prediction_type.value}_prediction_model.pkl"
    joblib.dump(ConstantPredictionModel(), path)
    return path


def _predictor(manager: ModelManager) -> UnifiedPredictor:
    features = SimpleNamespace(extract_features=AsyncMock(return_value=pd.DataFrame([{}])))
    predictor = UnifiedPredictor(manager, features)
    predictor._initialized = True
    return predictor


def _record(report, prediction_type: str) -> dict:
    found = [item for item in report["models"] if item["prediction_type"] == prediction_type]
    assert len(found) == 1
    return found[0]


async def _predict(manager: ModelManager, prediction_type: PredictionType):
    batch = await _predictor(manager).predict(
        prediction_type, 10.0, [], 1, PredictionGranularity.HOUR
    )
    return prediction_provenance(batch)


@pytest.fixture
def warning_log(caplog):
    caplog.set_level(logging.WARNING, logger="aiops.core.model_manager")
    return caplog


async def test_models_in_configured_directory_are_loaded_and_used(tmp_path, warning_log):
    for prediction_type in PredictionType:
        _write_model(tmp_path, prediction_type)
    configured = {
        prediction_type.value: {
            "model": f"/app/data/models/{prediction_type.value}_prediction_model.pkl",
            "scaler": f"/app/data/models/{prediction_type.value}_prediction_scaler.pkl",
            "metadata": f"/app/data/models/{prediction_type.value}_prediction_model_metadata.json",
        }
        for prediction_type in PredictionType
    }
    manager = ModelManager(base_path=str(tmp_path), configured_paths=configured)

    await manager.load_models()

    report = manager.availability()
    assert report["model_base_path"] == str(tmp_path)
    assert report["rule_fallback"] is False
    assert report["any_fallback"] is False
    for prediction_type in EXPECTED_TYPES:
        record = _record(report, prediction_type)
        assert record["loaded"] is True
        assert record["fallback"] is False
        assert record["status"] == "loaded"
        assert record["fallback_reason"] is None
        assert record["model_path"] == str(tmp_path / f"{prediction_type}_prediction_model.pkl")
    assert "规则兜底" not in warning_log.text
    provenance = await _predict(manager, PredictionType.QPS)
    assert provenance["method"].startswith("model:")
    assert provenance["fallback_mode"] is False
    assert provenance["fallback_reason"] is None


async def test_missing_models_warn_with_path_and_use_rules(tmp_path, warning_log):
    manager = ModelManager(base_path=str(tmp_path))

    await manager.load_models()

    report = manager.availability()
    assert report["model_base_path"] == str(tmp_path)
    assert report["rule_fallback"] is True
    assert report["any_fallback"] is True
    for prediction_type in EXPECTED_TYPES:
        record = _record(report, prediction_type)
        assert record["loaded"] is False
        assert record["fallback"] is True
        assert record["status"] == "missing"
        assert record["fallback_reason"] == "预测模型不可用"
        assert record["model_path"] == str(tmp_path / f"{prediction_type}_prediction_model.pkl")
        assert record["model_path"] in warning_log.text
    assert "规则兜底" in warning_log.text
    for hint in PROVIDE_HINTS:
        assert hint in warning_log.text
    provenance = await _predict(manager, PredictionType.CPU)
    assert provenance["method"] == "rule_based"
    assert provenance["fallback_mode"] is True
    assert provenance["fallback_reason"] == "预测模型不可用"


async def test_corrupt_models_warn_and_use_rules(tmp_path, warning_log):
    for prediction_type in PredictionType:
        path = tmp_path / f"{prediction_type.value}_prediction_model.pkl"
        path.write_bytes(b"this is not a model")
    manager = ModelManager(base_path=str(tmp_path))

    await manager.load_models()

    report = manager.availability()
    assert report["rule_fallback"] is True
    assert report["any_fallback"] is True
    for prediction_type in EXPECTED_TYPES:
        record = _record(report, prediction_type)
        assert record["loaded"] is False
        assert record["fallback"] is True
        assert record["status"] == "load_failed"
        assert record["fallback_reason"] == "预测模型不可用"
        assert record["model_path"] in warning_log.text
    assert "加载失败" in warning_log.text
    assert "规则兜底" in warning_log.text
    for hint in PROVIDE_HINTS:
        assert hint in warning_log.text
    provenance = await _predict(manager, PredictionType.MEMORY)
    assert provenance["method"] == "rule_based"
    assert provenance["fallback_mode"] is True
    assert provenance["fallback_reason"] == "预测模型不可用"


def _app() -> FastAPI:
    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(predict.router, prefix="/api/v1/predict")
    return app


_INFO_MODEL_FIELDS = {
    "prediction_type",
    "loaded",
    "fallback",
    "status",
    "file_status",
    "model_path",
    "fallback_reason",
}
_READY_MODEL_FIELDS = {"prediction_type", "status", "loaded", "fallback"}
_PATH_KEYS = {"model_path", "model_base_path", "file_status", "fallback_reason"}


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def test_info_reports_each_missing_model_without_training(tmp_path, monkeypatch):
    monkeypatch.setattr(config.prediction, "model_base_path", str(tmp_path))
    model_manager_module._status_cache.clear()
    response = TestClient(_app()).get("/api/v1/predict/info")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["service"] == "智能预测服务"
    assert "supported_prediction_types" in data
    report = data["prediction_models"]
    assert set(report) == {"model_base_path", "rule_fallback", "any_fallback", "models"}
    assert report["model_base_path"] == str(tmp_path)
    assert report["rule_fallback"] is True
    assert {item["prediction_type"] for item in report["models"]} == set(EXPECTED_TYPES)
    assert all(set(item) == _INFO_MODEL_FIELDS for item in report["models"])
    assert all(item["loaded"] is False and item["fallback"] is True and item["status"] == "missing" for item in report["models"])
    assert all(item["model_path"] == str(tmp_path / f"{item['prediction_type']}_prediction_model.pkl") for item in report["models"])


def test_ready_keeps_dependency_failure_and_info_still_shows_models(tmp_path, monkeypatch):
    monkeypatch.setattr(config.prediction, "model_base_path", str(tmp_path))
    model_manager_module._status_cache.clear()
    service = PredictionService()
    service._initialized = True
    manager = ModelManager(base_path=str(tmp_path))
    service._model_manager = manager
    monkeypatch.setattr(predict, "get_prediction_service", AsyncMock(return_value=service))
    monkeypatch.setattr("app.services.prometheus.PrometheusService.is_healthy", lambda self: False)
    client = TestClient(_app())
    ready = client.get("/api/v1/predict/ready")
    assert ready.status_code == 503
    assert ready.json()["code"] == 503
    info = client.get("/api/v1/predict/info")
    assert info.status_code == 200
    assert info.json()["data"]["prediction_models"]["rule_fallback"] is True


async def test_ready_reports_loaded_models(tmp_path, monkeypatch):
    for prediction_type in PredictionType:
        _write_model(tmp_path, prediction_type)
    monkeypatch.setattr(config.prediction, "model_base_path", str(tmp_path))
    model_manager_module._status_cache.clear()
    manager = ModelManager(base_path=str(tmp_path))
    await manager.load_models()
    service = PredictionService()
    service._initialized = True
    service._model_manager = manager
    service._predictor = _predictor(manager)
    monkeypatch.setattr(predict, "get_prediction_service", AsyncMock(return_value=service))
    monkeypatch.setattr("app.services.prometheus.PrometheusService.is_healthy", lambda self: True)
    response = TestClient(_app()).get("/api/v1/predict/ready")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ready"] is True
    assert data["service"] == "prediction"
    assert data["status"] == "ready"
    _assert_ready_model_report(data["prediction_models"], rule_fallback=False, any_fallback=False, status="loaded")
    health = TestClient(_app()).get("/api/v1/predict/health")
    assert health.status_code == 200, health.text
    health_data = health.json()["data"]
    _assert_ready_model_report(health_data["prediction_models"], rule_fallback=False, any_fallback=False, status="loaded")
    assert _PATH_KEYS.isdisjoint(_keys(health_data))
    info = TestClient(_app()).get("/api/v1/predict/info")
    assert info.status_code == 200
    info_report = info.json()["data"]["prediction_models"]
    assert info_report["model_base_path"] == str(tmp_path)
    assert all("model_path" in item and str(tmp_path) in item["model_path"] for item in info_report["models"])


def _assert_ready_model_report(report, *, rule_fallback, any_fallback, status):
    assert set(report) == {"rule_fallback", "any_fallback", "models"}
    assert report["rule_fallback"] is rule_fallback
    assert report["any_fallback"] is any_fallback
    assert report["models"]
    assert all(set(item) == _READY_MODEL_FIELDS for item in report["models"])
    assert all(item["status"] == status for item in report["models"])
    assert _PATH_KEYS.isdisjoint(_keys(report))


async def test_missing_dedicated_model_borrows_qps_and_keeps_fallback_reason(tmp_path, warning_log):
    _write_model(tmp_path, PredictionType.QPS)
    manager = ModelManager(base_path=str(tmp_path))

    await manager.load_models()

    report = manager.availability()
    assert _record(report, "qps")["status"] == "loaded"
    assert _record(report, "qps")["fallback_reason"] is None
    borrowed = _record(report, "cpu")
    assert borrowed["loaded"] is True
    assert borrowed["fallback"] is True
    assert borrowed["status"] == "adapted"
    assert borrowed["fallback_reason"] == "使用其他指标的模型"
    assert borrowed["model_path"] in warning_log.text
    assert report["any_fallback"] is True
    assert report["rule_fallback"] is False
    provenance = await _predict(manager, PredictionType.CPU)
    assert provenance["method"].startswith("model:")
    assert provenance["fallback_mode"] is True
    assert provenance["fallback_reason"] == "使用其他指标的模型"


async def test_startup_status_reads_local_files_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config.prediction, "model_base_path", str(tmp_path))
    model_manager_module._status_cache.clear()
    report = await collect_prediction_model_status()
    assert report["rule_fallback"] is True
    assert report["model_base_path"] == str(tmp_path)
    again = await collect_prediction_model_status()
    assert again == report
