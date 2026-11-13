"""GET /predict/models 必须报告真实加载结果。

替身边界：Prometheus.is_healthy 与预测器 is_healthy 是显式替身，只让就绪检查走到模型状态；
不连接 Prometheus、Redis、Kubernetes 或外部 LLM。模型文件是本地 joblib 对象或损坏字节。
"""

import json
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import joblib
import numpy as np

from app.api.middleware.error_handler import setup_error_handlers
from app.api.routes import predict
from app.config.settings import config
from app.core.prediction import model_manager as model_manager_module
from app.core.prediction.model_manager import ModelManager
from app.models import PredictionType
from app.services.prediction_service import PredictionService


class ConstantPredictionModel:
    def predict(self, features):
        return np.asarray([42.0])


def _write_model(directory, prediction_type: PredictionType):
    joblib.dump(ConstantPredictionModel(), directory / f"{prediction_type.value}_prediction_model.pkl")


def _app() -> FastAPI:
    application = FastAPI()
    setup_error_handlers(application)
    application.include_router(predict.router, prefix="/api/v1/predict")
    return application


async def _bind_loaded_service(tmp_path, monkeypatch, prepare):
    monkeypatch.setattr(config.prediction, "model_base_path", str(tmp_path))
    model_manager_module._status_cache.clear()
    prepare(tmp_path)
    manager = ModelManager(base_path=str(tmp_path))
    await manager.load_models()
    service = PredictionService()
    service._initialized = True
    service._model_manager = manager
    predictor = AsyncMock()
    predictor.is_healthy = AsyncMock(return_value=True)
    service._predictor = predictor
    monkeypatch.setattr(predict, "get_prediction_service", AsyncMock(return_value=service))
    monkeypatch.setattr("app.services.prometheus.PrometheusService.is_healthy", lambda self: True)
    return TestClient(_app())


def _models(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    assert "model_path" not in json.dumps(body)
    assert "model_base_path" not in json.dumps(body)
    return data


async def test_models_endpoint_does_not_claim_healthy_when_files_are_missing(tmp_path, monkeypatch):
    client = await _bind_loaded_service(tmp_path, monkeypatch, lambda directory: None)
    data = _models(client.get("/api/v1/predict/models"))
    assert data["status"] == "not_loaded"
    assert data["loaded_models"] == 0
    assert data["models_loaded"] == 0
    assert data["total_models"] == 0
    assert data["models"] == []


async def test_models_endpoint_does_not_claim_healthy_when_files_fail_to_load(tmp_path, monkeypatch):
    def prepare(directory):
        for prediction_type in PredictionType:
            (directory / f"{prediction_type.value}_prediction_model.pkl").write_bytes(b"not-a-model")

    client = await _bind_loaded_service(tmp_path, monkeypatch, prepare)
    data = _models(client.get("/api/v1/predict/models"))
    assert data["status"] == "load_failed"
    assert data["loaded_models"] == 0
    assert data["total_models"] == 0


async def test_models_endpoint_counts_dedicated_models(tmp_path, monkeypatch):
    def prepare(directory):
        for prediction_type in PredictionType:
            _write_model(directory, prediction_type)

    client = await _bind_loaded_service(tmp_path, monkeypatch, prepare)
    data = _models(client.get("/api/v1/predict/models"))
    assert data["status"] == "healthy"
    assert data["total_models"] == 4
    assert data["loaded_models"] == 4
    assert data["models_loaded"] == 4
    assert {item["type"] for item in data["models"]} == {"qps", "cpu", "memory", "disk"}
    assert all(item["status"] == "loaded" and item["fallback"] is False for item in data["models"])


async def test_models_endpoint_marks_borrowed_models_degraded(tmp_path, monkeypatch):
    client = await _bind_loaded_service(tmp_path, monkeypatch, lambda directory: _write_model(directory, PredictionType.QPS))
    data = _models(client.get("/api/v1/predict/models"))
    assert data["status"] == "degraded"
    assert data["loaded_models"] == 4
    assert data["total_models"] == 4
    by_type = {item["type"]: item for item in data["models"]}
    assert by_type["qps"]["status"] == "loaded"
    assert by_type["qps"]["fallback"] is False
    assert by_type["cpu"]["status"] == "adapted"
    assert by_type["cpu"]["fallback"] is True
    assert "model_path" not in by_type["cpu"]


async def test_ready_stays_unavailable_when_no_model_is_loaded(tmp_path, monkeypatch):
    """Prometheus 替身健康时，缺模型仍不能报就绪。503 不带文件路径。"""
    client = await _bind_loaded_service(tmp_path, monkeypatch, lambda directory: None)
    ready = client.get("/api/v1/predict/ready")
    health = client.get("/api/v1/predict/health")
    for response in (ready, health):
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["code"] == 503
        assert body["data"]["error_code"] == "SERVICE_UNAVAILABLE"
        assert body["data"].get("ready") is not True
        assert "model_path" not in json.dumps(body)
