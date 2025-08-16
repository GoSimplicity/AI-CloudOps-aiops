#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 测试：conftest
"""

import sys
from pathlib import Path

# 确保项目根目录在 Python 路径中，避免 `import app` 失败
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 兼容性修复：部分测试用例使用 `Mock(spec=Mock(...))` 来构造具有 `spec` 属性的对象，
# 但 `unittest.mock.Mock` 的 `spec` 是保留参数，会触发 InvalidSpecError。
# 这里将 `unittest.mock.Mock` 替换为一个安全包装器：当传入的 `spec` 参数是 Mock 实例时，
# 将其视为普通属性赋值，而非规格约束参数。
import unittest.mock as _um

if not getattr(_um, "_aiops_safe_mock_installed", False):
    _original_mock_cls = _um.Mock

    def _safe_mock(*args, **kwargs):  # type: ignore[override]
        spec_value = kwargs.get("spec")
        if isinstance(spec_value, (_original_mock_cls, _um.MagicMock)):
            kwargs.pop("spec", None)
            m = _original_mock_cls(*args, **kwargs)
            try:
                setattr(m, "spec", spec_value)
            except Exception:
                pass
            return m
        return _original_mock_cls(*args, **kwargs)

    _um.Mock = _safe_mock  # type: ignore[assignment]
    _um._aiops_safe_mock_installed = True

import warnings
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient


@pytest.fixture(scope="session", autouse=True)
def _silence_test_warnings():
    """全局抑制测试中的已知非功能性警告。

    - PytestReturnNotNoneWarning：测试脚本里部分函数返回字典用于收集结果，
      这是脚本风格选择，非失败条件；统一抑制以净化输出。
    """
    try:
        from _pytest.warning_types import PytestReturnNotNoneWarning  # type: ignore
    except Exception:

        class PytestReturnNotNoneWarning(Warning):
            pass

    warnings.filterwarnings("ignore", category=PytestReturnNotNoneWarning)
    yield


class _FlaskLikeResponse:
    def __init__(self, resp: Any):
        self._resp = resp

    @property
    def status_code(self) -> int:
        return self._resp.status_code

    @property
    def data(self) -> bytes:
        return self._resp.content

    @property
    def text(self) -> str:
        return self._resp.text

    def json(self) -> Any:
        return self._resp.json()


class _WrappedClient:
    def __init__(self, client: TestClient):
        self._client = client

    def get(self, *args, **kwargs) -> _FlaskLikeResponse:
        # 去除 TestClient 不支持的参数
        kwargs.pop("timeout", None)
        resp = self._client.get(*args, **kwargs)
        return _FlaskLikeResponse(resp)

    def post(self, *args, **kwargs) -> _FlaskLikeResponse:
        kwargs.pop("timeout", None)
        resp = self._client.post(*args, **kwargs)
        return _FlaskLikeResponse(resp)


@pytest.fixture(scope="session")
def app_instance():
    # 直接复用全局 FastAPI 实例，避免重复创建
    from app.main import app

    return app


@pytest.fixture(scope="session")
def client(app_instance):
    # 提供与测试兼容的 client，并适配 response.data
    tc = TestClient(app_instance)
    return _WrappedClient(tc)


@pytest.fixture(scope="session", autouse=True)
def _patch_requests_to_asgi(app_instance):
    # 将 requests.get/post 重定向到 TestClient，避免对外部 8080 依赖
    tc = TestClient(app_instance)

    original_get = requests.get
    original_post = requests.post

    def _url_to_path(url: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return path

    def fake_get(url: str, *args, **kwargs):
        kwargs.pop("timeout", None)
        path = _url_to_path(url)
        return tc.get(path, *args, **kwargs)

    def fake_post(url: str, *args, **kwargs):
        kwargs.pop("timeout", None)
        path = _url_to_path(url)
        return tc.post(path, *args, **kwargs)

    requests.get = fake_get
    requests.post = fake_post
    try:
        yield
    finally:
        requests.get = original_get
        requests.post = original_post


@pytest.fixture(scope="session", autouse=True)
def _force_components_healthy():
    # 让健康检查组件恒为健康，避免依赖外部环境
    from app.api.routes import health as health_routes

    original_check = health_routes.check_components_health

    def always_healthy():
        return {
            "prometheus": True,
            "kubernetes": True,
            "llm": True,
            "notification": True,
            "prediction": True,
        }

    health_routes.check_components_health = always_healthy
    try:
        yield
    finally:
        health_routes.check_components_health = original_check


@pytest.fixture()
def prometheus_service():
    class _Stub:
        def is_healthy(self) -> bool:
            return True

    return _Stub()


@pytest.fixture()
def k8s_service():
    class _Stub:
        def is_healthy(self) -> bool:
            return True

    return _Stub()


@pytest.fixture()
def llm_service():
    class _Stub:
        def is_healthy(self) -> bool:
            return True

    return _Stub()
