import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from app.api.routes import rca
from app.core.rca.recovering_client import RecoveringClient, RecoveryUnavailable
from app.services.rca_service import RCAService

CANARY = 'secret-password@private-host'


@pytest.mark.asyncio
async def test_real_asgi_recovers_initially_unavailable_sources_without_restart(monkeypatch, caplog):
    import app.core.cache.redis_cache_manager as cache
    import app.services.kubernetes as kubernetes
    import app.services.llm as llm
    import app.services.prometheus as prometheus

    states = {'prometheus': False, 'kubernetes': False}
    factories = {'prometheus': 0, 'kubernetes': 0}

    def new_prometheus():
        factories['prometheus'] += 1
        async def initialize():
            if not states['prometheus']:
                raise RuntimeError(CANARY)
        return SimpleNamespace(
            initialize=AsyncMock(side_effect=initialize),
            health_check=AsyncMock(side_effect=lambda: states['prometheus']),
            query_range=AsyncMock(return_value=None),
        )

    def new_kubernetes():
        factories['kubernetes'] += 1
        return SimpleNamespace(
            health_check=AsyncMock(side_effect=lambda: states['kubernetes']),
            get_events=AsyncMock(return_value=[]),
            get_pods=AsyncMock(return_value=[]),
        )

    def no_cache(*args, **kwargs):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(prometheus, 'PrometheusService', new_prometheus)
    monkeypatch.setattr(kubernetes, 'KubernetesService', new_kubernetes)
    monkeypatch.setattr(cache, 'RedisCacheManager', no_cache)
    monkeypatch.setattr(llm, 'LLMService', lambda: SimpleNamespace(generate_response=AsyncMock(return_value='')))
    service = RCAService()
    await service.initialize()
    for source in service._recovery_clients:
        source._retry_interval = 0
    monkeypatch.setattr(rca, 'get_rca_service', AsyncMock(return_value=service))
    app = FastAPI()
    app.include_router(rca.router, prefix='/rca')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        with caplog.at_level('DEBUG'):
            first = await client.post('/rca/analyze', json={'namespace': 'test', 'metrics': ['up']})
            assert first.status_code == 200
            assert first.json()['data']['status'] == 'failed'
            states['prometheus'] = True
            second = await client.post('/rca/analyze', json={'namespace': 'test', 'metrics': ['up']})
            assert second.json()['data']['status'] == 'degraded'
            states['kubernetes'] = True
            third = await client.post('/rca/analyze', json={'namespace': 'test', 'metrics': ['up']})
            assert third.json()['data']['status'] == 'completed'
            assert third.json()['data']['collection']['sources']['logs']['status'] == 'empty'
    assert factories == {'prometheus': 2, 'kubernetes': 3}
    assert CANARY not in caplog.text
    assert CANARY not in first.text + second.text + third.text
    await service.cleanup()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_connection_or_duplicate_attempts():
    started = asyncio.Event()
    release = asyncio.Event()
    client = SimpleNamespace(query_range=AsyncMock(return_value=None))
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return client

    source = RecoveringClient(factory, retry_interval=0, wait_timeout=1)
    first = asyncio.create_task(source.query_range())
    await started.wait()
    second = asyncio.create_task(source.query_range())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second is None
    assert calls == 1
    await source.close()


@pytest.mark.asyncio
async def test_timed_out_waiters_do_not_start_overlapping_connection():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    client = SimpleNamespace(query_range=AsyncMock(return_value=None))

    async def factory():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return client

    source = RecoveringClient(factory, retry_interval=0, wait_timeout=0.01)
    pending = asyncio.create_task(source.query_range())
    await started.wait()
    with pytest.raises(RecoveryUnavailable):
        await pending
    with pytest.raises(RecoveryUnavailable):
        await source.query_range()
    assert calls == 1
    release.set()
    await source._attempt
    assert await source.query_range() is None
    await source.close()


