"""MCP 生命周期、SSE 关闭与工具超时的离线异步回归"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config.settings import config
from app.mcp import main
from app.mcp.server import write_gate
from app.mcp.server.mcp_server import MCPServer


@pytest.fixture
def offline_tool_is_read_only(monkeypatch):
    """SEC-33 门禁把未归类工具按写处理；生命周期测试里的假工具 offline 显式登记为只读"""
    monkeypatch.setattr(write_gate, "READ_ONLY_TOOLS", write_gate.READ_ONLY_TOOLS | {"offline"})


@pytest.mark.parametrize("registration_failure", [False, True])
async def test_mcp_shutdown_runs_on_context_or_registration_failure(monkeypatch, registration_failure):
    server = Mock()
    server.register_tool = AsyncMock(side_effect=RuntimeError("registration") if registration_failure else None)
    server.shutdown = AsyncMock()
    monkeypatch.setattr(main, "MCPServer", Mock(return_value=server))
    monkeypatch.setattr(main, "mcp_tools", lambda: [SimpleNamespace(name="offline")])
    with pytest.raises(RuntimeError):
        async with main.lifespan(FastAPI()):
            raise RuntimeError("context")
    server.shutdown.assert_awaited_once()
    assert main.mcp_server is None


async def test_sse_aclose_cleans_registration_without_yielding(monkeypatch):
    monkeypatch.setattr(main, "mcp_server", SimpleNamespace(tools={}))
    request = Mock()
    request.is_disconnected = AsyncMock(return_value=False)
    response = await main.sse_endpoint(request)
    iterator = response.body_iterator
    await anext(iterator)
    assert id(request) in main.active_sse_connections
    await iterator.aclose()
    assert id(request) not in main.active_sse_connections


async def test_slow_tool_is_cancelled_before_timeout_returns(monkeypatch, offline_tool_is_read_only):
    from app.mcp.server import mcp_server

    monkeypatch.setattr(config.mcp, "timeout", 0.01)
    server = mcp_server.MCPServer()
    cancelled = asyncio.Event()

    async def execute(parameters):
        try:
            await asyncio.sleep(0.08)
            return "finished too late"
        finally:
            cancelled.set()

    tool = SimpleNamespace(name="offline", execute=execute)
    await server.register_tool(tool)
    with pytest.raises(asyncio.TimeoutError):
        await server.execute_tool("offline", {})
    assert cancelled.is_set()


async def test_shutdown_cancels_running_tool_before_cleanup(offline_tool_is_read_only):
    server = MCPServer()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    cleaned = asyncio.Event()
    release = asyncio.Event()

    async def execute(parameters):
        started.set()
        try:
            await release.wait()
        finally:
            cancelled.set()

    async def cleanup():
        assert cancelled.is_set()
        cleaned.set()

    await server.register_tool(SimpleNamespace(name="offline", execute=execute, cleanup=cleanup))
    task = asyncio.create_task(server.execute_tool("offline", {}))
    # 有界等待：工具若在开始前就失败（例如被门禁拒绝），这里应快速失败而不是无限挂起
    waiter = asyncio.ensure_future(started.wait())
    await asyncio.wait({waiter, task}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    if not started.is_set():
        waiter.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        pytest.fail(f"tool never started: {task.exception() if task.done() else 'timeout'!r}")
    try:
        await server.shutdown()
        assert task.done()
        assert cancelled.is_set()
        assert cleaned.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_http_tool_timeout_has_nonempty_protocol_error(monkeypatch):
    server = Mock(tools={"offline": object()})
    server.execute_tool = AsyncMock(side_effect=asyncio.TimeoutError())
    monkeypatch.setattr(main, "mcp_server", server)
    app = FastAPI()
    app.post("/tools/execute")(main.execute_tool)
    response = TestClient(app).post("/tools/execute", json={"tool": "offline"})
    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert "timed out" in response.json()["error"]


async def test_sse_shutdown_wakes_pending_heartbeat(monkeypatch):
    shutdown = asyncio.Event()
    monkeypatch.setattr(main, "sse_shutdown", shutdown)
    monkeypatch.setattr(main, "mcp_server", SimpleNamespace(tools={}))
    request = Mock()
    request.is_disconnected = AsyncMock(return_value=False)
    iterator = (await main.sse_endpoint(request)).body_iterator
    await anext(iterator)
    await anext(iterator)
    pending = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)
    shutdown.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, timeout=0.5)
    assert id(request) not in main.active_sse_connections


async def test_k8s_tool_cleanup_closes_owned_clients_once():
    from app.mcp.server.tools.k8s_pod_tool import K8sPodTool

    tool = K8sPodTool()
    original = tool._executor
    executor = Mock()
    client = Mock()
    original.shutdown(wait=False)
    tool._executor = executor
    tool._api_clients = {"one": SimpleNamespace(api_client=client), "two": SimpleNamespace(api_client=client)}
    await tool.cleanup()
    executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    client.close.assert_called_once()
    assert tool._api_clients is None
    await tool.cleanup()
    client.close.assert_called_once()


async def test_each_mcp_lifecycle_uses_fresh_tools(monkeypatch):
    monkeypatch.setattr(main, "mcp_tools", lambda: [])
    async with main.lifespan(FastAPI()):
        first = main.mcp_server
    async with main.lifespan(FastAPI()):
        assert main.mcp_server is not first
        assert main.mcp_server._closing is False
    assert main.mcp_server is None


async def test_system_cpu_sampling_runs_outside_event_loop(monkeypatch):
    import threading

    import psutil

    from app.mcp.server.tools.system_info_tool import SystemInfoTool

    event_loop_thread = threading.get_ident()
    threads = []

    def sample(*args, **kwargs):
        threads.append(threading.get_ident())
        return 10

    monkeypatch.setattr(psutil, "cpu_percent", sample)
    result = await SystemInfoTool().execute({"info_type": "cpu"})
    assert result["cpu"]["usage_percent"] == 10
    assert threads and threads[0] != event_loop_thread
