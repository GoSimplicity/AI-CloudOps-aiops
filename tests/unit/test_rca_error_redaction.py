"""RCA 对外错误不携带依赖异常原文。日志只保留异常类型和必要上下文。"""

from datetime import datetime, timedelta, timezone
import logging
from unittest.mock import AsyncMock, Mock

import pytest

from app.api.decorators import AIOpsHTTPException
from app.api.routes import rca
from app.common.exceptions import AIOpsException, RCAError, ValidationError
from app.core.rca.base_collector import BaseDataCollector
from app.models.rca_models import (
    RCAClearNamespaceCacheRequest,
    RCAClearOperationCacheRequest,
    RCAErrorSummaryRequest,
    RCAEventPatternsRequest,
    RCAEventsDataRequest,
    RCALogsDataRequest,
    RCAMetricsDataRequest,
)
from app.services.factory import ServiceFactory
from app.services.rca_service import RCAService

MARKER = "dependency-raw-detail"


def _service() -> RCAService:
    service = RCAService()
    service._initialized = True
    service._cache_manager = None
    service._metrics_collector = AsyncMock()
    service._events_collector = AsyncMock()
    service._logs_collector = AsyncMock()
    return service


def _rendered(caplog, logger_name: str) -> str:
    parts = []
    for record in caplog.records:
        if not record.name.startswith(logger_name):
            continue
        parts.append(record.getMessage())
        if record.exc_info:
            parts.append(logging.Formatter().formatException(record.exc_info))
    return "\n".join(parts)


def _assert_log_has_type_without_original(caplog, logger_name: str, *context: str) -> None:
    matched = [record for record in caplog.records if record.name.startswith(logger_name)]
    assert matched
    assert all(record.exc_info is None for record in matched)
    text = _rendered(caplog, logger_name)
    assert MARKER not in text
    assert "RuntimeError" in text
    for item in context:
        assert item in text


class _BoolBoom:
    def __bool__(self) -> bool:
        raise RuntimeError(MARKER)


class _Unorderable(str):
    def __lt__(self, other: object) -> bool:
        raise RuntimeError(MARKER)


class _PatternCache:
    def clear_by_pattern(self, pattern: str) -> int:
        raise RuntimeError(MARKER)


class _BatchCache:
    def get_keys(self, pattern: str) -> list:
        raise RuntimeError(MARKER)

    def delete_batch(self, keys: list) -> None:
        raise AssertionError("should not delete")


class _ClearAllCache:
    def clear_all(self) -> None:
        raise RuntimeError(MARKER)


class _ExplodingCache:
    def __getattribute__(self, name: str):
        raise RuntimeError(MARKER)


class _RaisingCollector(BaseDataCollector):
    def __init__(self) -> None:
        super().__init__("events")
        self._initialized = True

    async def _do_initialize(self) -> None:
        return None

    async def collect(self, namespace, start_time, end_time, **kwargs):
        raise RuntimeError(MARKER)

    async def health_check(self) -> bool:
        return False


def _bind(monkeypatch, service: RCAService) -> None:
    monkeypatch.setattr(rca, "get_rca_service", AsyncMock(return_value=service))


async def test_event_patterns_and_error_summary_hide_collector_text(caplog):
    service = _service()
    caplog.set_level(logging.DEBUG, logger="aiops.services.rca")
    service._events_collector.get_event_patterns.side_effect = RuntimeError(MARKER)
    service._logs_collector.get_error_summary.side_effect = RuntimeError(MARKER)

    with pytest.raises(RCAError) as patterns:
        await service.get_event_patterns("payments")
    assert patterns.value.error_code == "RCA_ERROR"
    assert patterns.value.message == "根因分析错误: 获取事件模式失败"
    assert patterns.value.details == {}
    assert MARKER not in patterns.value.message

    with pytest.raises(RCAError) as summary:
        await service.get_error_summary("payments")
    assert summary.value.error_code == "RCA_ERROR"
    assert summary.value.message == "根因分析错误: 获取错误摘要失败"
    assert summary.value.details == {}
    assert MARKER not in summary.value.message

    _assert_log_has_type_without_original(caplog, "aiops.services.rca", "payments")


