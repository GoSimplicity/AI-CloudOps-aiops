"""#22 残留路径不再把异常原文写入日志、返回字段或对外错误。"""

import asyncio
import importlib
import logging
import sys
import types
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from app.common.exceptions import AssistantError, AutoFixInternalError, FeatureDisabledError, ValidationError
from app.config.settings import config
from app.core.agents.fallback_models import FallbackChatModel, FallbackEmbeddings
from app.models.inspection_models import InspectionRunRequest
from app.services.autofix_service import AutoFixService
from app.services.factory import ServiceFactory
from app.services.inspection_service import InspectionService
from app.services.mcp_service import MCPService
from app.services.prometheus import PrometheusService
from app.services.workflow_approvals import WorkflowApprovals
from app.utils.error_handlers import ExternalServiceError, ServiceError, ValidationError as LLMValidationError

MARKER = "dependency-raw-detail-e758"


def _rendered(caplog, logger_name: str = "") -> str:
    parts = []
    for record in caplog.records:
        if logger_name and not record.name.startswith(logger_name):
            continue
        parts.append(record.getMessage())
        if record.exc_info:
            parts.append(logging.Formatter().formatException(record.exc_info))
    return "\n".join(parts)


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _assert_records(handler: _Collect, *context: str, exc_name: str = "RuntimeError") -> None:
    assert handler.records
    assert all(record.exc_info is None for record in handler.records)
    text = "\n".join(record.getMessage() for record in handler.records)
    assert MARKER not in text
    assert exc_name in text
    for item in context:
        assert item in text
    handler.records.clear()


def _assert_hidden(caplog, logger_name: str, *context: str, exc_name: str = "RuntimeError") -> None:
    matched = [record for record in caplog.records if record.name.startswith(logger_name)]
    assert matched
    assert all(record.exc_info is None for record in matched)
    text = _rendered(caplog, logger_name)
    assert MARKER not in text
    assert exc_name in text
    for item in context:
        assert item in text


class _InitBoom:
    async def initialize(self) -> None:
        raise RuntimeError(MARKER)


def _llm(monkeypatch):
    from app.services.llm import LLMService

    monkeypatch.setattr(LLMService, "_init_providers", lambda self: None)
    service = LLMService()
    service.provider = "openai"
    service.backup_provider = "ollama"
    service.model = "demo"
    service.task_model = "demo-task"
    service.temperature = 0.2
    service.max_tokens = 32
    return service


async def test_sigusr1_status_logs_hide_exception_text(monkeypatch):
    import app.main as main

    collected = _Collect()
    logger = logging.getLogger("aiops")
    logger.addHandler(collected)
    logger.setLevel(logging.DEBUG)
    try:
        handler = main.GracefulShutdown()
        monkeypatch.setattr(main.asyncio, "get_event_loop", Mock(side_effect=RuntimeError(MARKER)))
        handler._status_handler(None, None)
        _assert_records(collected, "无法获取详细服务状态")

        monkeypatch.setattr(main.startup_service, "get_uptime", Mock(side_effect=RuntimeError(MARKER)))
        handler._status_handler(None, None)
        _assert_records(collected, "状态报告失败")

        monkeypatch.setattr(main.startup_service, "get_services_status", AsyncMock(side_effect=RuntimeError(MARKER)))
        await handler._log_services_status()
        _assert_records(collected, "获取服务状态失败")
    finally:
        logger.removeHandler(collected)


async def test_ensure_initialized_log_hides_exception_text(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="aiops.services.factory")
    monkeypatch.setattr(ServiceFactory, "_instances", {"demo": _InitBoom()})

    assert await ServiceFactory.ensure_initialized("missing") is False
    assert await ServiceFactory.ensure_initialized("demo") is False
    _assert_hidden(caplog, "aiops.services.factory", "demo")


