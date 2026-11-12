"""预测、缓存、Redis、K8s fixer 和关闭流程不把依赖异常原文带出。"""

from datetime import datetime, timedelta, timezone
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

from fastapi import HTTPException
import pytest

from app.api.decorators import AIOpsHTTPException
from app.api.routes import cache, predict
from app.common.exceptions import PredictionError
from app.config.settings import config
from app.core.agents.k8s_fixer import K8sFixerAgent
from app.core.cache.redis_cache_manager import RedisCacheManager
from app.models.data_models import AgentState
from app.services.base import BaseService
from app.services.factory import ServiceFactory
from app.services.prediction_service import PredictionService
from app.services.rca_service import RCAService
from app.services.startup import StartupService

MARKER = "dependency-raw-detail"
START = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _rendered(caplog, logger_name: str) -> str:
    parts = []
    for record in caplog.records:
        if not record.name.startswith(logger_name):
            continue
        parts.append(record.getMessage())
        if record.exc_info:
            parts.append(logging.Formatter().formatException(record.exc_info))
    return "\n".join(parts)


def _assert_hidden(caplog, logger_name: str, *context: str) -> None:
    matched = [record for record in caplog.records if record.name.startswith(logger_name)]
    assert matched
    assert all(record.exc_info is None for record in matched)
    text = _rendered(caplog, logger_name)
    assert MARKER not in text
    assert "RuntimeError" in text
    for item in context:
        assert item in text


def _prediction() -> PredictionService:
    service = PredictionService()
    service._initialized = True
    service._cache_manager = None
    return service


async def _boom(*args, **kwargs):
    raise RuntimeError(MARKER)


def _redis() -> RedisCacheManager:
    manager = RedisCacheManager.__new__(RedisCacheManager)
    manager.redis_client = MagicMock()
    manager.connection_pool = MagicMock()
    manager._shutdown = False
    manager.cache_prefix = "t:"
    manager.default_ttl = 10
    manager.max_cache_size = 10
    manager.enable_compression = False
    manager.stats_key = "t:stats"
    manager.index_key = "t:index"
    pipe = manager.redis_client.pipeline.return_value
    pipe.__enter__.return_value = pipe
    pipe.__exit__.return_value = False
    return manager


def _allow_repair(monkeypatch) -> None:
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["owned"])


def _agent() -> tuple:
    k8s = AsyncMock()
    llm = AsyncMock()
    agent = K8sFixerAgent(k8s_client=k8s, llm_client=llm)
    agent.retry_delay = 0
    return agent, k8s, llm


class _DemoService(BaseService):
    def __init__(self) -> None:
        super().__init__("demo")

    async def _do_initialize(self) -> None:
        return None

    async def _do_health_check(self) -> bool:
        return True

    async def cleanup(self) -> None:
        raise RuntimeError(MARKER)


class _BoomInstance:
    async def shutdown(self) -> None:
        raise RuntimeError(MARKER)

    def health_check(self) -> bool:
        raise RuntimeError(MARKER)


class _Unpickleable:
    def __reduce__(self):
        raise RuntimeError(MARKER)


class _BoomDict(dict):
    def __bool__(self) -> bool:
        return True

    def items(self):
        raise RuntimeError(MARKER)


class _BoomProvider:
    def lower(self) -> str:
        raise RuntimeError(MARKER)


class _BoomPod:
    def get(self, *args, **kwargs):
        raise RuntimeError(MARKER)


class _BoomAction(str):
    def __contains__(self, item: object) -> bool:
        raise RuntimeError(MARKER)


def _backtest_payload() -> dict:
    issued = START.isoformat()
    target = (START + timedelta(hours=1)).isoformat()
    return {
        "records": [
            {
                "issued_at": issued,
                "target_at": target,
                "observed_at": target,
                "forecast": 1.0,
                "actual": 1.0,
            }
        ]
    }


async def test_prediction_initialization_and_health_hide_dependency_text(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="aiops.services.prediction")

    def redis_boom(**kwargs):
        raise RuntimeError(MARKER)

    class BadManager:
        async def initialize(self):
            raise RuntimeError(MARKER)

    monkeypatch.setattr("app.core.cache.redis_cache_manager.RedisCacheManager", redis_boom)
    monkeypatch.setattr("app.core.prediction.ModelManager", BadManager)
    service = PredictionService()
    with pytest.raises(PredictionError) as raised:
        await service._do_initialize()
    assert raised.value.error_code == "PREDICTION_ERROR"
    assert raised.value.message == "预测服务错误: 服务初始化失败"
    assert raised.value.details == {}
    assert MARKER not in raised.value.message
    assert service._cache_manager is None
    _assert_hidden(caplog, "aiops.services.prediction")

    caplog.clear()
    healthy = _prediction()
    healthy._model_manager = AsyncMock()
    healthy._model_manager.is_healthy.side_effect = RuntimeError(MARKER)
    assert await healthy._do_health_check() is False
    _assert_hidden(caplog, "aiops.services.prediction")


