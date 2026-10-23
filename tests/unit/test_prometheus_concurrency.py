"""慢速指标源不能阻塞 API 的事件循环"""

import asyncio
from datetime import datetime, timedelta
import threading
from unittest.mock import Mock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import requests

from app.services.prometheus import PrometheusService


@pytest.mark.parametrize(
    "operation",
    ["_do_health_check", "_test_connection", "query_range", "query_instant", "get_available_metrics", "health_check", "get_metric_metadata"],
)
async def test_prometheus_io_runs_outside_event_loop(monkeypatch, operation):
    event_loop_thread = threading.get_ident()
    request_threads = []
    response = Mock(status_code=200)
    response.json.return_value = {"status": "success", "data": {"result": []}}

    def get(*args, **kwargs):
        request_threads.append(threading.get_ident())
        assert kwargs["timeout"] == 30
        return response

    monkeypatch.setattr(requests, "get", get)
    service = PrometheusService()
    service.timeout = 30
    now = datetime.now()
    args = {
        "query_range": ("up", now - timedelta(hours=1), now),
        "query_instant": ("up",),
        "get_metric_metadata": ("up",),
    }.get(operation, ())
    await getattr(service, operation)(*args)
    assert request_threads
    assert all(thread != event_loop_thread for thread in request_threads)


async def test_health_endpoint_responds_while_metrics_request_is_pending(monkeypatch):
    started = threading.Event()
    released = threading.Event()

    def slow_get(*args, **kwargs):
        started.set()
        if not released.wait(timeout=2):
            raise requests.Timeout("offline blocked metric source")
        response = Mock(status_code=200)
        response.json.return_value = {"status": "success", "data": {"result": []}}
        return response

    monkeypatch.setattr(requests, "get", slow_get)
    service = PrometheusService()
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    task = asyncio.create_task(service.query_instant("up"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert not task.done()
    finally:
        released.set()
        await task


async def test_async_health_preserves_unavailable_result(monkeypatch):
    def unavailable(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", unavailable)
    assert await PrometheusService().health_check() is False


async def test_successful_prometheus_health_is_true(monkeypatch):
    monkeypatch.setattr(requests, "get", Mock(return_value=Mock(status_code=200)))
    assert await PrometheusService()._do_health_check() is True
