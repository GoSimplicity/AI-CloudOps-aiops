import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pandas as pd
import pytest

from app.api.middleware.error_handler import setup_error_handlers
from app.api.routes import predict
from app.common.exceptions import PredictionError
from app.common.identity import principal_scope
from app.core.prediction.intelligent_predictor import IntelligentPredictor
from app.core.prediction.unified_predictor import UnifiedPredictor
from app.models import PredictionGranularity, PredictionType
from app.services.prediction_service import PredictionService


class FakeModel:
    def __init__(self, fail=False):
        self.fail = fail

    def predict(self, features):
        if self.fail:
            raise ValueError("private model failure")
        return [42.0]


def predictor_for(model, metadata=None):
    manager = SimpleNamespace(
        models_loaded=True,
        metadata={PredictionType.CPU: metadata or {}},
        get_model=lambda _: model,
        get_scaler=lambda _: None,
    )
    features = SimpleNamespace(extract_features=AsyncMock(return_value=pd.DataFrame([{}])))
    predictor = UnifiedPredictor(manager, features)
    predictor._initialized = True
    return predictor


@pytest.mark.parametrize(
    "model,metadata,method,reason,accuracy,version",
    [
        (None, {}, "rule_based", "预测模型不可用", None, None),
        (FakeModel(True), {}, "rule_based", "模型预测失败", None, None),
        (FakeModel(), {"model_version": "v3", "backtest": {"accuracy": 0.72, "sample_count": 12}}, "model:", None, 0.72, "v3"),
        (FakeModel(), {"model_version": "v2", "backtest": {"accuracy": 0.9, "sample_count": 0}}, "model:", None, None, "v2"),
        (FakeModel(), {"model_version": "v2", "adapted_for": "cpu", "backtest": {"accuracy": 0.9, "sample_count": 30}}, "model:", "使用其他指标的模型", None, "v2"),
        (FakeModel(), {"metadata_missing": True, "model_version": "1.0"}, "model:", None, None, None),
    ],
)
async def test_prediction_provenance_has_no_invented_confidence(model, metadata, method, reason, accuracy, version):
    batch = await predictor_for(model, metadata).predict(PredictionType.CPU, 10, [], 1, PredictionGranularity.HOUR)
    assert batch.method.startswith(method)
    assert batch.fallback_reason == reason
    assert batch.model_accuracy == accuracy
    assert batch.model_version == version
    assert all(point.confidence_level is None and point.confidence_lower is None and point.confidence_upper is None for point in batch)


def test_asgi_returns_actual_rule_fallback_and_unknown_quality(monkeypatch):
    service = PredictionService()
    service._initialized = True
    service._predictor = predictor_for(None)
    service._scaling_advisor = SimpleNamespace(generate_recommendations=AsyncMock(return_value=[]))
    service._anomaly_detector = SimpleNamespace(detect_anomalies=AsyncMock(return_value=[]))
    service._fetch_historical_data = AsyncMock(return_value=[{"value": 1}, {"value": 2}])
    service._get_from_cache = AsyncMock(return_value=None)
    service._save_to_cache = AsyncMock()
    monkeypatch.setattr(predict, "get_prediction_service", AsyncMock(return_value=service))
    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(predict.router, prefix="/predict")
    response = TestClient(app, raise_server_exceptions=False).post("/predict/cpu", json={"current_cpu_percent": 25, "prediction_hours": 1})
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["method"] == "rule_based"
    assert result["fallback_reason"] == "预测模型不可用"
    assert result["model_accuracy"] is None and result["model_version"] is None
    assert result["predicted_data"][0]["confidence_level"] is None


def test_asgi_ai_analysis_discloses_null_llm_fallback(monkeypatch):
    service = PredictionService()
    service._initialized = True
    engine = IntelligentPredictor(None, None)
    engine._initialized = True
    engine.unified_predictor = predictor_for(None)
    service._intelligent_predictor = engine
    service._scaling_advisor = SimpleNamespace(generate_recommendations=AsyncMock(return_value=[]))
    service._anomaly_detector = SimpleNamespace(detect_anomalies=AsyncMock(return_value=[]))
    service._fetch_historical_data = AsyncMock(
        return_value=[
            {"timestamp": "2026-10-22T10:00:00Z", "value": 1},
            {"timestamp": "2026-10-22T11:00:00Z", "value": 2},
        ]
    )
    service._get_from_cache = AsyncMock(return_value=None)
    service._save_to_cache = AsyncMock()
    monkeypatch.setattr(predict, "get_prediction_service", AsyncMock(return_value=service))
    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(predict.router, prefix="/predict")
    response = TestClient(app, raise_server_exceptions=False).post("/predict/cpu", json={"current_cpu_percent": 25, "prediction_hours": 1, "enable_ai_insights": True})
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["ai_enhanced"] is False
    assert result["ai_fallback_reason"] == "AI分析未完成"
    assert result["fallback_reason"] == "预测模型不可用"
    assert result["method"] == "rule_based"
    assert result["model_accuracy"] is None


