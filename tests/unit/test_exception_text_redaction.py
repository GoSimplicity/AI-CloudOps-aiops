"""直接调用预测引擎、健康结果、启动注册、全局错误处理和 MCP 工具时不带出异常原文。"""

from datetime import datetime, timezone
import logging
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.api.middleware.error_handler import setup_error_handlers
from app.common.exceptions import HistoricalDataError, PredictionError, ValidationError
from app.config.settings import config
from app.core.agents.notifier import NotifierAgent
from app.core.agents.supervisor import SupervisorAgent
from app.core.prediction.feature_extractor import FeatureExtractor
from app.core.prediction.intelligent_predictor import IntelligentPredictor
from app.core.prediction.intelligent_report_generator import (
    IntelligentReportGenerator,
    ReportContext,
)
from app.core.prediction.model_manager import ModelManager
from app.core.prediction.prediction_analyzer import PredictionAnalyzer
from app.core.prediction.prompt_templates import PromptTemplate, PromptTemplateManager
from app.core.prediction.unified_predictor import UnifiedPredictor
from app.models import PredictionType
from app.models.data_models import AgentState
from app.services.cache_service import CacheService
from app.services.factory import ServiceFactory
from app.utils.error_handlers import ErrorHandler, ServiceError, retry_on_exception

MARKER = "dependency-raw-detail-91c4"


def _rendered(caplog, logger_name: str) -> str:
    parts = []
    for record in caplog.records:
        if logger_name and not record.name.startswith(logger_name):
            continue
        parts.append(record.getMessage())
        if record.exc_info:
            parts.append(logging.Formatter().formatException(record.exc_info))
    return "\n".join(parts)


def _assert_hidden(caplog, logger_name: str, *context: str, exc_name: str = "RuntimeError") -> None:
    matched = [record for record in caplog.records if record.name.startswith(logger_name)]
    assert matched
    assert all(record.exc_info is None for record in matched)
    text = _rendered(caplog, logger_name)
    assert MARKER not in text
    assert exc_name in text
    for item in context:
        assert item in text


def _report_context() -> ReportContext:
    return ReportContext(
        prediction_type=PredictionType.QPS,
        analysis_context={},
        prediction_results={"prediction_summary": {"trend": "stable"}},
        interpretation={},
        insights=[],
        scaling_recommendations=[],
        cost_analysis={"cost_savings_potential": 1},
        quantitative_metrics={},
    )


class _BoomNotify:
    enabled = True
    feishu_webhook = "https://example.invalid/hook"

    async def send_feishu_message(self, *args, **kwargs):
        raise RuntimeError(MARKER)

    def is_healthy(self) -> bool:
        raise RuntimeError(MARKER)


class _BoomLLM:
    async def generate_response(self, *args, **kwargs):
        raise RuntimeError(MARKER)

    async def is_healthy(self) -> bool:
        return True


class _HealthBoom:
    def is_initialized(self) -> bool:
        return True

    async def health_check(self) -> bool:
        raise RuntimeError(MARKER)


async def test_prediction_engine_initialize_hides_dependency_text(caplog):
    caplog.set_level(logging.DEBUG, logger="aiops.core")
    manager = Mock()
    manager.models_loaded = False
    manager.load_models = AsyncMock(side_effect=RuntimeError(MARKER))
    predictor = UnifiedPredictor(manager, object())

    with pytest.raises(PredictionError) as raised:
        await predictor.initialize()

    assert raised.value.error_code == "PREDICTION_ERROR"
    assert raised.value.message == "预测服务错误: 预测器初始化失败"
    assert raised.value.details == {}
    assert raised.value.__cause__ is None
    assert MARKER not in raised.value.message
    assert predictor._initialized is False
    _assert_hidden(caplog, "aiops.core.predictor")

    caplog.clear()
    engine = IntelligentPredictor(object(), object())
    engine.unified_predictor.initialize = AsyncMock(side_effect=RuntimeError(MARKER))
    with pytest.raises(PredictionError) as wrapped:
        await engine.initialize()
    assert wrapped.value.error_code == "PREDICTION_ERROR"
    assert wrapped.value.message == "预测服务错误: 预测引擎初始化失败"
    assert MARKER not in wrapped.value.message
    assert wrapped.value.__cause__ is None
    _assert_hidden(caplog, "aiops.core.prediction.intelligent")


