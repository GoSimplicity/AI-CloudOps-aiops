#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

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