async def test_llm_public_error_stays_generic_and_keeps_validation(caplog, monkeypatch):
    import app.utils.error_handlers as error_handlers

    caplog.set_level(logging.DEBUG, logger="aiops.llm")
    monkeypatch.setattr(error_handlers.asyncio, "sleep", AsyncMock())
    service = _llm(monkeypatch)
    service._call_openai_api = AsyncMock(side_effect=RuntimeError(MARKER))
    service._call_ollama_api = AsyncMock(side_effect=RuntimeError(MARKER))
    service._use_fallback_chat_model = AsyncMock(side_effect=RuntimeError(MARKER))

    with pytest.raises(ServiceError) as raised:
        await service.generate_response([{"role": "user", "content": "你好"}])

    assert raised.value.error_code == "SERVICE_ERROR"
    assert raised.value.message == "内部服务错误"
    assert raised.value.details == {"service": "llm_service", "operation": "generate_response"}
    assert MARKER not in raised.value.message
    context = raised.value.__context__
    assert isinstance(context, ExternalServiceError)
    assert context.error_code == "EXTERNAL_SERVICE_ERROR"
    assert context.details["external_service"] == "openai_all_failed"
    assert MARKER not in str(context)
    assert MARKER not in _rendered(caplog, "aiops.llm")
    _assert_hidden(caplog, "aiops.llm", "openai", "ollama")

    caplog.clear()
    with pytest.raises(LLMValidationError) as invalid:
        await service.generate_response([])
    assert invalid.value.error_code == "VALIDATION_ERROR"
    assert str(invalid.value) == "消息列表不能为空"


async def test_llm_exhausted_sentinel_still_skips_backup_without_logging_text(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="aiops.llm")
    service = _llm(monkeypatch)
    service._call_openai_api = AsyncMock(side_effect=RuntimeError(f"upstream all_failed {MARKER}"))
    service._call_ollama_api = AsyncMock(side_effect=RuntimeError("backup-should-not-run"))
    service._use_fallback_chat_model = AsyncMock(side_effect=RuntimeError(MARKER))

    with pytest.raises(ExternalServiceError) as raised:
        await service._execute_generation_with_fallback.__wrapped__(
            service,
            messages=[{"role": "user", "content": "你好"}],
            response_format=None,
            temperature=0.2,
            max_tokens=32,
        )

    assert raised.value.error_code == "EXTERNAL_SERVICE_ERROR"
    assert raised.value.message == "所有LLM服务都彻底失败"
    assert raised.value.details["external_service"] == "openai"
    assert raised.value.__cause__ is None
    assert MARKER not in str(raised.value)
    service._call_ollama_api.assert_not_awaited()
    assert MARKER not in _rendered(caplog, "aiops.llm")
    _assert_hidden(caplog, "aiops.llm", "openai")


async def test_llm_provider_and_health_logs_hide_exception_text(caplog, monkeypatch):
    import ollama

    caplog.set_level(logging.DEBUG, logger="aiops.llm")
    service = _llm(monkeypatch)

    class BoomUrl:
        def replace(self, *_args, **_kwargs):
            raise RuntimeError(MARKER)

    ollama_base_url = config.llm.ollama_base_url
    monkeypatch.setattr(config.llm, "ollama_base_url", BoomUrl())
    service._init_backup_ollama()
    _assert_hidden(caplog, "aiops.llm", "备用Ollama")
    monkeypatch.setattr(config.llm, "ollama_base_url", ollama_base_url)

    caplog.clear()
    monkeypatch.setattr("app.services.llm.OpenAI", Mock(side_effect=RuntimeError(MARKER)))
    service._init_backup_openai()
    _assert_hidden(caplog, "aiops.llm", "备用OpenAI")

    caplog.clear()
    service.provider = "openai"
    service.client = Mock()
    service.client.chat.completions.create.side_effect = RuntimeError(MARKER)
    with pytest.raises(RuntimeError):
        await service._call_openai_api([{"role": "user", "content": "hi"}], None, 0.2, 8)
    _assert_hidden(caplog, "aiops.llm", "OpenAI")

    caplog.clear()
    monkeypatch.setattr(ollama, "chat", Mock(side_effect=RuntimeError(MARKER)))
    with pytest.raises(RuntimeError):
        await service._call_ollama_api([{"role": "user", "content": "hi"}], 0.2, 8)
    _assert_hidden(caplog, "aiops.llm", "Ollama")

    caplog.clear()
    service.generate_response = AsyncMock(side_effect=RuntimeError(MARKER))
    analysis = await service.analyze_k8s_problem("kind: Deployment", "CrashLoop")
    assert analysis["problem_summary"] == "无法分析问题"
    assert MARKER not in repr(analysis)
    _assert_hidden(caplog, "aiops.llm", "K8s")

    caplog.clear()
    assert await service.generate_rca_summary({}, {}, []) is None
    _assert_hidden(caplog, "aiops.llm", "RCA")

    caplog.clear()
    assert await service.generate_fix_explanation("api", ["restart"], False) is None
    _assert_hidden(caplog, "aiops.llm", "修复说明")

    caplog.clear()
    service._check_provider_health = AsyncMock(side_effect=RuntimeError(MARKER))
    assert await service.is_healthy() is False
    _assert_hidden(caplog, "aiops.llm", "健康状态")

    caplog.clear()
    monkeypatch.setattr(ollama, "list", Mock(side_effect=RuntimeError(MARKER)))
    monkeypatch.setattr(ollama, "chat", Mock(side_effect=RuntimeError(MARKER)))
    assert await service._check_ollama_health() is False
    _assert_hidden(caplog, "aiops.llm", "Ollama")

    caplog.clear()
    from app.services.llm import LLMService

    service._check_provider_health = LLMService._check_provider_health.__get__(service)
    service._check_openai_health = AsyncMock(side_effect=RuntimeError(MARKER))
    assert await service._check_provider_health("openai") is False
    _assert_hidden(caplog, "aiops.llm", "openai")

    caplog.clear()
    import app.core.agents.fallback_models as fallback_models

    monkeypatch.setattr(fallback_models.FallbackChatModel, "_generate", Mock(side_effect=RuntimeError(MARKER)))
    text = await service._use_fallback_chat_model([{"role": "user", "content": "你好"}])
    assert text == "很抱歉，AI服务暂时不可用。请稍后重试或联系技术支持。"
    assert MARKER not in text
    _assert_hidden(caplog, "aiops.llm", "备用聊天模型")

    caplog.clear()
    repaired = await service._extract_json_from_k8s_analysis("not-json", [{"role": "user", "content": "x"}])
    assert repaired["problem_summary"] == "无法分析问题"
    assert MARKER not in repr(repaired)
    _assert_hidden(caplog, "aiops.llm", "JSON")