async def test_prediction_engine_keeps_historical_and_validation_errors():
    manager = Mock()
    manager.models_loaded = False
    historical = HistoricalDataError("历史窗口不足")
    manager.load_models = AsyncMock(side_effect=historical)
    predictor = UnifiedPredictor(manager, object())
    with pytest.raises(HistoricalDataError) as raised:
        await predictor.initialize()
    assert raised.value is historical
    assert raised.value.error_code == "HISTORICAL_DATA_ERROR"
    assert "历史窗口不足" in raised.value.message

    invalid = ValidationError("hours", "必须为正整数")
    manager.load_models = AsyncMock(side_effect=invalid)
    with pytest.raises(ValidationError) as validation:
        await predictor.initialize()
    assert validation.value is invalid
    assert validation.value.error_code == "VALIDATION_ERROR"


async def test_correlation_and_cost_report_hide_dependency_text(caplog):
    caplog.set_level(logging.DEBUG, logger="aiops.core.prediction")
    analyzer = PredictionAnalyzer(_BoomLLM())
    result = await analyzer.analyze_multi_dimension_correlation(
        {
            PredictionType.QPS: {"prediction_summary": {"trend": "stable"}},
            PredictionType.CPU: {"prediction_summary": {"trend": "stable"}},
        }
    )
    assert result["status"] == "error"
    assert result["correlation_analysis"] == "分析失败"
    assert MARKER not in repr(result)
    _assert_hidden(caplog, "aiops.core.prediction.analyzer")

    caplog.clear()
    generator = IntelligentReportGenerator(_BoomLLM())
    report = await generator.generate_cost_optimization_report(_report_context())
    assert report["status"] == "error"
    assert report["message"] == "生成成本优化报告失败"
    assert MARKER not in repr(report)
    _assert_hidden(caplog, "aiops.core.prediction.report_generator")


async def test_feature_model_and_template_logs_hide_dependency_text(caplog, tmp_path):
    caplog.set_level(logging.DEBUG, logger="aiops.core")
    extractor = FeatureExtractor()
    extractor._extract_time_features = Mock(side_effect=RuntimeError(MARKER))
    frame = await extractor.extract_features(
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        1.0,
        [],
        PredictionType.QPS,
    )
    assert MARKER not in repr(frame)
    _assert_hidden(caplog, "aiops.core.feature_extractor")

    caplog.clear()
    manager = ModelManager(base_path=str(tmp_path))
    model = Mock()
    model.predict.side_effect = RuntimeError(MARKER)
    assert manager._validate_model(model, None, PredictionType.QPS) is False
    _assert_hidden(caplog, "aiops.core.model_manager")

    caplog.clear()
    template = PromptTemplate("demo", "值={secret_name}", ["secret_name"], "说明", "analysis")
    with pytest.raises(ValueError) as missing:
        template.format()
    assert str(missing.value) == "模板 demo 缺少必要变量"
    assert "secret_name" not in str(missing.value)

    caplog.clear()
    prompts = PromptTemplateManager()
    broken = tmp_path / "templates.json"
    broken.write_text(MARKER, encoding="utf-8")
    prompts.load_templates_from_file(str(broken))
    _assert_hidden(caplog, "aiops.core.prediction.prompts", exc_name="JSONDecodeError")


async def test_cache_and_factory_health_hide_dependency_text(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="aiops.services")
    service = CacheService()
    service._prediction_service = Mock()
    service._prediction_service.initialize = AsyncMock(side_effect=RuntimeError(MARKER))
    service._rca_service = Mock()
    service._rca_service.initialize = AsyncMock(side_effect=RuntimeError(MARKER))

    health = await service.cache_health_check()
    assert health["services"]["prediction"]["error"] == "缓存健康检查失败"
    assert health["services"]["rca"]["error"] == "缓存健康检查失败"
    assert health["services"]["prediction"]["status"] == "error"
    assert health["overall_status"] == "unhealthy"
    assert MARKER not in repr(health)

    monkeypatch.setattr(ServiceFactory, "_instances", {"demo": _HealthBoom()})
    status = await ServiceFactory.health()
    assert status["overall"] == "unhealthy"
    assert status["services"]["demo"]["healthy"] is False
    assert status["services"]["demo"]["initialized"] is True
    assert status["services"]["demo"]["error"] == "健康检查失败"
    assert MARKER not in repr(status)


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _collected_hides(handler: _Collect, *context: str, exc_name: str = "RuntimeError") -> None:
    assert handler.records
    assert all(record.exc_info is None for record in handler.records)
    text = "\n".join(record.getMessage() for record in handler.records)
    assert MARKER not in text
    assert exc_name in text
    for item in context:
        assert item in text
    handler.records.clear()