async def test_event_pattern_and_error_summary_routes_keep_generic_detail(monkeypatch):
    service = _service()
    service._events_collector.get_event_patterns.side_effect = RuntimeError(MARKER)
    service._logs_collector.get_error_summary.side_effect = RuntimeError(MARKER)
    _bind(monkeypatch, service)

    with pytest.raises(AIOpsHTTPException) as patterns:
        await rca.get_event_patterns(RCAEventPatternsRequest(namespace="payments"))
    assert patterns.value.status_code == 502
    assert patterns.value.error_code == "RCA_ERROR"
    assert patterns.value.detail == "根因分析错误: 获取事件模式失败"
    assert patterns.value.error_details == {}
    assert MARKER not in str(patterns.value.detail)

    with pytest.raises(AIOpsHTTPException) as summary:
        await rca.get_error_summary(RCAErrorSummaryRequest(namespace="payments"))
    assert summary.value.status_code == 502
    assert summary.value.error_code == "RCA_ERROR"
    assert summary.value.detail == "根因分析错误: 获取错误摘要失败"
    assert summary.value.error_details == {}
    assert MARKER not in str(summary.value.detail)


async def test_config_info_failure_raises_generic_rca_error(caplog):
    service = _service()
    service._cache_manager = _BoolBoom()
    caplog.set_level(logging.DEBUG, logger="aiops.services.rca")

    with pytest.raises(RCAError) as raised:
        await service.get_config_info()
    assert raised.value.error_code == "RCA_ERROR"
    assert raised.value.message == "根因分析错误: 获取配置信息失败"
    assert raised.value.details == {}
    assert MARKER not in raised.value.message
    _assert_log_has_type_without_original(caplog, "aiops.services.rca")


async def test_config_route_hides_unexpected_dependency_text(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="aiops.api.rca")

    async def explode(self):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(ServiceFactory, "_instances", {})
    monkeypatch.setattr(RCAService, "get_config_info", explode)

    with pytest.raises(AIOpsHTTPException) as raised:
        await rca.get_rca_config()
    assert raised.value.status_code == 502
    assert raised.value.error_code == "RCA_ERROR"
    assert raised.value.detail == "根因分析错误: 配置获取失败"
    assert raised.value.error_details == {}
    assert MARKER not in str(raised.value.detail)
    _assert_log_has_type_without_original(caplog, "aiops.api.rca")


async def test_cache_clear_results_omit_dependency_text(caplog):
    service = _service()
    service._cache_manager = _BoolBoom()
    caplog.set_level(logging.DEBUG, logger="aiops.services.rca")

    cleared = await service.clear_all_cache()
    namespace = await service.clear_namespace_cache("payments")
    operation = await service.clear_operation_cache("analyze")

    assert cleared == {"success": False, "message": "清理缓存失败", "cleared_count": 0}
    assert namespace["success"] is False
    assert namespace["message"] == "清理缓存失败"
    assert namespace["namespace"] == "payments"
    assert namespace["cleared_count"] == 0
    assert operation["success"] is False
    assert operation["message"] == "清理缓存失败"
    assert operation["operation"] == "analyze"
    assert operation["cleared_count"] == 0
    assert MARKER not in str((cleared, namespace, operation))
    _assert_log_has_type_without_original(caplog, "aiops.services.rca", "payments", "analyze")


async def test_cache_clear_routes_return_generic_messages(monkeypatch):
    service = _service()
    service._cache_manager = _BoolBoom()
    _bind(monkeypatch, service)

    cleared = await rca.clear_all_cache()
    namespace = await rca.clear_namespace_cache(RCAClearNamespaceCacheRequest(namespace="payments"))
    operation = await rca.clear_operation_cache(RCAClearOperationCacheRequest(operation="analyze"))

    assert cleared["data"]["success"] is False
    assert cleared["data"]["message"] == "清理缓存失败"
    assert cleared["data"]["cleared_count"] == 0
    assert namespace["data"]["namespace"] == "payments"
    assert namespace["data"]["message"] == "清理缓存失败"
    assert operation["data"]["operation"] == "analyze"
    assert operation["data"]["message"] == "清理缓存失败"
    assert MARKER not in str((cleared, namespace, operation))