async def test_prediction_ai_init_failure_keeps_basic_mode(monkeypatch, caplog):
    import app.services.prediction_service as module

    caplog.set_level(logging.DEBUG, logger="aiops.services.prediction")

    class Ready:
        async def initialize(self):
            return None

    class Boom:
        def __init__(self, *args, **kwargs):
            pass

        async def initialize(self):
            raise RuntimeError(MARKER)

    monkeypatch.setattr("app.core.cache.redis_cache_manager.RedisCacheManager", lambda **kwargs: object())
    monkeypatch.setattr("app.core.prediction.ModelManager", lambda: Ready())
    monkeypatch.setattr(module, "FeatureExtractor", lambda: object())
    monkeypatch.setattr(module, "UnifiedPredictor", lambda **kwargs: Ready())
    monkeypatch.setattr(module, "AnomalyDetector", lambda: object())
    monkeypatch.setattr(module, "ScalingAdvisor", lambda: object())
    monkeypatch.setattr(module, "CostAnalyzer", lambda: object())
    monkeypatch.setattr("app.services.llm.LLMService", lambda: object())
    monkeypatch.setattr(module, "IntelligentPredictor", Boom)

    service = PredictionService()
    await service._do_initialize()
    assert service.is_initialized() is True
    assert service._intelligent_predictor is None
    assert service._prediction_analyzer is None
    assert service._report_generator is None
    _assert_hidden(caplog, "aiops.services.prediction")


async def test_prediction_failures_use_generic_messages(caplog):
    service = _prediction()
    service._fetch_historical_data = _boom
    caplog.set_level(logging.DEBUG, logger="aiops.services.prediction")
    expected = {
        "predict_qps": ("QPS预测失败", {"current_qps": 10.0}),
        "predict_cpu_utilization": ("CPU预测失败", {"current_cpu_percent": 10.0}),
        "predict_memory_utilization": ("内存预测失败", {"current_memory_percent": 10.0}),
        "predict_disk_utilization": ("磁盘预测失败", {"current_disk_percent": 10.0}),
    }
    for method, (message, kwargs) in expected.items():
        caplog.clear()
        with pytest.raises(PredictionError) as raised:
            await getattr(service, method)(**kwargs)
        assert raised.value.error_code == "PREDICTION_ERROR"
        assert raised.value.message == f"预测服务错误: {message}"
        assert raised.value.details == {}
        assert MARKER not in raised.value.message
        _assert_hidden(caplog, "aiops.services.prediction")

    caplog.clear()
    with pytest.raises(PredictionError) as safe:
        await PredictionService().predict_qps(current_qps=10.0)
    assert safe.value.message == "预测服务错误: 预测服务未初始化，请先调用 initialize()"
    assert MARKER not in safe.value.message

    async def dirty(*args, **kwargs):
        raise PredictionError(MARKER)

    service._fetch_historical_data = dirty
    caplog.clear()
    with pytest.raises(PredictionError) as wrapped:
        await service.predict_qps(current_qps=10.0)
    assert wrapped.value.message == "预测服务错误: QPS预测失败"
    assert MARKER not in wrapped.value.message
    wrapped_log = _rendered(caplog, "aiops.services.prediction")
    assert MARKER not in wrapped_log
    assert "PredictionError" in wrapped_log
    assert all(record.exc_info is None for record in caplog.records if record.name.startswith("aiops.services.prediction"))


