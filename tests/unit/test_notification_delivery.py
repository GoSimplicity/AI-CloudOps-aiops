"""通知投递在离线替身下验证，禁止向真实 Webhook 发送消息"""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import requests

from app.api.routes import autofix
from app.services.notification import NotificationService


@pytest.fixture
def service():
    service = NotificationService()
    service.enabled = True
    service.feishu_webhook = "https://offline.invalid/webhook/test-token"
    return service


async def test_slow_delivery_does_not_block_event_loop(monkeypatch, service):
    started = threading.Event()
    released = threading.Event()
    request_threads = []
    event_loop_thread = threading.get_ident()

    def slow_post(url, **kwargs):
        request_threads.append(threading.get_ident())
        assert kwargs["timeout"] == service.DEFAULT_REQUEST_TIMEOUT
        card = json.loads(kwargs["data"])
        assert card["card"]["header"]["title"]["content"] == "测试"
        started.set()
        if not released.wait(2):
            raise requests.Timeout("offline")
        return Mock(status_code=200, json=Mock(return_value={"code": 0}))

    monkeypatch.setattr(requests, "post", slow_post)
    task = asyncio.create_task(service.send_notification("测试", "离线消息"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0)
        assert not task.done()
        assert len(request_threads) == 1
        assert request_threads[0] != event_loop_thread
    finally:
        released.set()
        await task
    assert task.result() is True


@pytest.mark.parametrize("status,payload,expected", [(200, {"code": 0}, True), (200, {"code": 999}, False), (500, {"code": 0}, False), (200, {}, False), (200, [], False)])
async def test_delivery_checks_transport_and_provider_result(monkeypatch, service, status, payload, expected):
    post = Mock(return_value=Mock(status_code=status, json=Mock(return_value=payload)))
    monkeypatch.setattr(requests, "post", post)
    assert await service.send_notification("测试", "离线消息") is expected
    post.assert_called_once()


@pytest.mark.parametrize("error", [requests.Timeout("offline"), requests.ConnectionError("offline"), ValueError("invalid json")])
async def test_delivery_failure_returns_false(monkeypatch, service, error):
    monkeypatch.setattr(requests, "post", Mock(side_effect=error))
    assert await service.send_notification("测试", "离线消息") is False


@pytest.mark.parametrize("enabled,webhook", [(False, "https://offline.invalid"), (True, "")])
async def test_disabled_or_missing_webhook_never_posts(monkeypatch, service, enabled, webhook):
    service.enabled = enabled
    service.feishu_webhook = webhook
    post = Mock(side_effect=AssertionError("must not send"))
    monkeypatch.setattr(requests, "post", post)
    assert await service.send_notification("测试", "离线消息") is False
    post.assert_not_called()


@pytest.mark.parametrize("outcome,status", [(True, 200), (False, 503), (RuntimeError("private-token"), 503)])
def test_notify_endpoint_reports_actual_delivery(monkeypatch, outcome, status):
    notification = AsyncMock()
    if isinstance(outcome, Exception):
        notification.send_notification.side_effect = outcome
    else:
        notification.send_notification.return_value = outcome
    monkeypatch.setattr(autofix, "get_notification_service", AsyncMock(return_value=notification))
    app = FastAPI()
    app.include_router(autofix.router, prefix="/autofix")
    response = TestClient(app).post("/autofix/notify", json={"message": "离线消息"})
    assert response.status_code == status
    assert "private-token" not in response.text
    if status == 200:
        assert response.json()["data"]["success"] is True


async def test_retry_helper_uses_public_signature_and_retries_false(monkeypatch):
    notification = AsyncMock()
    notification.send_notification.side_effect = [False, True]
    monkeypatch.setattr(autofix, "get_notification_service", AsyncMock(return_value=notification))
    monkeypatch.setattr(autofix.ServiceConstants, "DEFAULT_RETRY_DELAY", 0)
    assert await autofix.send_fix_notification("web", "test", "completed", ["restart"]) is True
    assert notification.send_notification.await_count == 2
    assert notification.send_notification.call_args.kwargs["notification_type"] == "info"