@pytest.mark.asyncio
async def test_failed_connect_obeys_cooldown_and_closes_on_shutdown():
    factory = AsyncMock(side_effect=RuntimeError(CANARY))
    source = RecoveringClient(factory, retry_interval=60)
    for _ in range(3):
        with pytest.raises(RecoveryUnavailable):
            await source.query_range()
    assert factory.await_count == 1
    await source.close()
    with pytest.raises(RecoveryUnavailable):
        await source.query_range()


@pytest.mark.asyncio
async def test_cleanup_cancels_pending_connection_and_prevents_late_reuse():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def factory():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    source = RecoveringClient(factory, wait_timeout=10)
    waiter = asyncio.create_task(source.query_range())
    await started.wait()
    await source.close()
    assert cancelled.is_set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    with pytest.raises(RecoveryUnavailable):
        await source.query_range()


def test_prometheus_constructor_does_not_log_url_credentials(monkeypatch, caplog):
    from app.services import prometheus

    monkeypatch.setattr(prometheus, 'config', SimpleNamespace(
        prometheus=SimpleNamespace(url='http://' + CANARY, timeout=1),
    ))
    with caplog.at_level('DEBUG'):
        prometheus.PrometheusService()
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_prometheus_health_failure_does_not_log_error_details(monkeypatch, caplog):
    from app.services import prometheus

    def fail(*args, **kwargs):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(prometheus.requests, 'get', fail)
    with caplog.at_level('DEBUG'):
        assert await prometheus.PrometheusService().health_check() is False
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_real_prometheus_construct_initialization_and_recovery_through_asgi(monkeypatch, caplog):
    import requests

    import app.core.cache.redis_cache_manager as cache
    import app.services.kubernetes as kubernetes
    import app.services.llm as llm
    import app.services.prometheus as prometheus

    online = False
    requests_made = []

    def mock_get(url, **kwargs):
        requests_made.append(url)
        if not online:
            raise requests.ConnectionError(CANARY)
        response = Mock(status_code=200)
        response.raise_for_status = Mock()
        response.json.return_value = {'status': 'success', 'data': {'result': []}}
        return response

    monkeypatch.setattr(prometheus.requests, 'get', mock_get)
    monkeypatch.setattr(prometheus, 'config', SimpleNamespace(
        prometheus=SimpleNamespace(url='http://fixture', timeout=1),
    ))
    monkeypatch.setattr(kubernetes, 'KubernetesService', lambda: SimpleNamespace(health_check=AsyncMock(return_value=False)))
    monkeypatch.setattr(cache, 'RedisCacheManager', lambda **kwargs: (_ for _ in ()).throw(RuntimeError(CANARY)))
    monkeypatch.setattr(llm, 'LLMService', lambda: SimpleNamespace(generate_response=AsyncMock(return_value='')))
    service = RCAService()
    await service.initialize()
    for source in service._recovery_clients:
        source._retry_interval = 0
    monkeypatch.setattr(rca, 'get_rca_service', AsyncMock(return_value=service))
    app = FastAPI()
    app.include_router(rca.router, prefix='/rca')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        with caplog.at_level('DEBUG'):
            first = await client.post('/rca/analyze', json={'namespace': 'test', 'metrics': ['up']})
            assert first.json()['data']['status'] == 'failed'
            online = True
            second = await client.post('/rca/analyze', json={'namespace': 'test', 'metrics': ['up']})
            assert second.json()['data']['status'] == 'degraded'
            assert second.json()['data']['collection']['sources']['metrics']['status'] == 'empty'
    connected = service._recovery_clients[0]._client
    assert isinstance(connected, prometheus.PrometheusService)
    assert connected.is_initialized() is True
    assert connected._health_status is True
    assert len([url for url in requests_made if url.endswith('/api/v1/query')]) == 2
    assert len([url for url in requests_made if url.endswith('/api/v1/query_range')]) == 1
    assert CANARY not in first.text + second.text + caplog.text
    await service.cleanup()