async def test_multi_dimension_and_enhancement_hide_dependency_text(caplog):
    service = _prediction()
    caplog.set_level(logging.DEBUG, logger="aiops.services.prediction")
    with pytest.raises(PredictionError) as missing:
        await service.predict_multi_dimension_with_correlation(
            [{"prediction_type": "qps", "current_value": 10.0}]
        )
    assert missing.value.message == "预测服务错误: AI增强组件不可用，无法执行多维度关联分析"
    assert MARKER not in missing.value.message

    service._intelligent_predictor = object()
    service._fetch_historical_data = _boom
    caplog.clear()
    with pytest.raises(PredictionError) as failed:
        await service.predict_multi_dimension_with_correlation(
            [{"prediction_type": "qps", "current_value": 10.0}]
        )
    assert failed.value.error_code == "PREDICTION_ERROR"
    assert failed.value.message == "预测服务错误: 多维度预测失败"
    assert failed.value.details == {}
    assert MARKER not in failed.value.message
    _assert_hidden(caplog, "aiops.services.prediction")

    caplog.clear()
    service._scaling_advisor = AsyncMock()
    service._scaling_advisor.generate_recommendations.side_effect = RuntimeError(MARKER)
    service._cost_analyzer = AsyncMock()
    service._cost_analyzer.analyze_cost.side_effect = RuntimeError(MARKER)
    service._anomaly_detector = None
    enhanced = await service._enhance_ai_prediction_result(
        {
            "predicted_data": [{"timestamp": "2026-01-01T00:00:00", "predicted_value": 1.0}],
            "prediction_type": "qps",
        },
        resource_constraints={"cpu_cores": 1},
    )
    assert enhanced["analysis_failures"] == ["异常检测组件不可用", "扩缩容分析失败", "成本分析失败"]
    assert MARKER not in str(enhanced)
    _assert_hidden(caplog, "aiops.services.prediction")

    caplog.clear()
    broken = await service._enhance_ai_prediction_result({"predicted_data": [{"timestamp": "not-a-time"}]})
    assert broken == {"predicted_data": [{"timestamp": "not-a-time"}]}
    assert MARKER not in _rendered(caplog, "aiops.services.prediction")
    assert "ValidationError" in _rendered(caplog, "aiops.services.prediction")


async def test_prediction_health_model_and_cache_fields_hide_dependency_text(caplog):
    service = _prediction()
    caplog.set_level(logging.DEBUG, logger="aiops.services.prediction")
    service._model_manager = AsyncMock()
    service._model_manager.get_models_info.side_effect = RuntimeError(MARKER)
    health = await service.get_service_health_info()
    assert health["service_status"] == "error"
    assert health["model_status"] == "unknown"
    assert health["error"] == "获取健康信息失败"
    assert "timestamp" in health
    assert MARKER not in str(health)

    caplog.clear()
    service._model_manager.get_detailed_info.side_effect = RuntimeError(MARKER)
    models = await service.get_model_info()
    assert models == {"models": [], "status": "error", "error_message": "获取模型信息失败"}
    assert MARKER not in str(models)

    caplog.clear()
    key = service._generate_prediction_cache_key("qps", 1.0, resource_constraints=_BoomDict())
    assert key.startswith("prediction:uncacheable:")
    assert MARKER not in key

    cache_client = Mock()
    cache_client.get.side_effect = RuntimeError(MARKER)
    cache_client.set.side_effect = RuntimeError(MARKER)
    cache_client.shutdown.side_effect = RuntimeError(MARKER)
    service._cache_manager = cache_client
    caplog.clear()
    assert await service._get_from_cache("prediction:qps:1") is None
    await service._save_to_cache("prediction:qps:1", {"status": "ok"})
    await service.cleanup()
    assert service._cache_manager is None
    _assert_hidden(caplog, "aiops.services.prediction")

    class _InfoBoom:
        def __init__(self, inner):
            self._inner = inner

        def warning(self, *args, **kwargs):
            self._inner.warning(*args, **kwargs)

        def error(self, *args, **kwargs):
            self._inner.error(*args, **kwargs)

        def info(self, *args, **kwargs):
            raise RuntimeError(MARKER)

        def debug(self, *args, **kwargs):
            self._inner.debug(*args, **kwargs)

    service.logger = _InfoBoom(service.logger)
    caplog.clear()
    await service.cleanup()
    outer = _rendered(caplog, "aiops.services.prediction")
    assert MARKER not in outer
    assert "清理资源失败" in outer
    assert "RuntimeError" in outer
    assert all(record.exc_info is None for record in caplog.records if record.name.startswith("aiops.services.prediction"))


