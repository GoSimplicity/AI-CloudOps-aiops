"""MCP 日志不得记录参数值、结果内容或异常携带的凭据"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.mcp import main
from app.mcp.mcp_client import MCPSessionClient
from app.mcp.server.mcp_server import MCPServer


@pytest.mark.parametrize("failure", [False, True])
def test_tool_http_and_core_logs_exclude_payload_and_error_text(monkeypatch, caplog, failure):
    marker = "private-PASS-78261"
    caplog.set_level(logging.INFO, logger="aiops.mcp.core")
    caplog.set_level(logging.INFO, logger="aiops.mcp.main")
    server = MCPServer()
    execution = AsyncMock(side_effect=RuntimeError(marker) if failure else None, return_value={"output": marker})
    server.tools["offline"] = SimpleNamespace(execute=execution)
    monkeypatch.setattr(main, "mcp_server", server)
    app = FastAPI()
    app.post("/tools/execute")(main.execute_tool)
    with caplog.at_level(logging.INFO):
        response = TestClient(app).post("/tools/execute", json={"tool": "offline", "parameters": {"password": marker, "script": "print('" + marker + "')"}})
    assert response.status_code == 200
    assert marker not in caplog.text
    assert "offline" in caplog.text
    if failure:
        assert marker not in response.text
        assert response.json()["error"]
        assert "RuntimeError" in caplog.text
    else:
        assert response.json()["result"]["output"] == marker


async def test_client_error_logs_exclude_url_credentials_and_exception_text(monkeypatch, caplog):
    marker = "private-PASS-78261"
    caplog.set_level(logging.INFO, logger="aiops.mcp.client")

    class BrokenSession:
        async def __aenter__(self):
            raise RuntimeError(marker)

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: BrokenSession())
    with caplog.at_level(logging.INFO):
        client = MCPSessionClient(f"https://user:{marker}@offline.invalid")
        with pytest.raises(RuntimeError):
            await client.execute_tool("offline", {"password": marker})
    assert marker not in caplog.text
