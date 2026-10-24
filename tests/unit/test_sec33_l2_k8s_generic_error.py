#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SEC-33 L2 P3-4：K8s 工具基类不得把原始异常文本 str(e) 返回给调用方"""

from contextlib import ExitStack
import logging

import pytest

from app.mcp.server.tools import k8s_base_tool
from app.mcp.server.tools.k8s_base_tool import K8sBaseTool, K8sToolInputError

MARKER = "sec33-l2-P34-MARKER-90417"


class _Tool(K8sBaseTool):
    def __init__(self, error):
        super().__init__("sec33_l2_tool", "synthetic")
        self.error = error

    def get_parameters(self):
        return {"type": "object", "properties": {}}

    async def _execute_internal(self, parameters):
        raise self.error


@pytest.fixture
def capture(caplog):
    with ExitStack() as stack:
        for name in (None, "aiops", "aiops.mcp.k8s"):
            stack.enter_context(caplog.at_level(logging.DEBUG, logger=name))
        yield caplog


def _rendered(caplog, level):
    return "\n".join(f"{r.getMessage()}\n{logging.Formatter().formatException(r.exc_info) if r.exc_info else ''}" for r in caplog.records if r.levelno >= level)


async def test_unknown_exception_returns_generic_message(capture):
    result = await _Tool(RuntimeError(f"token={MARKER}")).execute({})
    assert result["success"] is False
    assert result["error"] == "执行失败"
    assert result["message"] == "K8s工具执行失败"
    assert MARKER not in repr(result)
    assert MARKER not in _rendered(capture, logging.INFO)
    assert any("RuntimeError" in r.getMessage() and r.levelno == logging.ERROR for r in capture.records)
    assert any(MARKER in logging.Formatter().formatException(r.exc_info) for r in capture.records if r.levelno == logging.DEBUG and r.exc_info)


async def test_safe_input_error_message_passes_through(capture):
    result = await _Tool(K8sToolInputError("K8s工具已绑定首次配置，不能在请求中切换集群")).execute({})
    assert result["message"] == "K8s工具已绑定首次配置，不能在请求中切换集群"


async def test_kubeconfig_load_failure_does_not_leak(capture, tmp_path, monkeypatch):
    # SEC-46：按路径加载只剩默认来源（~/.kube/config）
    home = tmp_path / "home"
    (home / ".kube").mkdir(parents=True)
    (home / ".kube" / "config").write_text("synthetic", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    def boom(*args, **kwargs):
        raise RuntimeError(f"load failed {MARKER}")

    monkeypatch.setattr(k8s_base_tool.config, "load_kube_config", boom)

    class _InitTool(_Tool):
        async def _execute_internal(self, parameters):
            self._initialize_clients(None)

    result = await _InitTool(None).execute({})
    assert result["message"] == "无法加载Kubernetes配置"
    assert MARKER not in repr(result)
    assert MARKER not in _rendered(capture, logging.INFO)


async def test_custom_kubeconfig_load_failure_does_not_leak(capture, tmp_path, monkeypatch):
    from app.config.settings import config

    allowed = tmp_path / "kubeconfigs"
    allowed.mkdir()
    (allowed / "alt.yaml").write_text("clusters: []\nusers: []\n", encoding="utf-8")
    monkeypatch.setattr(config.mcp, "kubeconfig_allowed_dir", str(allowed))

    def boom(*args, **kwargs):
        raise RuntimeError(f"load failed {MARKER}")

    monkeypatch.setattr(k8s_base_tool.config, "load_kube_config_from_dict", boom)

    class _InitTool(_Tool):
        async def _execute_internal(self, parameters):
            self._initialize_clients(str(allowed / "alt.yaml"))

    result = await _InitTool(None).execute({})
    assert "config_path 不可用" in result["message"]
    assert MARKER not in repr(result)
    assert MARKER not in _rendered(capture, logging.INFO)