async def test_registration_and_startup_logs_hide_dependency_text(monkeypatch):
    import app.api.middleware.auth as auth
    import app.core.agents.enterprise_assistant as assistant
    import app.main as main

    # 应用日志会替换根 handler，挂在 aiops 自己的 logger 上才收得到。
    collected = _Collect()
    logging.getLogger("aiops").addHandler(collected)
    try:
        await _assert_registration_logs(monkeypatch, main, assistant, auth, collected)
    finally:
        logging.getLogger("aiops").removeHandler(collected)


async def _assert_registration_logs(monkeypatch, main, assistant, auth, collected):
    registered = await main._register_service(
        {
            "name": "demo",
            "module": f"no_such_{MARKER}",
            "class": "Missing",
            "required": False,
        },
        logging.getLogger("aiops"),
    )
    assert registered is False
    _collected_hides(collected, "demo", exc_name="ModuleNotFoundError")

    async def boom_assistant():
        raise RuntimeError(MARKER)

    monkeypatch.setattr(assistant, "get_enterprise_assistant", boom_assistant)
    await main._register_additional_instances(logging.getLogger("aiops"))
    _collected_hides(collected)

    monkeypatch.setattr(main, "register_routes", Mock(side_effect=RuntimeError(MARKER)))
    with pytest.raises(RuntimeError) as routes:
        main.create_app()
    assert str(routes.value) == "路由注册失败"
    assert routes.value.__cause__ is None
    assert MARKER not in str(routes.value)
    _collected_hides(collected)

    monkeypatch.setattr(auth, "ensure_service_auth_configuration", Mock(side_effect=RuntimeError(MARKER)))
    monkeypatch.setattr(main.GracefulShutdown, "setup_signal_handlers", lambda self: None)
    with pytest.raises(RuntimeError) as started:
        main.run_application()
    assert str(started.value) == "服务启动失败"
    assert started.value.__cause__ is None
    assert MARKER not in str(started.value)
    _collected_hides(collected)


async def test_notifier_and_supervisor_hide_dependency_text(caplog):
    caplog.set_level(logging.DEBUG, logger="aiops")
    notifier = NotifierAgent(_BoomNotify())
    text = await notifier.send_human_help_request("磁盘打满")
    assert text == "❌ 发送人工帮助请求异常"
    assert MARKER not in text
    health = await notifier.check_notification_health()
    assert health == {"healthy": False, "error": "通知服务健康检查失败"}
    _assert_hidden(caplog, "aiops.notifier")

    caplog.clear()
    supervisor = SupervisorAgent(_BoomLLM())
    state = AgentState(messages=[{"role": "user", "content": "pod 重启"}], current_step="start", context={})
    decision = await supervisor.route_next_action(state)
    assert decision["next"] == "FINISH"
    assert decision["reasoning"] == "决策失败"
    assert MARKER not in repr(decision)
    supervisor.get_workflow_summary = Mock(side_effect=RuntimeError(MARKER))
    updated = await supervisor.process_agent_state(state)
    assert updated.context["error"] == "生成工作流总结失败"
    assert MARKER not in repr(updated.context)
    _assert_hidden(caplog, "aiops.supervisor")