async def test_cache_routes_raise_generic_rca_error_when_service_raises(monkeypatch, caplog):
    service = _service()
    caplog.set_level(logging.DEBUG, logger="aiops.api.rca")
    _bind(monkeypatch, service)

    service.clear_all_cache = AsyncMock(side_effect=RuntimeError(MARKER))
    with pytest.raises(AIOpsHTTPException) as cleared:
        await rca.clear_all_cache()
    assert cleared.value.error_code == "RCA_ERROR"
    assert cleared.value.detail == "根因分析错误: 清理缓存失败"
    assert cleared.value.error_details == {}

    service.clear_namespace_cache = AsyncMock(side_effect=RuntimeError(MARKER))
    with pytest.raises(AIOpsHTTPException) as namespace:
        await rca.clear_namespace_cache(RCAClearNamespaceCacheRequest(namespace="payments"))
    assert namespace.value.error_code == "RCA_ERROR"
    assert namespace.value.detail == "根因分析错误: 清理命名空间缓存失败"
    assert namespace.value.error_details == {}

    service.clear_operation_cache = AsyncMock(side_effect=RuntimeError(MARKER))
    with pytest.raises(AIOpsHTTPException) as operation:
        await rca.clear_operation_cache(RCAClearOperationCacheRequest(operation="analyze"))
    assert operation.value.error_code == "RCA_ERROR"
    assert operation.value.detail == "根因分析错误: 清理操作缓存失败"
    assert operation.value.error_details == {}
    assert MARKER not in str(cleared.value.detail)
    assert MARKER not in str(namespace.value.detail)
    assert MARKER not in str(operation.value.detail)
    _assert_log_has_type_without_original(caplog, "aiops.api.rca", "payments", "analyze")


async def test_cache_stats_route_hides_dependency_text(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="aiops.api.rca")

    async def explode(self):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(ServiceFactory, "_instances", {})
    monkeypatch.setattr(RCAService, "get_cache_stats", explode)

    with pytest.raises(AIOpsHTTPException) as raised:
        await rca.get_cache_stats()
    assert raised.value.status_code == 502
    assert raised.value.error_code == "RCA_ERROR"
    assert raised.value.detail == "根因分析错误: 获取缓存统计失败"
    assert raised.value.error_details == {}
    assert MARKER not in str(raised.value.detail)
    _assert_log_has_type_without_original(caplog, "aiops.api.rca")


async def test_internal_cache_cleanup_logs_only_the_exception_type(caplog):
    service = _service()
    caplog.set_level(logging.DEBUG, logger="aiops.services.rca")
    service._cache_manager = _PatternCache()
    assert await service._clear_cache_by_pattern("rca_cache:*") == 0

    service._cache_manager = _BatchCache()
    assert await service._clear_cache_by_pattern("rca_cache:analyze:*") == 0

    service._cache_manager = _ClearAllCache()
    assert await service._clear_cache_by_pattern("rca_cache:*") == 0

    service._cache_manager = _ExplodingCache()
    assert await service._clear_cache_by_pattern("rca_cache:*") == 0

    _assert_log_has_type_without_original(caplog, "aiops.services.rca")