async def test_predict_routes_hide_validation_and_dependency_text(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="aiops.api.predict")
    with pytest.raises(HTTPException) as invalid:
        await predict.predict_qps({"current_qps": -1})
    assert invalid.value.status_code == 422
    assert invalid.value.detail == "请求参数验证失败"
    assert "-1" not in str(invalid.value.detail)
    assert "input_value" not in str(invalid.value.detail)
    text = _rendered(caplog, "aiops.api.predict")
    assert MARKER not in text and "-1" not in text
    assert "ValidationError" in text
    assert all(record.exc_info is None for record in caplog.records if record.name.startswith("aiops.api.predict"))

    service = AsyncMock()
    service.initialize = AsyncMock()
    service.predict_qps.side_effect = RuntimeError(MARKER)
    service.predict_cpu_utilization.side_effect = RuntimeError(MARKER)
    service.predict_memory_utilization.side_effect = RuntimeError(MARKER)
    service.predict_disk_utilization.side_effect = RuntimeError(MARKER)
    service.get_model_info.side_effect = RuntimeError(MARKER)
    monkeypatch.setattr(predict, "get_prediction_service", AsyncMock(return_value=service))

    cases = [
        (predict.predict_qps, ({"current_qps": 10},), "预测服务错误: 预测服务错误", "QPS预测失败"),
        (predict.predict_cpu, (predict.CpuPredictionRequest(current_cpu_percent=10),), "预测服务错误: 预测服务错误", "CPU预测失败"),
        (predict.predict_memory, (predict.MemoryPredictionRequest(current_memory_percent=10),), "预测服务错误: 预测服务错误", "内存预测失败"),
        (predict.predict_disk, (predict.DiskPredictionRequest(current_disk_percent=10),), "预测服务错误: 预测服务错误", "磁盘预测失败"),
        (predict.model_info, (), "预测服务错误: 获取模型信息失败", "获取模型信息失败"),
    ]
    for endpoint, args, detail, context in cases:
        caplog.clear()
        with pytest.raises(AIOpsHTTPException) as raised:
            await endpoint(*args)
        assert raised.value.status_code == 502
        assert raised.value.error_code == "PREDICTION_ERROR"
        assert raised.value.detail == detail
        assert raised.value.error_details == {}
        assert MARKER not in str(raised.value.detail)
        _assert_hidden(caplog, "aiops.api.predict", context)


async def test_prediction_readiness_does_not_log_original_text(monkeypatch, caplog):
    service = AsyncMock()
    service.initialize.side_effect = RuntimeError(MARKER)
    monkeypatch.setattr(predict, "get_prediction_service", AsyncMock(return_value=service))
    caplog.set_level(logging.DEBUG, logger="aiops.api.predict")
    with pytest.raises(AIOpsHTTPException) as raised:
        await predict.prediction_ready()
    assert raised.value.status_code == 503
    assert raised.value.error_code == "SERVICE_UNAVAILABLE"
    assert raised.value.detail == "服务 prediction 当前不可用"
    assert MARKER not in str(raised.value.detail)
    _assert_hidden(caplog, "aiops.api.predict", "就绪检查失败")


async def test_backtest_keeps_known_messages_and_hides_other_value_errors(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="aiops.api.predict")

    def known(*args, **kwargs):
        raise ValueError("回测记录数量须为 1 至 10000")

    monkeypatch.setattr(predict, "rolling_backtest", known)
    with pytest.raises(HTTPException) as safe:
        await predict.backtest_prediction(_backtest_payload())
    assert safe.value.status_code == 400
    assert safe.value.detail == "回测记录数量须为 1 至 10000"

    def dirty(*args, **kwargs):
        raise ValueError(MARKER)

    monkeypatch.setattr(predict, "rolling_backtest", dirty)
    caplog.clear()
    with pytest.raises(HTTPException) as raised:
        await predict.backtest_prediction(_backtest_payload())
    assert raised.value.status_code == 400
    assert raised.value.detail == "回测请求无效"
    assert MARKER not in str(raised.value.detail)
    backtest_log = _rendered(caplog, "aiops.api.predict")
    assert MARKER not in backtest_log
    assert "ValueError" in backtest_log
    assert "回测失败" in backtest_log
    assert all(record.exc_info is None for record in caplog.records if record.name.startswith("aiops.api.predict"))


async def test_cache_routes_hide_dependency_text(monkeypatch, caplog):
    service = AsyncMock()
    service.get_cache_stats.side_effect = RuntimeError(MARKER)
    service.clear_cache.side_effect = RuntimeError(MARKER)
    service.get_cache_performance.side_effect = RuntimeError(MARKER)
    service.get_cache_config.side_effect = RuntimeError(MARKER)
    monkeypatch.setattr(cache, "get_cache_service", AsyncMock(return_value=service))
    caplog.set_level(logging.DEBUG, logger="aiops.api.cache")
    cases = [
        (cache.get_cache_stats, {}, "配置错误: 获取缓存统计失败", "获取缓存统计失败"),
        (cache.clear_cache, {"service": "prediction", "pattern": None}, "配置错误: 清空缓存失败", "清空缓存失败"),
        (cache.get_cache_performance, {}, "配置错误: 获取缓存性能报告失败", "获取缓存性能报告失败"),
        (cache.get_cache_config, {}, "配置错误: 获取缓存配置失败", "获取缓存配置失败"),
    ]
    for endpoint, kwargs, detail, context in cases:
        caplog.clear()
        with pytest.raises(AIOpsHTTPException) as raised:
            await endpoint(**kwargs)
        assert raised.value.status_code == 500
        assert raised.value.error_code == "CONFIGURATION_ERROR"
        assert raised.value.detail == detail
        assert raised.value.error_details == {}
        assert MARKER not in str(raised.value.detail)
        _assert_hidden(caplog, "aiops.api.cache", context)