async def test_prometheus_query_logs_hide_exception_text(caplog, monkeypatch):
    import requests

    caplog.set_level(logging.DEBUG, logger="aiops.services.prometheus")
    service = PrometheusService()
    monkeypatch.setattr(requests, "get", Mock(side_effect=RuntimeError(MARKER)))

    assert await service.query_instant("up") is None
    _assert_hidden(caplog, "aiops.services.prometheus", "即时查询")

    caplog.clear()
    assert await service.get_available_metrics() == []
    _assert_hidden(caplog, "aiops.services.prometheus", "可用指标")

    caplog.clear()
    assert await service.get_metric_metadata("up") is None
    _assert_hidden(caplog, "aiops.services.prometheus", "元数据")


async def test_mcp_service_results_hide_exception_text(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="aiops.services.mcp")
    service = MCPService()
    service._mcp_assistant = Mock()
    service._do_health_check = AsyncMock(return_value=True)
    service._mcp_assistant.client.get_available_tools = AsyncMock(side_effect=RuntimeError(MARKER))

    info = await service.get_service_health_info()
    assert info["service"] == "mcp"
    assert info["status"] == "healthy"
    assert info["tools"]["error"] == "获取工具信息失败"
    assert MARKER not in repr(info)
    _assert_hidden(caplog, "aiops.services.mcp")

    caplog.clear()
    service._do_health_check = AsyncMock(side_effect=RuntimeError(MARKER))
    failed = await service.get_service_health_info()
    assert failed["status"] == "unhealthy"
    assert failed["error"] == "健康检查失败"
    assert set(failed) == {"service", "status", "error", "timestamp"}
    assert MARKER not in repr(failed)

    caplog.clear()
    service._mcp_assistant = None
    monkeypatch.setattr("app.services.mcp_service.MCPAssistant", Mock(side_effect=RuntimeError(MARKER)))
    with pytest.raises(AssistantError) as raised:
        await service._ensure_ready()
    assert raised.value.error_code == "ASSISTANT_ERROR"
    assert raised.value.message == "智能助手错误: MCP服务暂未就绪"
    assert raised.value.details == {}
    assert raised.value.__cause__ is None
    assert MARKER not in raised.value.message
    _assert_hidden(caplog, "aiops.services.mcp")