async def test_prometheus_metrics_failure_stays_generic(monkeypatch, caplog):
    service = _service()
    caplog.set_level(logging.DEBUG, logger="aiops")
    service._metrics_collector.prometheus.get_available_metrics = AsyncMock(side_effect=RuntimeError(MARKER))
    metrics = await service.get_all_available_metrics()
    assert metrics[0] == "up"
    assert MARKER not in str(metrics)

    service.get_all_available_metrics = AsyncMock(side_effect=RuntimeError(MARKER))
    _bind(monkeypatch, service)
    with pytest.raises(AIOpsHTTPException) as raised:
        await rca.get_all_prometheus_metrics()
    assert raised.value.error_code == "RCA_ERROR"
    assert raised.value.detail == "根因分析错误: 根因分析服务错误"
    assert raised.value.error_details == {}
    assert MARKER not in str(raised.value.detail)
    assert MARKER not in _rendered(caplog, "aiops")
    assert all(record.exc_info is None for record in caplog.records if record.name.startswith("aiops"))


async def test_ready_failure_logs_type_without_stack(monkeypatch, caplog):
    service = AsyncMock()
    service.get_health_status.side_effect = RuntimeError(MARKER)
    _bind(monkeypatch, service)
    caplog.set_level(logging.DEBUG, logger="aiops.api.rca")

    with pytest.raises(AIOpsHTTPException) as raised:
        await rca.rca_ready()
    assert raised.value.status_code == 503
    assert raised.value.error_code == "SERVICE_UNAVAILABLE"
    assert raised.value.detail == "服务 rca 当前不可用"
    assert raised.value.error_details == {}
    assert MARKER not in str(raised.value.detail)
    _assert_log_has_type_without_original(caplog, "aiops.api.rca")


@pytest.mark.parametrize(
    ("endpoint", "payload", "method_name", "operation"),
    [
        (rca.query_metrics_data, RCAMetricsDataRequest(namespace="payments"), "get_metrics", "查询指标数据"),
        (rca.query_events_data, RCAEventsDataRequest(namespace="payments"), "get_events", "查询事件数据"),
        (rca.query_logs_data, RCALogsDataRequest(namespace="payments"), "get_logs", "查询日志数据"),
    ],
)
async def test_data_query_routes_map_dependency_errors_without_original_text(
    monkeypatch, caplog, endpoint, payload, method_name, operation
):
    service = _service()
    setattr(service, method_name, AsyncMock(side_effect=RuntimeError(MARKER)))
    _bind(monkeypatch, service)
    caplog.set_level(logging.DEBUG, logger="aiops.services.rca")

    with pytest.raises(AIOpsHTTPException) as raised:
        await endpoint(payload)
    assert raised.value.status_code == 500
    assert raised.value.error_code == "INTERNAL_ERROR"
    assert raised.value.detail == f"{operation}失败"
    assert raised.value.error_details == {"operation": operation}
    assert MARKER not in str(raised.value.detail)
    assert MARKER not in str(raised.value.error_details)
    _assert_log_has_type_without_original(caplog, "aiops.services.rca", operation)


@pytest.mark.parametrize(
    ("error", "error_type", "code", "operation", "message"),
    [
        (ValueError(MARKER), ValidationError, "VALIDATION_ERROR", "查询指标数据", "字段 parameters 验证失败: 参数无效"),
        (ConnectionError(MARKER), AIOpsException, "SERVICE_UNAVAILABLE", "查询事件数据", "查询事件数据失败"),
        (TimeoutError(MARKER), AIOpsException, "REQUEST_TIMEOUT", "查询日志数据", "查询日志数据失败"),
    ],
)
def test_handle_service_error_keeps_codes_and_drops_original_text(caplog, error, error_type, code, operation, message):
    service = _service()
    caplog.set_level(logging.DEBUG, logger="aiops.services.rca")

    with pytest.raises(error_type) as raised:
        service.handle_service_error(operation, error)
    assert raised.value.error_code == code
    assert raised.value.message == message
    assert MARKER not in raised.value.message
    if code == "VALIDATION_ERROR":
        assert raised.value.details == {}
    else:
        assert raised.value.details == {"operation": operation}
    text = _rendered(caplog, "aiops.services.rca")
    assert MARKER not in text
    assert type(error).__name__ in text
    assert all(record.exc_info is None for record in caplog.records if record.name.startswith("aiops.services.rca"))


