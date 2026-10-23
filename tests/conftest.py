"""默认测试完全离线；真实服务验收只能通过显式环境开关选择"""

import os
from pathlib import Path
import socket
import sys

import pytest

# 在导入应用前禁用本机 .env 和生产配置
os.environ["ENV"] = "test"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
if os.getenv("AIOPS_RUN_EXTERNAL_TESTS") != "1":
    os.environ["LLM_API_KEY"] = ""
    os.environ["AIOPS_API_KEY"] = "offline-service-key-with-32-characters"
    os.environ["K8S_CONFIG_PATH"] = "/nonexistent"
    os.environ["KUBECONFIG"] = "/nonexistent"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest_plugins = ["pytester"]


class OfflineNetworkError(AssertionError):
    """离线测试尝试访问真实网络"""


_ORIGINAL_NETWORK = {
    "getaddrinfo": socket.getaddrinfo,
    "connect": socket.socket.connect,
    "connect_ex": socket.socket.connect_ex,
    "sendto": socket.socket.sendto,
}


def pytest_sessionstart(session):
    """收集阶段也禁止模块导入副作用连接网络"""
    def resolution(*args, **kwargs):
        raise OfflineNetworkError("测试收集阶段禁止真实网络访问")

    def wrap(name):
        def operation(sock, *args):
            if sock.family in {socket.AF_INET, socket.AF_INET6}:
                raise OfflineNetworkError("测试收集阶段禁止真实网络访问")
            return _ORIGINAL_NETWORK[name](sock, *args)
        return operation

    socket.getaddrinfo = resolution
    for name in ("connect", "connect_ex", "sendto"):
        setattr(socket.socket, name, wrap(name))


def pytest_sessionfinish(session, exitstatus):
    socket.getaddrinfo = _ORIGINAL_NETWORK["getaddrinfo"]
    for name in ("connect", "connect_ex", "sendto"):
        setattr(socket.socket, name, _ORIGINAL_NETWORK[name])


def pytest_ignore_collect(collection_path, config):
    if "external" in collection_path.parts:
        if os.getenv("AIOPS_RUN_EXTERNAL_TESTS") != "1":
            return True
        if collection_path.name == "test_model_tools.py" and os.getenv("AIOPS_RUN_MODEL_TESTS") != "1":
            return True
    return None


def pytest_configure(config):
    if os.getenv("AIOPS_RUN_EXTERNAL_TESTS") == "1":
        if not os.getenv("AIOPS_EXTERNAL_BASE_URL") or not os.getenv("AIOPS_API_KEY"):
            raise pytest.UsageError("真实服务验收要求显式 AIOPS_EXTERNAL_BASE_URL 和 AIOPS_API_KEY，禁止使用默认地址或空凭据")


def pytest_collection_modifyitems(items):
    for item in items:
        if "unit" in item.path.parts:
            item.add_marker(pytest.mark.unit)


@pytest.fixture(autouse=True)
def offline_network_guard(request, monkeypatch):
    """外部访问即使被业务代码捕获也会使离线测试失败"""
    if request.node.get_closest_marker("external"):
        if os.getenv("AIOPS_RUN_EXTERNAL_TESTS") != "1":
            pytest.fail("external 测试缺少 AIOPS_RUN_EXTERNAL_TESTS=1")
        if request.node.get_closest_marker("model") and os.getenv("AIOPS_RUN_MODEL_TESTS") != "1":
            pytest.fail("model 测试缺少 AIOPS_RUN_MODEL_TESTS=1")
        monkeypatch.setattr(socket, "getaddrinfo", _ORIGINAL_NETWORK["getaddrinfo"])
        for name in ("connect", "connect_ex", "sendto"):
            monkeypatch.setattr(socket.socket, name, _ORIGINAL_NETWORK[name])
        yield
        return
    attempts = []

    def deny_resolution(*args, **kwargs):
        attempts.append("DNS")
        raise OfflineNetworkError("离线测试禁止真实 DNS/网络访问；请注入客户端替身")

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto

    def connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            attempts.append("TCP/UDP")
            raise OfflineNetworkError("离线测试禁止真实 TCP/UDP 连接；请注入客户端替身")
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            attempts.append("TCP/UDP")
            raise OfflineNetworkError("离线测试禁止真实 TCP/UDP 连接；请注入客户端替身")
        return original_connect_ex(sock, address)

    def sendto(sock, *args):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            attempts.append("UDP")
            raise OfflineNetworkError("离线测试禁止真实 UDP 投递；请注入客户端替身")
        return original_sendto(sock, *args)

    monkeypatch.setattr(socket, "getaddrinfo", deny_resolution)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    yield
    assert not attempts, f"离线测试尝试访问外部网络（{len(attempts)}次）；不能将被捕获的连接失败当作验证通过"


@pytest.fixture
def app(monkeypatch):
    """真实 FastAPI 应用及路由，业务依赖由各测试显式替换"""
    from app.config.settings import config
    from app.main import create_app

    monkeypatch.setenv("AIOPS_PREWARM", "false")
    monkeypatch.setattr(config.security, "api_key", "offline-service-key-with-32-characters")
    return create_app()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app, headers={"X-API-Key": "offline-service-key-with-32-characters"}) as client:
        yield client