async def test_inspection_logs_and_task_error_hide_exception_text(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="aiops.services.inspection")
    monkeypatch.setattr(config.inspection, "enabled", True)
    monkeypatch.setattr("app.services.inspection_service.KubernetesService", Mock(side_effect=RuntimeError(MARKER)))

    class BoomProm:
        async def initialize(self):
            raise RuntimeError(MARKER)

    monkeypatch.setattr("app.services.inspection_service.PrometheusService", BoomProm)
    service = InspectionService()
    await service._do_initialize()
    assert service._k8s is None
    assert service._prom is None
    _assert_hidden(caplog, "aiops.services.inspection", "Kubernetes", "Prometheus")

    service._initialized = True
    service.run_inspection = AsyncMock(side_effect=RuntimeError(MARKER))
    submitted = await service.run_inspection_async(InspectionRunRequest())
    status = await _wait_task(service, submitted["task_id"])
    assert status["status"] == "failed"
    assert status["progress"] == 1.0
    assert status["error"] == "巡检任务失败"
    assert MARKER not in repr(status)

    service.run_inspection = AsyncMock(
        side_effect=FeatureDisabledError("巡检功能已关闭", "inspection.enabled", error_code="INSPECTION_FEATURE_DISABLED")
    )
    submitted = await service.run_inspection_async(InspectionRunRequest())
    kept = await _wait_task(service, submitted["task_id"])
    assert kept["status"] == "failed"
    assert kept["error"] == "巡检功能已关闭"


async def _wait_task(service, task_id):
    status = {"status": "running"}
    for _ in range(20):
        status = await service.get_task_status(task_id)
        if status["status"] != "running":
            return status
        await asyncio.sleep(0)
    return status


def test_fallback_models_hide_exception_text(caplog):
    from langchain_core.messages import HumanMessage

    caplog.set_level(logging.DEBUG, logger="aiops.fallback_models")
    embeddings = FallbackEmbeddings(dimension=4, enable_cache=False)
    embeddings._generate_pseudo_embedding = Mock(side_effect=RuntimeError(MARKER))
    assert embeddings.embed_documents(["hello"]) == [[0.0, 0.0, 0.0, 0.0]]
    _assert_hidden(caplog, "aiops.fallback_models", "文档嵌入")

    caplog.clear()
    assert embeddings.embed_query("hello") == [0.0, 0.0, 0.0, 0.0]
    _assert_hidden(caplog, "aiops.fallback_models", "查询嵌入")

    caplog.clear()
    model = FallbackChatModel()
    model._generate_structured_response = Mock(side_effect=RuntimeError(MARKER))
    result = model._generate([HumanMessage(content="hello")])
    assert result.generations[0].message.content == "抱歉，当前服务不可用，请稍后重试。"
    assert result.generations[0].generation_info == {"model": model.model_name, "error": "生成响应失败"}
    assert MARKER not in repr(result.generations[0].generation_info)
    _assert_hidden(caplog, "aiops.fallback_models", "备用聊天模型")


@pytest.mark.parametrize(
    ("module_name", "message"),
    [
        ("predict", "注册预测路由失败"),
        ("rca", "注册根因分析路由失败"),
        ("autofix", "注册自动修复路由失败"),
        ("assistant", "注册智能助手路由失败"),
        ("cache", "注册缓存管理路由失败"),
        ("inspection", "注册巡检路由失败"),
    ],
)
def test_route_registration_failure_hides_exception_text(caplog, module_name, message):
    import app.api.routes as routes

    full_name = f"app.api.routes.{module_name}"
    original = sys.modules.get(full_name)

    class Boom(types.ModuleType):
        def __getattr__(self, item):
            if item.startswith("__"):
                raise AttributeError(item)
            raise ImportError(MARKER)

    fake = Boom(full_name)
    fake.__package__ = "app.api.routes"
    sys.modules[full_name] = fake
    try:
        caplog.set_level(logging.DEBUG, logger="aiops.routes")
        with pytest.raises(RuntimeError) as raised:
            importlib.reload(routes)
        assert str(raised.value) == message
        assert raised.value.__cause__ is None
        assert MARKER not in str(raised.value)
        _assert_hidden(caplog, "aiops.routes", message, exc_name="ImportError")
    finally:
        caplog.clear()
        if original is not None:
            sys.modules[full_name] = original
        else:
            sys.modules.pop(full_name, None)
        importlib.reload(routes)


def test_cors_keeps_explicit_origin_error_and_hides_other_text(caplog, monkeypatch):
    from fastapi import FastAPI

    from app.api.middleware.cors import setup_cors

    caplog.set_level(logging.DEBUG, logger="aiops.cors")
    monkeypatch.setattr(config.security, "cors_origins", "*")
    with pytest.raises(ValueError) as origin_error:
        setup_cors(FastAPI())
    assert str(origin_error.value) == "CORS_ORIGINS must list explicit origins"
    assert "CORS_ORIGINS must list explicit origins" not in _rendered(caplog, "aiops.cors")
    _assert_hidden(caplog, "aiops.cors", "CORS", exc_name="ValueError")

    caplog.clear()
    monkeypatch.setattr(config.security, "cors_origins", "https://example.com")
    app = FastAPI()
    monkeypatch.setattr(app, "add_middleware", Mock(side_effect=RuntimeError(MARKER)))
    with pytest.raises(RuntimeError) as raised:
        setup_cors(app)
    assert str(raised.value) == "CORS中间件设置失败"
    assert raised.value.__cause__ is None
    assert MARKER not in str(raised.value)
    _assert_hidden(caplog, "aiops.cors", "CORS")