@pytest.mark.asyncio
async def test_reports_receive_final_anomalies_scaling_and_cost_before_generation():
    engine = IntelligentPredictor(None, None)
    engine._initialized = True
    engine.unified_predictor = predictor_for(None)
    engine.analyzer = SimpleNamespace(
        analyze_historical_context=AsyncMock(return_value={"status": "success"}),
        interpret_prediction_results=AsyncMock(return_value={"status": "success"}),
        generate_insights=AsyncMock(return_value=[]),
    )
    contexts = []

    async def report(context, *args):
        contexts.append(context)
        return {"status": "success"}

    engine.report_generator = SimpleNamespace(
        generate_comprehensive_report=report,
        generate_executive_summary=report,
        generate_action_plan=report,
        generate_cost_optimization_report=report,
    )
    service = PredictionService()
    service._initialized = True
    service._intelligent_predictor = engine
    service._get_from_cache = AsyncMock(return_value=None)
    service._save_to_cache = AsyncMock()
    service._fetch_historical_data = AsyncMock(return_value=[{"value": 1}, {"value": 2}])
    scaling = SimpleNamespace(model_dump=lambda: {"action": "scale_up", "confidence": None})
    anomaly = SimpleNamespace(model_dump=lambda: {"anomaly_type": "spike"})
    cost = SimpleNamespace(model_dump=lambda: {"predicted_hourly_cost": 7.0})
    service._scaling_advisor = SimpleNamespace(generate_recommendations=AsyncMock(return_value=[scaling]))
    service._anomaly_detector = SimpleNamespace(detect_anomalies=AsyncMock(return_value=[anomaly]))
    service._cost_analyzer = SimpleNamespace(analyze_cost=AsyncMock(return_value=cost))
    result = await service.predict_with_ai_analysis("cpu", 25, prediction_hours=1, resource_constraints={"cpu_cores": 4})
    assert len(contexts) == 4
    for context in contexts:
        assert context.prediction_results["anomaly_predictions"] == result["anomaly_predictions"]
        assert context.scaling_recommendations == result["scaling_recommendations"]
        assert context.cost_analysis == result["cost_analysis"]
    assert result["data_quality_assessment"]["overall_score"] is None


@pytest.mark.asyncio
async def test_multidimension_enriches_before_correlation_and_reports():
    engine = IntelligentPredictor(None, None)
    engine._initialized = True
    engine.unified_predictor = predictor_for(None)
    correlation_seen = []
    reports_seen = []

    async def correlate(results):
        correlation_seen.append(results)
        assert all(item["anomaly_predictions"] and item["scaling_recommendations"] for item in results.values())
        return {"status": "success", "marker": "final-correlation"}

    async def report(context, style):
        reports_seen.append(context)
        assert context.prediction_results["anomaly_predictions"]
        assert context.scaling_recommendations
        assert context.analysis_context["correlation_analysis"]["marker"] == "final-correlation"
        return {"status": "success"}

    engine.analyzer = SimpleNamespace(analyze_multi_dimension_correlation=correlate)
    engine._generate_multi_dimension_insights = AsyncMock(return_value=["fixture insight"])
    engine.report_generator = SimpleNamespace(generate_comprehensive_report=report)
    service = PredictionService()
    service._initialized = True
    service._intelligent_predictor = engine
    service._fetch_historical_data = AsyncMock(return_value=[{"value": 1}, {"value": 2}])
    scaling = SimpleNamespace(model_dump=lambda: {"action": "scale_up", "confidence": None})
    anomaly = SimpleNamespace(model_dump=lambda: {"anomaly_type": "spike"})
    service._scaling_advisor = SimpleNamespace(generate_recommendations=AsyncMock(return_value=[scaling]))
    service._anomaly_detector = SimpleNamespace(detect_anomalies=AsyncMock(return_value=[anomaly]))
    result = await service.predict_multi_dimension_with_correlation(
        [
            {"prediction_type": "cpu", "current_value": 20, "prediction_hours": 1},
            {"prediction_type": "memory", "current_value": 30, "prediction_hours": 1},
        ]
    )
    assert len(correlation_seen) == 1 and len(reports_seen) == 2
    assert set(result["multi_dimension_reports"]) == {"cpu", "memory"}
    assert result["summary_statistics"]["avg_confidence"] is None


