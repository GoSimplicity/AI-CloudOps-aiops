"""RCA 服务健康状态、缓存命中和失败降级。收集器全部用替身。"""

from datetime import datetime, timezone
import logging
from unittest.mock import AsyncMock, Mock

import pytest

from app.common.exceptions import RCAError
from app.models.rca_models import CorrelationResult, RootCause, RootCauseAnalysis
from app.services.rca_service import RCAService


def _service():
    service = RCAService()
    service._initialized = True
    service._cache_manager = None
    service._metrics_collector = AsyncMock()
    service._events_collector = AsyncMock()
    service._logs_collector = AsyncMock()
    service._metrics_collector.health_check.return_value = False
    service._events_collector.health_check.return_value = False
    service._logs_collector.health_check.return_value = False
    return service


async def test_health_status_requires_collectors_and_redis():
    service = _service()
    service._metrics_collector.health_check.return_value = True
    service._events_collector.health_check.return_value = True
    service._cache_manager = Mock()
    service._cache_manager.health_check.return_value = {"status": "healthy"}

    healthy = await service.get_health_status()
    assert healthy["status"] == "healthy"
    assert healthy["prometheus_connected"] is True
    assert healthy["kubernetes_connected"] is True
    assert healthy["redis_connected"] is True

    service._cache_manager.health_check.return_value = {"status": "down"}
    service._events_collector.health_check.return_value = False
    service._logs_collector.health_check.return_value = False
    degraded = await service.get_health_status()
    assert degraded["status"] == "degraded"
    assert degraded["prometheus_connected"] is True
    assert degraded["kubernetes_connected"] is False
    assert degraded["redis_connected"] is False

    service._metrics_collector.health_check.return_value = False
    service._logs_collector.health_check.return_value = False
    service._cache_manager.health_check.side_effect = RuntimeError("redis-offline-marker")
    unhealthy = await service.get_health_status()
    assert unhealthy["status"] == "unhealthy"
    assert unhealthy["redis_connected"] is False
    assert "redis-offline-marker" not in str(unhealthy)


async def test_uninitialized_health_and_quick_diagnosis_fail_closed():
    service = RCAService()
    status = await service.get_health_status()
    assert status["status"] == "unhealthy"
    assert status["collectors"] == {"metrics": False, "events": False, "logs": False}

    diagnosis = await service.quick_diagnosis("payments")
    assert diagnosis["namespace"] == "payments"
    assert diagnosis["status"] == "failed"
    assert diagnosis["confidence_score"] == 0.0


async def test_collector_exception_becomes_unhealthy_without_returning_the_message():
    service = _service()
    service._metrics_collector.health_check.side_effect = RuntimeError("collector-offline-marker")
    status = await service.get_health_status()
    assert status["status"] == "unhealthy"
    assert "collector-offline-marker" not in str(status)


def _analysis():
    return RootCauseAnalysis(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        namespace="payments",
        root_causes=[
            RootCause(
                cause_type="saturation",
                description="cpu",
                confidence=0.8,
                affected_components=["api"],
                evidence={"metric": "cpu"},
                recommendations=["scale"],
            )
        ],
        anomalies={"cpu": 1},
        correlations=[
            CorrelationResult(0.7, "causal", ["cpu then errors"], [{"at": "t1"}]),
            {"confidence": 0.2, "correlation_type": "none", "evidence": [], "timeline": []},
        ],
        recommendations=["scale"],
        confidence_score=0.8,
        analysis_metadata={"collection": {"status": "complete"}},
    )


async def test_analyze_returns_completed_report_and_cache_hit_skips_the_engine():
    service = _service()
    service._engine = AsyncMock()
    service._engine.analyze.return_value = _analysis()
    report = await service.analyze_root_cause("payments", time_window_hours=1.0, metrics=["cpu"])
    assert report["status"] == "completed"
    assert report["success"] is True
    assert report["namespace"] == "payments"
    assert report["root_causes"][0]["cause_type"] == "saturation"
    assert report["correlations"][0]["correlation_type"] == "causal"
    assert report["correlations"][1]["correlation_type"] == "none"
    assert report["anomalies"] == {"cpu": 1}

    cached = {"collection": {"status": "complete"}, "namespace": "payments", "root_causes": []}
    service._cache_manager = Mock()
    service._cache_manager.get.return_value = cached
    hit = await service.analyze_root_cause("payments")
    assert hit["cache_hit"] is True
    assert service._engine.analyze.await_count == 1


async def test_engine_failure_is_wrapped_and_quick_diagnosis_stays_structured():
    service = _service()
    service._engine = AsyncMock()
    service._engine.analyze.side_effect = RuntimeError("engine-offline-marker")
    with pytest.raises(RCAError, match="RCA分析失败") as raised:
        await service.analyze_root_cause("payments")
    assert "engine-offline-marker" not in str(raised.value)

    diagnosis = await service.quick_diagnosis("payments")
    assert diagnosis["status"] == "failed"
    assert diagnosis["namespace"] == "payments"
    assert diagnosis["critical_issues"][0]["type"] == "analysis_error"
    assert "engine-offline-marker" not in str(diagnosis)


async def test_metric_collection_failure_returns_empty_and_event_failure_raises():
    service = _service()
    service._metrics_collector.collect.side_effect = RuntimeError("metric-collector-offline")
    metrics = await service.get_metrics("payments")
    assert metrics == {"items": [], "total": 0}
    assert "metric-collector-offline" not in str(metrics)

    service._events_collector.collect.side_effect = RuntimeError("event-collector-offline")
    with pytest.raises(RCAError) as raised:
        await service.get_events("payments")
    assert raised.value.error_code == "RCA_ERROR"


async def test_collector_failures_return_generic_errors_and_log_only_the_type(caplog):
    marker = "collector-secret-token"
    service = _service()
    caplog.set_level(logging.DEBUG, logger="aiops.services.rca")

    service._metrics_collector.collect.side_effect = RuntimeError(marker)
    assert await service.get_metrics("payments") == {"items": [], "total": 0}

    service._events_collector.collect.side_effect = RuntimeError(marker)
    with pytest.raises(RCAError) as events:
        await service.get_events("payments")
    assert events.value.error_code == "RCA_ERROR"
    assert events.value.message == "根因分析错误: 获取事件失败"
    assert events.value.details == {}

    service._logs_collector.collect.side_effect = RuntimeError(marker)
    with pytest.raises(RCAError) as logs:
        await service.get_logs("payments", pod_name="api-0")
    assert logs.value.error_code == "RCA_ERROR"
    assert logs.value.message == "根因分析错误: 获取日志失败"
    assert logs.value.details == {}

    rendered = []
    for record in caplog.records:
        rendered.append(record.getMessage())
        if record.exc_info:
            rendered.append(logging.Formatter().formatException(record.exc_info))
    text = "\n".join(rendered)
    assert marker not in text
    assert "RuntimeError" in text
    assert "payments" in text