def test_mcp_import_failure_log_hides_exception_text(monkeypatch):
    import builtins

    import app.mcp.main as mcp_main

    real_import = builtins.__import__

    def blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name in {"app.config.settings", "app.mcp.server.mcp_server", "app.mcp.server.tools"}:
            raise ImportError(MARKER)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.setattr(mcp_main.sys, "exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    collected = _Collect()
    root = logging.getLogger()
    root.addHandler(collected)
    try:
        with pytest.raises(SystemExit) as raised:
            mcp_main._load_mcp_dependencies()
    finally:
        root.removeHandler(collected)
    assert raised.value.code == 1
    _assert_records(collected, "导入模块失败", exc_name="ImportError")


class _LeakyClientError(aiohttp.ClientError):
    def __init__(self):
        super().__init__(MARKER)


class _LeakySession:
    async def __aenter__(self):
        raise _LeakyClientError()

    async def __aexit__(self, *args):
        return False


@pytest.mark.parametrize("method", ["execute_tool", "get_available_tools"])
async def test_mcp_client_connection_error_is_generic(caplog, monkeypatch, method):
    from app.mcp.mcp_client import MCPSessionClient

    caplog.set_level(logging.DEBUG, logger="aiops.mcp.client")
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: _LeakySession())
    client = MCPSessionClient("http://offline.invalid")
    with pytest.raises(RuntimeError) as raised:
        if method == "execute_tool":
            await client.execute_tool("offline", {"password": MARKER})
        else:
            await client.get_available_tools()
    assert str(raised.value) == "MCP服务连接失败"
    assert raised.value.__cause__ is None
    assert MARKER not in str(raised.value)
    _assert_hidden(caplog, "aiops.mcp.client", exc_name="_LeakyClientError")


async def test_autofix_debug_logs_drop_traceback_and_exception_text(caplog):
    caplog.set_level(logging.DEBUG, logger="aiops.services.autofix")
    service = AutoFixService(approvals=WorkflowApprovals())
    service._initialized = True
    service._k8s_fixer = Mock()
    service._supervisor = Mock()
    service.execute_with_timeout = AsyncMock(side_effect=RuntimeError(MARKER))

    assert await service._do_health_check() is False
    _assert_hidden(caplog, "aiops.services.autofix", "健康检查")

    caplog.clear()
    service._k8s_fixer.k8s_service = Mock()

    async def logs_timeout(operation, timeout, operation_name):
        if operation_name == "get_pod_logs":
            raise RuntimeError(MARKER)
        if operation_name == "get_pods":
            return [{"name": "api-0"}]
        return []

    service.execute_with_timeout = logs_timeout
    result = await service._diagnose_kubernetes_issues("api", "default", include_pods=True, include_logs=True, include_events=False)
    assert result["diagnosis"]["logs"] == {"api-0": "获取日志失败"}
    assert MARKER not in repr(result)
    _assert_hidden(caplog, "aiops.services.autofix", "api-0")

    caplog.clear()
    service.execute_with_timeout = AsyncMock(side_effect=RuntimeError(MARKER))
    with pytest.raises(AutoFixInternalError) as diagnosed:
        await service._diagnose_kubernetes_issues("api", "default", include_pods=False, include_logs=False, include_events=False)
    assert diagnosed.value.error_code == "AUTOFIX_INTERNAL_ERROR"
    assert MARKER not in diagnosed.value.message
    assert diagnosed.value.details == {}
    _assert_hidden(caplog, "aiops.services.autofix", "诊断")

    caplog.clear()
    service._k8s_fixer = Mock()
    service._k8s_fixer.cleanup = Mock(side_effect=RuntimeError(MARKER))
    service._supervisor = Mock()
    service._supervisor.cleanup = Mock(side_effect=RuntimeError(MARKER))
    await service.cleanup()
    _assert_hidden(caplog, "aiops.services.autofix", "清理")

    service._initialized = True
    with pytest.raises(ValidationError) as invalid:
        await service._diagnose_kubernetes_issues("api", namespace="")
    assert invalid.value.error_code == "VALIDATION_ERROR"
    assert "命名空间不能为空" in str(invalid.value)