def test_handle_service_error_reraises_existing_validation_error(caplog):
    service = _service()
    original = ValidationError("开始时间", "无效的时间格式，请使用ISO格式")
    caplog.set_level(logging.DEBUG, logger="aiops.services.rca")

    with pytest.raises(ValidationError) as raised:
        service.handle_service_error("查询指标数据", original)
    assert raised.value is original
    assert raised.value.error_code == "VALIDATION_ERROR"
    assert "无效的时间格式，请使用ISO格式" in raised.value.message
    assert all(record.exc_info is None for record in caplog.records if record.name.startswith("aiops.services.rca"))


async def test_health_metrics_cache_key_and_background_cache_logs_omit_original_text(caplog):
    service = _service()
    caplog.set_level(logging.DEBUG, logger="aiops.services.rca")

    async def explode_health():
        raise RuntimeError(MARKER)

    service.get_health_status = explode_health
    health = await service.get_service_health_info()
    assert health["service"] == "rca"
    assert health["status"] == "unhealthy"
    assert health["error"] == "获取服务健康信息失败"
    assert "timestamp" in health
    assert MARKER not in str(health)

    key = service._generate_rca_cache_key(
        "analyze", "payments", metrics=[_Unorderable("cpu"), _Unorderable("memory")]
    )
    assert key == "rca:analyze:payments:1"
    assert MARKER not in key

    class _SplitLogger:
        def __init__(self, inner):
            self._inner = inner

        def info(self, *args, **kwargs):
            raise RuntimeError(MARKER)

        def error(self, *args, **kwargs):
            self._inner.error(*args, **kwargs)

        def warning(self, *args, **kwargs):
            self._inner.warning(*args, **kwargs)

    service.logger = _SplitLogger(service.logger)
    await service.cache_analysis_result({"status": "completed"})
    _assert_log_has_type_without_original(caplog, "aiops.services.rca")


async def test_cleanup_logs_component_failures_without_original_text(caplog):
    service = _service()
    caplog.set_level(logging.DEBUG, logger="aiops.services.rca")
    service._cache_manager = Mock()
    service._cache_manager.shutdown.side_effect = RuntimeError(MARKER)
    service._metrics_collector.cleanup = AsyncMock(side_effect=RuntimeError(MARKER))
    service._events_collector.cleanup = AsyncMock(side_effect=RuntimeError(MARKER))
    service._logs_collector.cleanup = AsyncMock(side_effect=RuntimeError(MARKER))
    service._engine = AsyncMock()
    service._engine.cleanup.side_effect = RuntimeError(MARKER)

    await service.cleanup()
    assert service._cache_manager is None
    assert service._metrics_collector is None
    assert service._engine is None
    _assert_log_has_type_without_original(caplog, "aiops.services.rca")


async def test_cleanup_outer_failure_reraises_without_logging_original_text(caplog):
    service = _service()
    caplog.set_level(logging.DEBUG, logger="aiops.services.rca")

    class _Client:
        async def close(self):
            raise RuntimeError(MARKER)

    service._recovery_clients = [_Client()]
    with pytest.raises(RuntimeError) as raised:
        await service.cleanup()
    assert str(raised.value) == MARKER
    text = _rendered(caplog, "aiops.services.rca")
    assert MARKER not in text
    assert "RuntimeError" in text
    assert all(record.exc_info is None for record in caplog.records if record.name.startswith("aiops.services.rca"))


async def test_collector_retry_logs_type_and_context_only(caplog):
    collector = _RaisingCollector()
    caplog.set_level(logging.DEBUG, logger="aiops.rca.collectors.events")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError) as raised:
        await collector.collect_with_retry("payments", start, start + timedelta(minutes=5), max_retries=0)
    assert str(raised.value) == MARKER
    text = _rendered(caplog, "aiops.rca.collectors.events")
    assert MARKER not in text
    assert "RuntimeError" in text
    assert "payments" in text
    assert "events" in text
    assert all(record.exc_info is None for record in caplog.records if record.name.startswith("aiops.rca.collectors"))
