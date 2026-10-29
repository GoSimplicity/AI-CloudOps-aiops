"""原生模型工具调用链，不使用关键词路由或真实模型"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai.types.chat import ChatCompletionMessage
import pytest

from app.api.routes import assistant as assistant_routes
from app.common.exceptions import AssistantError
from app.mcp.mcp_client import MCPAssistant
from app.services.assistant_service import OptimizedAssistantService
from app.services.mcp_service import MCPService


def response(content=None, name=None, arguments='{"expression":"2+3*4"}'):
    calls = [{"id": "call-1", "type": "function", "function": {"name": name, "arguments": arguments}}] if name else None
    return SimpleNamespace(choices=[SimpleNamespace(message=ChatCompletionMessage(role="assistant", content=content, tool_calls=calls))])


def make_assistant(responses, result=14):
    assistant = object.__new__(MCPAssistant)
    assistant.client = AsyncMock()
    assistant.client.get_available_tools.return_value = {"tools": [{
        "name": "calculate", "description": "计算表达式",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
    }]}
    assistant.client.execute_tool.return_value = result
    create = AsyncMock(side_effect=responses)
    assistant.llm_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    assistant.model = "offline"
    assistant._history = {}
    assistant._max_history_turns = 6
    return assistant


@pytest.mark.parametrize("result,answer", [(14, "14"), (0, "0"), (False, "false")])
async def test_native_tool_calls_execute_and_return_model_answer(result, answer):
    assistant = make_assistant([response(name="calculate"), response(content=answer)], result)
    assert await assistant.process_query("仅使用工具给出结果", "session") == answer
    assistant.client.execute_tool.assert_awaited_once_with("calculate", {"expression": "2+3*4"})
    calls = assistant.llm_client.chat.completions.create.call_args_list
    assert calls[0].kwargs["tools"][0]["function"]["name"] == "calculate"
    assert calls[1].kwargs["messages"][-1]["role"] == "tool"
    assert calls[1].kwargs["messages"][-1]["tool_call_id"] == "call-1"
    assert assistant._history["service:internal:session"][-1]["a"] == answer


@pytest.mark.parametrize("name,arguments", [("not-advertised", "{}"), ("calculate", "not json"), ("calculate", "[]")])
async def test_invalid_tool_call_never_executes(name, arguments):
    assistant = make_assistant([response(name=name, arguments=arguments)])
    with pytest.raises(AssistantError):
        await assistant.process_query("几点了")
    assistant.client.execute_tool.assert_not_awaited()


async def test_provider_failure_has_no_keyword_execution_fallback():
    assistant = make_assistant([RuntimeError("private provider error")])
    with pytest.raises(AssistantError):
        await assistant.process_query("现在几点了")
    assistant.client.execute_tool.assert_not_awaited()


def test_mcp_failure_is_http_error_and_does_not_initialize_rag(monkeypatch):
    mcp = MCPService()
    mcp._initialized = True
    mcp._mcp_assistant = SimpleNamespace(process_query=AsyncMock(side_effect=RuntimeError("private-token")))
    from app.services.assistant_sessions import AssistantSessions
    service = OptimizedAssistantService(sessions=AssistantSessions())
    service.initialize = AsyncMock(side_effect=AssertionError("MCP must not initialize RAG"))
    monkeypatch.setattr(service, "_get_mcp_service", AsyncMock(return_value=mcp))
    monkeypatch.setattr(assistant_routes, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    app.include_router(assistant_routes.router, prefix="/assistant")
    response = TestClient(app).post("/assistant/query", json={"question": "计算", "mode": 2})
    assert response.status_code == 502
    assert "private-token" not in response.text
    service.initialize.assert_not_awaited()


async def test_service_creation_can_defer_rag_initialization(monkeypatch):
    from app.services.factory import ServiceFactory

    class Factory(ServiceFactory):
        _instances = {}
        _locks = {}
        _initialization_tasks = {}

    service = await Factory.get_service("assistant", OptimizedAssistantService, initialize=False)
    service.initialize = AsyncMock()
    import asyncio

    await asyncio.sleep(0)
    service.initialize.assert_not_awaited()
    await Factory.cleanup_all()


async def test_assistant_cleanup_closes_model_connection_pool():
    assistant = make_assistant([])
    await assistant.cleanup()
    assistant.llm_client.close.assert_awaited_once()


async def test_tool_reported_failure_is_not_formatted_as_success():
    assistant = make_assistant([response(name="calculate")], result={"success": False, "error": "offline"})
    with pytest.raises(AssistantError):
        await assistant.process_query("计算")
    assert assistant.llm_client.chat.completions.create.await_count == 1


async def test_model_tool_loop_has_a_finite_request_budget():
    assistant = make_assistant([response(name="calculate")] * 4)
    with pytest.raises(AssistantError, match="轮次"):
        await assistant.process_query("执行工具", "session")
    assert assistant.llm_client.chat.completions.create.await_count == 4
    assert "session" not in assistant._history