def test_redis_manager_hides_dependency_text(caplog, monkeypatch):
    import pickle

    caplog.set_level(logging.DEBUG, logger="app.core.cache.redis_cache_manager")
    manager = _redis()
    manager.redis_client.ping.side_effect = RuntimeError(MARKER)
    with pytest.raises(RuntimeError) as raised:
        manager._initialize()
    assert str(raised.value) == "无法连接到Redis缓存"
    assert MARKER not in str(raised.value)
    _assert_hidden(caplog, "app.core.cache.redis_cache_manager")

    caplog.clear()
    with pytest.raises(RuntimeError) as serialized:
        manager._serialize_data(_Unpickleable())
    assert str(serialized.value) == "数据序列化失败"
    assert MARKER not in str(serialized.value)
    _assert_hidden(caplog, "app.core.cache.redis_cache_manager")

    def fail_loads(data):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(pickle, "loads", fail_loads)
    caplog.clear()
    assert manager._deserialize_data(b"raw:x") is None
    _assert_hidden(caplog, "app.core.cache.redis_cache_manager")


def test_redis_operations_return_generic_failures(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="app.core.cache.redis_cache_manager")
    manager = _redis()
    manager.redis_client.get.side_effect = RuntimeError(MARKER)
    assert manager.get("question") is None
    assert manager.get_embedding_cache("text") is None
    manager.redis_client.setex.side_effect = RuntimeError(MARKER)
    manager.set("question", {"answer": "ok"})
    manager.set_embedding_cache("text", [0.1])
    manager.redis_client.scard.side_effect = RuntimeError(MARKER)
    manager._cleanup_if_needed()
    manager.redis_client.pipeline.side_effect = RuntimeError(MARKER)
    manager._update_stats("hit")
    manager.redis_client.hgetall.side_effect = RuntimeError(MARKER)
    assert manager.get_stats() == {}
    manager.redis_client.ping.side_effect = RuntimeError(MARKER)
    health = manager.health_check()
    assert health == {"status": "unhealthy", "error": "缓存健康检查失败", "redis_connected": False}
    assert MARKER not in str(health)
    manager.redis_client.scan.side_effect = RuntimeError(MARKER)
    cleared = manager.clear_pattern("predict:*")
    assert cleared == {
        "success": False,
        "message": "根据模式清除缓存失败",
        "cleared_count": 0,
        "pattern": "predict:*",
    }
    assert MARKER not in str(cleared)
    manager.redis_client.smembers.side_effect = RuntimeError(MARKER)
    emptied = manager.clear_all()
    assert emptied == {"success": False, "message": "清空缓存失败", "cleared_count": 0}
    manager.connection_pool.disconnect.side_effect = RuntimeError(MARKER)
    manager.shutdown()

    def explode(pattern):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(manager, "clear_pattern", explode)
    embedding = manager.clear_embedding_cache()
    assert embedding == {"success": False, "message": "清除失败", "cleared_count": 0}
    assert MARKER not in str(embedding)
    _assert_hidden(caplog, "app.core.cache.redis_cache_manager", "predict:*")

    caplog.clear()
    fresh = _redis()
    fresh.redis_client.get.return_value = b"cached"
    fresh._deserialize_data = Mock(return_value={
        "timestamp": 10**12,
        "data": {"answer": "ok"},
        "access_count": 0,
        "last_access": 10**12,
        "ttl": 10,
    })
    fresh._serialize_data = Mock(side_effect=RuntimeError(MARKER))
    assert fresh.get("question") == {"answer": "ok"}
    text = _rendered(caplog, "app.core.cache.redis_cache_manager")
    assert MARKER not in text
    assert "更新缓存访问信息失败" in text
    assert "RuntimeError" in text


def _deployment() -> dict:
    return {
        "metadata": {"name": "owned", "namespace": "owned"},
        "spec": {"template": {"spec": {"containers": [{"name": "owned", "image": "busybox"}]}}},
    }