@pytest.mark.asyncio
async def test_report_snapshots_are_unique_owned_expiring_and_reject_legacy_ids():
    engine = IntelligentPredictor(None, None)
    engine._initialized = True
    engine.unified_predictor = predictor_for(None)

    async def make_prediction(owner):
        with principal_scope(owner):
            return await engine.predict_with_ai_analysis(
                PredictionType.CPU,
                25,
                [],
                1,
                PredictionGranularity.HOUR,
                enable_ai_insights=False,
            )

    alice, bob = await asyncio.gather(make_prediction("user:1"), make_prediction("user:2"))
    assert alice["analysis_id"] != bob["analysis_id"]
    engine._analysis_cache["old-id"] = {"result_summary": {"prediction_type": "cpu"}}
    service = PredictionService()
    service._initialized = True
    service._intelligent_predictor = engine
    seen = []

    async def report(context, style):
        seen.append(context.prediction_results)
        return {"status": "success", "report": {"full_content": "fixture"}}

    service._report_generator = SimpleNamespace(generate_comprehensive_report=report)
    with principal_scope("user:1"):
        result = await service.generate_prediction_report(alice["analysis_id"])
        assert result["status"] == "success"
        assert seen[0]["predicted_data"] == alice["predicted_data"]
        with pytest.raises(PredictionError):
            await service.generate_prediction_report(bob["analysis_id"])
        with pytest.raises(PredictionError):
            await service.generate_prediction_report("old-id")
        engine._analysis_cache[alice["analysis_id"]]["expires_at"] = 0
        with pytest.raises(PredictionError):
            await service.generate_prediction_report(alice["analysis_id"])
    with principal_scope("user:2"):
        assert (await service.generate_prediction_report(bob["analysis_id"]))["status"] == "success"


def test_prediction_cache_key_is_scoped_by_authenticated_subject():
    service = PredictionService()
    with principal_scope("user:1"):
        alice = service._generate_prediction_cache_key("cpu", 20)
    with principal_scope("user:2"):
        bob = service._generate_prediction_cache_key("cpu", 20)
    assert alice != bob


@pytest.mark.asyncio
async def test_failed_enrichment_is_reported_instead_of_fake_empty_success():
    service = PredictionService()
    service._scaling_advisor = SimpleNamespace(generate_recommendations=AsyncMock(side_effect=RuntimeError("offline")))
    service._anomaly_detector = SimpleNamespace(detect_anomalies=AsyncMock(side_effect=RuntimeError("offline")))
    service._cost_analyzer = SimpleNamespace(analyze_cost=AsyncMock(side_effect=RuntimeError("offline")))
    batch = await predictor_for(None).predict(PredictionType.CPU, 25, [], 1, PredictionGranularity.HOUR)
    result = await service._enhance_ai_prediction_result(
        {"prediction_type": "cpu", "predicted_data": [p.model_dump() for p in batch]},
        {"cpu_cores": 4},
    )
    assert set(result["analysis_failures"]) == {"异常检测失败", "扩缩容分析失败", "成本分析失败"}
    assert result["cost_analysis"] is None


@pytest.mark.parametrize(
    "model,model_fallback,reason",
    [
        (FakeModel(), False, None),
        (None, True, "预测模型不可用"),
    ],
)
@pytest.mark.asyncio
async def test_ai_failure_does_not_overwrite_model_fallback(model, model_fallback, reason, caplog):
    engine = IntelligentPredictor(None, None)
    engine._initialized = True
    engine.unified_predictor = predictor_for(model, {"model_version": "v3"})
    engine.analyzer = SimpleNamespace(analyze_historical_context=AsyncMock(side_effect=RuntimeError("sensitive-ai-error")))
    result = await engine.predict_with_ai_analysis(PredictionType.CPU, 25, [], 1, PredictionGranularity.HOUR)
    assert result["ai_enhanced"] is False
    assert result["ai_fallback_reason"] == "AI分析失败"
    assert result["fallback_mode"] is model_fallback
    assert result["fallback_reason"] == reason
    assert "sensitive-ai-error" not in str(result) and "sensitive-ai-error" not in caplog.text


@pytest.mark.asyncio
async def test_complete_prediction_failure_never_exposes_dependency_exception(caplog):
    engine = IntelligentPredictor(None, None)
    engine._initialized = True
    engine.unified_predictor = SimpleNamespace(predict=AsyncMock(side_effect=RuntimeError("private-model-canary")))
    with pytest.raises(PredictionError) as exc:
        await engine.predict_with_ai_analysis(PredictionType.CPU, 25, [], 1, PredictionGranularity.HOUR, enable_ai_insights=False)
    assert "private-model-canary" not in str(exc.value)
    assert "private-model-canary" not in caplog.text

@pytest.mark.asyncio
async def test_report_dependency_error_does_not_expose_private_details():
    engine = IntelligentPredictor(None, None)
    engine._analysis_cache['private-report'] = {
        'owner': 'user:1', 'expires_at': time.monotonic() + 60,
        'result': {'prediction_type': 'cpu'},
    }
    service = PredictionService()
    service._initialized = True
    service._intelligent_predictor = engine
    service._report_generator = SimpleNamespace(generate_comprehensive_report=AsyncMock(side_effect=RuntimeError('private-report-canary')))
    service.logger = SimpleNamespace(error=Mock())
    with principal_scope('user:1'):
        with pytest.raises(PredictionError) as exc:
            await service.generate_prediction_report('private-report')
    assert 'private-report-canary' not in str(exc.value)
    assert 'private-report-canary' not in str(service.logger.error.call_args)
