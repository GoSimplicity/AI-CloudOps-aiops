#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SEC-33 L2 P3-3：operation 存在但不是字符串时，门禁先做类型检查，返回结构化错误码并写审计日志"""

import logging
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.common.exceptions import FeatureDisabledError
from app.config.settings import config
from app.mcp import main
from app.mcp.server import write_gate
from app.mcp.server.mcp_server import MCPServer
from app.mcp.server.tools import create_tools

MARKER = "sec33-l2-P33-MARKER-31877"
OPERATION_TOOLS = sorted(tool.name for tool in create_tools() if (tool.parameters.get("properties", {}).get("operation") or {}).get("enum"))
BAD_OPERATIONS = [["delete_pod"], {"op": "delete_pod"}, 7, None, True]


class _Clients(dict):
    def __init__(self, api):
        super().__init__()
        self.api = api

    def __getitem__(self, key):
        return self.api

    def get(self, key, default=None):
        return self.api


@pytest.fixture
def gates_open(monkeypatch):
    """所有开关都打开、白名单含 default/staging：只验证类型检查本身"""
    monkeypatch.setattr(config.mcp, "write_tools_enabled", True)
    monkeypatch.setattr(config.mcp, "secret_read_enabled", True)
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default", "staging"])


@pytest.fixture
async def server_with_mocks():
    server = MCPServer()
    api = Mock(name="k8s-api")
    init = Mock(name="initialize_clients", return_value=_Clients(api))
    for tool in create_tools():
        if hasattr(tool, "_initialize_clients"):
            tool._initialize_clients = init
        await server.register_tool(tool)
    try:
        yield server, api, init
    finally:
        await server.shutdown()


def test_invalid_operation_code_is_registered():
    assert write_gate.MCP_INVALID_OPERATION == "MCP_INVALID_OPERATION"
    assert write_gate.MCP_INVALID_OPERATION in write_gate.ERROR_CODES


@pytest.mark.parametrize("operation", BAD_OPERATIONS, ids=repr)
def test_classify_non_str_operation_is_invalid(operation):
    assert write_gate.classify("k8s_resource_operations", {"operation": operation, "resource_type": "pod"}).kind == "invalid"


@pytest.mark.parametrize("operation", BAD_OPERATIONS, ids=repr)
@pytest.mark.parametrize("tool", OPERATION_TOOLS)
async def test_non_str_operation_denied_with_code_audit_and_zero_calls(gates_open, server_with_mocks, caplog, tool, operation):
    server, api, init = server_with_mocks
    caplog.set_level(logging.DEBUG)
    with pytest.raises(FeatureDisabledError) as denied:
        await server.execute_tool(tool, {"operation": operation, "namespace": "staging", "resource_type": "pod", "resource_name": MARKER})
    assert denied.value.error_code == write_gate.MCP_INVALID_OPERATION
    assert denied.value.status_code == 403
    assert denied.value.message == write_gate.INVALID_OPERATION_MESSAGE
    audit = [r for r in caplog.records if r.name == "aiops.mcp.write_gate" and r.levelno == logging.WARNING]
    assert audit and write_gate.MCP_INVALID_OPERATION in audit[-1].getMessage() and tool in audit[-1].getMessage()
    assert MARKER not in caplog.text
    init.assert_not_called()
    assert api.mock_calls == []


async def test_http_non_str_operation_returns_structured_error(gates_open, server_with_mocks, monkeypatch, caplog):
    server, api, init = server_with_mocks
    monkeypatch.setattr(main, "mcp_server", server)
    app = FastAPI()
    app.post("/tools/execute")(main.execute_tool)
    caplog.set_level(logging.DEBUG)
    response = TestClient(app).post("/tools/execute", json={"tool": "k8s_resource_operations", "parameters": {"operation": ["add_labels", MARKER], "resource_type": "pod", "namespace": "staging"}})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error" and body["result"] is None
    assert body["error_code"] == write_gate.MCP_INVALID_OPERATION
    assert MARKER not in response.text and MARKER not in caplog.text
    init.assert_not_called()
    assert api.mock_calls == []