async def test_k8s_fixer_returns_and_logs_hide_dependency_text(monkeypatch, caplog):
    import asyncio

    import app.core.agents.k8s_fixer as fixer

    async def fast_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    _allow_repair(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="aiops.k8s_fixer")
    agent, k8s, llm = _agent()
    real_identify = agent._identify_and_fix_common_issues
    real_execute = agent._execute_fix

    monkeypatch.setattr(config.llm, "provider", _BoomProvider())
    agent._test_model_failover()
    _assert_hidden(caplog, "aiops.k8s_fixer", "模型故障切换测试失败")

    caplog.clear()
    monkeypatch.setattr(config.llm, "provider", "openai")

    def reject(*args, **kwargs):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(fixer.requests, "get", reject)
    agent._test_model_failover()
    text = _rendered(caplog, "aiops.k8s_fixer")
    assert MARKER not in text
    assert "RuntimeError" in text
    assert "OpenAI" in text and "Ollama" in text
    assert all(record.exc_info is None for record in caplog.records if record.name.startswith("aiops.k8s_fixer"))

    caplog.clear()
    k8s.health_check.side_effect = RuntimeError(MARKER)
    assert await agent.analyze_and_fix_deployment("owned", "owned", "offline") == "修复失败"
    _assert_hidden(caplog, "aiops.k8s_fixer", "owned")

    caplog.clear()
    k8s.health_check.side_effect = None
    k8s.health_check.return_value = True
    k8s.get_deployment.return_value = _deployment()
    k8s.get_events.return_value = []
    k8s.get_pods.return_value = []
    agent._identify_and_fix_common_issues = AsyncMock(return_value={"fixed": False, "message": "未修复"})
    llm.analyze_k8s_problem.return_value = {"analysis": "offline"}
    agent._execute_fix = AsyncMock(side_effect=RuntimeError(MARKER))
    assert await agent.analyze_and_fix_deployment("owned", "owned", "offline") == "分析或执行修复失败"
    _assert_hidden(caplog, "aiops.k8s_fixer", "owned")

    caplog.clear()
    real_info = fixer.logger.info

    def info(message, *args, **kwargs):
        if "尝试显式切换到备用模型" in str(message):
            raise RuntimeError(MARKER)
        return real_info(message, *args, **kwargs)

    monkeypatch.setattr(fixer.logger, "info", info)
    llm.analyze_k8s_problem.side_effect = RuntimeError(MARKER)
    assert await agent.analyze_and_fix_deployment("owned", "owned", "offline") == "无法分析问题，建议手动检查"
    analysis_log = _rendered(caplog, "aiops.k8s_fixer")
    assert MARKER not in analysis_log
    assert "分析出错" in analysis_log
    assert "切换到备用模型失败" in analysis_log
    assert "最终分析尝试失败" in analysis_log
    assert "RuntimeError" in analysis_log

    caplog.clear()
    agent._identify_and_fix_common_issues = real_identify
    agent._execute_fix = real_execute
    identified = await agent._identify_and_fix_common_issues(_deployment(), {"pods": [_BoomPod()]})
    assert identified == {"fixed": False, "message": "修复失败"}
    assert MARKER not in str(identified)
    _assert_hidden(caplog, "aiops.k8s_fixer")

    caplog.clear()
    assert await agent._execute_fix("owned", "owned", {"action": _BoomAction("x")}) == "执行修复操作失败"
    _assert_hidden(caplog, "aiops.k8s_fixer", "owned")

    caplog.clear()
    k8s.scale_deployment.side_effect = RuntimeError(MARKER)
    assert await agent._execute_additional_action("owned", "owned", "扩展副本 replicas 2") == "执行额外操作失败"
    _assert_hidden(caplog, "aiops.k8s_fixer", "owned")

    caplog.clear()
    k8s.get_deployment.side_effect = RuntimeError(MARKER)
    assert await agent._verify_fix("owned", "owned") == "验证失败"
    _assert_hidden(caplog, "aiops.k8s_fixer", "owned")