async def test_mcp_tool_results_hide_dependency_text_and_keep_validation(monkeypatch):
    from kubernetes.client.rest import ApiException

    from app.mcp.server.tools import calculator_tool, time_tool
    from app.mcp.server.tools.calculator_tool import CalculatorTool
    from app.mcp.server.tools.k8s_cluster_check_tool import K8sClusterCheckTool
    from app.mcp.server.tools.k8s_scaling_tool import K8sScalingTool
    from app.mcp.server.tools.time_tool import TimeTool

    scaling = K8sScalingTool()
    apps = Mock()
    apps.read_namespaced_deployment.side_effect = ApiException(status=500, reason=MARKER)
    try:
        result = await scaling._scale_deployment(
            apps, {"resource_name": "web", "namespace": "ns", "replicas": 2}
        )
    finally:
        await scaling.cleanup()
    assert result["success"] is False
    assert result["error"] == "伸缩Deployment失败"
    assert result["message"] == "操作失败"
    assert MARKER not in repr(result)

    cluster = K8sClusterCheckTool()
    version = Mock()
    version.get_code.side_effect = RuntimeError(MARKER)
    try:
        info = await cluster._get_cluster_info(version, Mock())
    finally:
        await cluster.cleanup()
    assert info == {"error": "获取集群信息失败"}
    assert MARKER not in repr(info)

    calculator = CalculatorTool()
    monkeypatch.setattr(calculator_tool, "evaluate", Mock(side_effect=RuntimeError(MARKER)))
    with pytest.raises(RuntimeError) as failed:
        await calculator.execute({"expression": "1+1", "precision": 2})
    assert str(failed.value) == "计算失败"
    assert failed.value.__cause__ is None
    assert MARKER not in str(failed.value)

    with pytest.raises(RuntimeError) as precision:
        await CalculatorTool().execute({"expression": "1+1", "precision": 11})
    assert str(precision.value) == "precision must be between 0 and 10"

    clock = TimeTool()

    class _BoomDateTime:
        @staticmethod
        def now(*args, **kwargs):
            raise RuntimeError(MARKER)

    monkeypatch.setattr(time_tool, "datetime", _BoomDateTime)
    with pytest.raises(RuntimeError) as current:
        await clock.execute({})
    assert str(current.value) == "获取时间失败"
    assert MARKER not in str(current.value)


def test_global_handlers_hide_unexpected_text_and_keep_client_detail(monkeypatch, caplog):
    monkeypatch.setattr(config, "debug", True)
    monkeypatch.setattr(config, "env", "test")
    caplog.set_level(logging.DEBUG, logger="aiops.error_handler")
    app = FastAPI()
    setup_error_handlers(app)

    @app.get("/unexpected")
    def unexpected():
        raise RuntimeError(MARKER)

    @app.get("/client")
    def client_error():
        raise HTTPException(400, "参数不合法")

    @app.get("/http500")
    def http500():
        raise HTTPException(500, MARKER)

    client = TestClient(app, raise_server_exceptions=False)
    unexpected_response = client.get("/unexpected")
    assert unexpected_response.status_code == 500
    body = unexpected_response.json()
    assert body["code"] == 500
    assert body["message"] == "内部服务器错误"
    assert body["data"]["detail"] == "Internal server error"
    assert body["data"]["status_code"] == 500
    assert MARKER not in unexpected_response.text

    client_response = client.get("/client")
    assert client_response.status_code == 400
    assert client_response.json()["message"] == "参数不合法"
    assert client_response.json()["data"]["detail"] == "参数不合法"

    server_response = client.get("/http500")
    assert server_response.status_code == 500
    # 非生产且 debug 时，HTTPException.detail 仍按原契约回显；日志不带这段文案。
    assert server_response.json()["code"] == 500
    assert server_response.json()["message"] == MARKER
    assert server_response.json()["data"]["detail"] == MARKER
    assert server_response.json()["data"]["status_code"] == 500
    _assert_hidden(caplog, "aiops.error_handler")


def test_error_handler_helper_returns_generic_text(caplog):
    caplog.set_level(logging.DEBUG, logger="aiops.test.errors")
    handler = ErrorHandler(logging.getLogger("aiops.test.errors"))
    message, details = handler.log_and_return_error(
        RuntimeError(MARKER), "生成响应失败", include_traceback=True
    )
    assert message == "内部服务错误"
    assert details["error_type"] == "RuntimeError"
    assert details["context"] == "生成响应失败"
    assert "traceback" not in details
    assert MARKER not in repr(details)
    _assert_hidden(caplog, "aiops.test.errors", "生成响应失败")

    @retry_on_exception(max_retries=0, delay=0, exceptions=(RuntimeError,))
    def explode():
        raise RuntimeError(MARKER)

    caplog.clear()
    with pytest.raises(RuntimeError) as raised:
        explode()
    assert type(raised.value) is RuntimeError
    assert MARKER not in _rendered(caplog, "")
    assert all(record.exc_info is None for record in caplog.records)

    from app.utils.error_handlers import error_handler as error_decorator

    @error_decorator(logging.getLogger("aiops.test.errors"))
    def wrapped_call():
        raise RuntimeError(MARKER)

    with pytest.raises(ServiceError) as service_error:
        wrapped_call()
    assert service_error.value.error_code == "SERVICE_ERROR"
    assert service_error.value.message == "内部服务错误"
    assert MARKER not in service_error.value.message