async def test_k8s_fixer_diagnosis_and_state_hide_dependency_text(monkeypatch, caplog):
    import app.core.agents.k8s_fixer as fixer

    monkeypatch.setattr(fixer, "ensure_mutation_allowed", lambda *args, **kwargs: None)
    _allow_repair(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="aiops.k8s_fixer")
    agent, k8s, llm = _agent()
    k8s.health_check.side_effect = RuntimeError(MARKER)
    assert await agent.diagnose_cluster_health("owned") == "诊断集群健康失败"
    _assert_hidden(caplog, "aiops.k8s_fixer", "owned")

    caplog.clear()
    k8s.health_check.side_effect = None
    k8s.health_check.return_value = True

    def fail_list(*args, **kwargs):
        raise RuntimeError(MARKER)

    k8s.core_v1 = SimpleNamespace(list_node=fail_list)
    k8s.apps_v1 = SimpleNamespace(list_namespaced_deployment=fail_list)
    k8s.get_pods.side_effect = RuntimeError(MARKER)
    k8s.get_events.side_effect = RuntimeError(MARKER)
    diagnosis = await agent.diagnose_cluster_health("owned")
    assert "集群健康诊断" in diagnosis
    assert MARKER not in diagnosis
    assert "无法获取节点状态" in diagnosis
    _assert_hidden(caplog, "aiops.k8s_fixer", "owned", "获取节点状态失败", "获取Pod状态失败")

    caplog.clear()
    llm.is_healthy.side_effect = RuntimeError(MARKER)

    class BadContext:
        def __iter__(self):
            raise TypeError(MARKER)

        def keys(self):
            raise TypeError(MARKER)

    state = AgentState(messages=[], current_step="fix", context=BadContext())
    updated = await agent.process_agent_state(state)
    assert updated.context["error"] == "K8sFixer处理失败"
    assert MARKER not in str(updated.context)
    text = _rendered(caplog, "aiops.k8s_fixer")
    assert MARKER not in text
    assert "TypeError" in text
    assert "转换状态上下文失败" in text

    caplog.clear()
    llm.is_healthy.side_effect = None
    llm.is_healthy.return_value = True
    llm.provider = "offline"
    llm.model = "offline"
    agent.analyze_and_fix_deployment = AsyncMock(side_effect=RuntimeError(MARKER))
    state = AgentState(
        messages=[],
        current_step="fix",
        context={"deployment": "owned", "namespace": "owned", "problem": "offline"},
    )
    updated = await agent.process_agent_state(state)
    assert updated.context["result"] == "修复过程发生错误"
    assert updated.context["error"] == "修复失败，请查看结果详情"
    assert MARKER not in str(updated.context)
    _assert_hidden(caplog, "aiops.k8s_fixer", "owned")


async def test_factory_and_startup_cleanup_hide_dependency_text(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="aiops.services")

    class Factory(ServiceFactory):
        _instances = {}
        _locks = {}
        _initialization_tasks = {}
        _closing = False
        _cleanup_lock = __import__("asyncio").Lock()

    Factory._instances["demo"] = _DemoService()
    result = await Factory.cleanup_all()
    assert result["success"] is False
    assert result["results"]["demo"] == {"success": False, "error": "服务清理失败"}
    assert MARKER not in str(result)
    _assert_hidden(caplog, "aiops.services.factory", "demo")

    startup = StartupService()
    startup.register_service(_DemoService())
    startup.register_service_instance("cache", _BoomInstance())
    caplog.clear()
    cleaned = await startup.cleanup_all_services()
    assert cleaned["success"] is False
    assert cleaned["services"]["demo"]["error"] == "服务清理失败"
    assert cleaned["instances"]["cache"]["error"] == "实例清理失败"
    assert cleaned["services"]["demo"]["success"] is False
    assert MARKER not in str(cleaned)
    _assert_hidden(caplog, "aiops.services.startup", "demo", "cache")

    caplog.clear()
    status = await startup.get_services_status()
    assert status["cache"]["healthy"] is False
    assert status["cache"]["error"] == "获取服务状态失败"
    assert MARKER not in str(status)
    _assert_hidden(caplog, "aiops.services.startup", "cache")

    async def assistant_boom():
        raise RuntimeError(MARKER)

    monkeypatch.setattr(
        "app.core.agents.enterprise_assistant.get_enterprise_assistant",
        assistant_boom,
    )
    caplog.clear()
    warmed = await startup._warmup_assistant()
    assert warmed["success"] is False
    assert warmed["error"] == "智能助手初始化失败"
    assert MARKER not in str(warmed)
    _assert_hidden(caplog, "aiops.services.startup")

    real_info = startup.logger.info

    def info(message, *args, **kwargs):
        if "正在初始化" in str(message):
            raise RuntimeError(MARKER)
        return real_info(message, *args, **kwargs)

    startup.logger.info = info
    caplog.clear()
    outer = await startup._warmup_assistant()
    assert outer["error"] == "智能助手预热失败"
    assert MARKER not in str(outer)
    _assert_hidden(caplog, "aiops.services.startup")


async def test_shutdown_logs_and_rca_cleanup_chain_hide_dependency_text(monkeypatch, caplog):
    import asyncio
    import gc
    import signal

    from app import main

    logging.getLogger().addHandler(caplog.handler)
    logging.getLogger().setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="aiops")
    main.app_state.active_requests = 0
    main.app_state.is_shutting_down = False
    real_cleanup = main.cleanup_resources
    try:
        async def factory_failed():
            return {"success": False, "results": {"rca": {"success": False, "error": MARKER}}}

        monkeypatch.setattr(main.ServiceFactory, "cleanup_all", factory_failed)
        monkeypatch.setattr(main, "cleanup_resources", AsyncMock())
        monkeypatch.setattr(main.startup_service, "get_uptime", lambda: 1.0)
        await main._shutdown_application()
        text = _rendered(caplog, "aiops")
        assert MARKER not in text
        assert "rca" in text

        async def factory_raises():
            raise RuntimeError(MARKER)

        monkeypatch.setattr(main.ServiceFactory, "cleanup_all", factory_raises)
        caplog.clear()
        main.app_state.is_shutting_down = False
        await main._shutdown_application()
        _assert_hidden(caplog, "aiops", "ServiceFactory 清理失败")

        async def partial():
            return {
                "success": False,
                "services": {"rca": {"success": False, "error": MARKER}},
                "instances": {"cache": {"success": False, "error": MARKER}},
            }

        monkeypatch.setattr(main.startup_service, "cleanup_all_services", partial)
        monkeypatch.setattr(main, "cleanup_resources", real_cleanup)
        caplog.clear()
        await main.cleanup_resources()
        cleanup_log = _rendered(caplog, "aiops")
        assert MARKER not in cleanup_log
        assert "rca" in cleanup_log and "cache" in cleanup_log

        async def cleanup_raises():
            raise RuntimeError(MARKER)

        monkeypatch.setattr(main.startup_service, "cleanup_all_services", cleanup_raises)
        caplog.clear()
        await main.cleanup_resources()
        _assert_hidden(caplog, "aiops", "资源清理失败")

        def collect_boom():
            raise RuntimeError(MARKER)

        monkeypatch.setattr(gc, "collect", collect_boom)
        caplog.clear()
        await main._cleanup_global_resources()
        _assert_hidden(caplog, "aiops", "全局资源清理时出错")

        calls = []

        def debug(message, *args, **kwargs):
            rendered = message % args if args else message
            calls.append(rendered)
            if "缓存资源清理检查完成" in str(message):
                raise RuntimeError(MARKER)

        monkeypatch.setattr(logging.getLogger("aiops"), "debug", debug)
        await main._cleanup_global_resources()
        assert MARKER not in "\n".join(calls)
        assert any("RuntimeError" in item for item in calls)

        def loop_boom():
            raise RuntimeError(MARKER)

        monkeypatch.setattr(main.asyncio, "get_event_loop", loop_boom)
        caplog.clear()
        main.GracefulShutdown().signal_handler(signal.SIGTERM, None)
        _assert_hidden(caplog, "aiops", "启动优雅关闭流程失败")

        async def no_sleep(*args, **kwargs):
            return None

        monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
        monkeypatch.setattr(main.os, "_exit", lambda code: None)
        monkeypatch.setattr(main, "cleanup_resources", cleanup_raises)
        caplog.clear()
        main.app_state.active_requests = 0
        await main.GracefulShutdown()._handle_graceful_shutdown()
        _assert_hidden(caplog, "aiops", "优雅关闭流程失败")

        service = RCAService()

        class Client:
            async def close(self):
                raise RuntimeError(MARKER)

        service._recovery_clients = [Client()]
        service._cache_manager = None
        service._metrics_collector = None
        service._events_collector = None
        service._logs_collector = None
        service._engine = None
        startup = StartupService()
        startup.register_service(service)
        caplog.clear()
        cleaned = await startup.cleanup_all_services()
        assert cleaned["services"]["rca"]["error"] == "服务清理失败"
        assert cleaned["services"]["rca"]["success"] is False
        assert MARKER not in str(cleaned)
        monkeypatch.setattr(main, "startup_service", startup)
        monkeypatch.setattr(main, "cleanup_resources", real_cleanup)
        await main.cleanup_resources()
        chain = _rendered(caplog, "aiops")
        assert MARKER not in chain
        assert "RuntimeError" in chain
        assert "rca" in chain
        assert all(record.exc_info is None for record in caplog.records if record.name.startswith("aiops"))
    finally:
        main.app_state.active_requests = 0
        main.app_state.is_shutting_down = False
        await asyncio.sleep(0)