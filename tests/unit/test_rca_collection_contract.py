from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import rca
from app.core.rca.events_collector import EventsCollector
from app.core.rca.logs_collector import LogsCollector
from app.core.rca.metrics_collector import MetricsCollector
from app.core.rca.rca_engine import RCAAnalysisEngine
from app.services.rca_service import RCAService

CANARY = 'redis://secret-password@private-host'


def setup_service(monkeypatch, metrics_failure=False, event_failure=False, logs_failure=False):
    prometheus = Mock()
    prometheus.health_check = AsyncMock(return_value=True)
    prometheus.query_range = AsyncMock(side_effect=RuntimeError(CANARY) if metrics_failure else None)
    k8s = Mock()
    k8s.health_check = AsyncMock(return_value=True)
    k8s.get_events = AsyncMock(side_effect=RuntimeError(CANARY) if event_failure else None, return_value=[])
    k8s.get_pods = AsyncMock(side_effect=RuntimeError(CANARY) if logs_failure else None, return_value=[])
    metric = MetricsCollector(prometheus_client=prometheus)
    metric.default_metrics = ['up']
    engine = RCAAnalysisEngine(metrics_collector=metric, events_collector=EventsCollector(k8s_client=k8s), logs_collector=LogsCollector(k8s_client=k8s))
    service = RCAService()
    service._engine = engine
    service._initialized = True
    monkeypatch.setattr(rca, 'get_rca_service', AsyncMock(return_value=service))
    app = FastAPI()
    app.include_router(rca.router, prefix='/rca')
    return TestClient(app), prometheus, k8s


@pytest.mark.parametrize('failures,status,coverage', [
    ((False, False, False), 'completed', 1.0),
    ((True, False, False), 'degraded', 2 / 3),
    ((True, True, True), 'failed', 0.0),
])
def test_actual_collectors_to_asgi_report_empty_partial_and_failed(monkeypatch, caplog, failures, status, coverage):
    client, _, _ = setup_service(monkeypatch, *failures)
    with caplog.at_level('DEBUG'):
        response = client.post('/rca/analyze', json={'namespace': 'test', 'time_window_hours': 1})
    assert response.status_code == 200, response.text
    result = response.json()['data']
    assert result['status'] == status
    assert result['collection']['coverage'] == pytest.approx(coverage)
    assert result['collection']['sources']['metrics']['status'] == ('failed' if failures[0] else 'empty')
    assert result['collection']['sources']['events']['status'] == ('failed' if failures[1] else 'empty')
    assert result['collection']['sources']['logs']['status'] == ('failed' if failures[2] else 'empty')
    assert CANARY not in response.text
    assert CANARY not in caplog.text


def test_partial_metric_queries_report_coverage(monkeypatch):
    client, prometheus, _ = setup_service(monkeypatch)
    async def query_range(**kwargs):
        if 'bad' in kwargs['query']:
            raise RuntimeError(CANARY)
        return None
    prometheus.query_range = AsyncMock(side_effect=query_range)
    response = client.post('/rca/analyze', json={'namespace': 'test', 'metrics': ['up', 'bad']})
    assert response.status_code == 200, response.text
    result = response.json()['data']
    assert result['status'] == 'degraded'
    assert result['collection']['sources']['metrics'] == {
        'status': 'partial', 'count': 0, 'succeeded': 1, 'failed': 1,
        'error_category': 'collection_failed',
    }
    assert result['collection']['coverage'] == pytest.approx(0.75)


def test_pod_container_partial_failure_is_not_empty_success(monkeypatch):
    client, _, k8s = setup_service(monkeypatch)
    k8s.get_pods.return_value = [{'metadata': {'name': 'pod'}}]
    k8s.get_pod = AsyncMock(return_value={'spec': {'containers': [{'name': 'ok'}, {'name': 'bad'}]}})
    async def get_pod_logs(**kwargs):
        if kwargs['container_name'] == 'bad':
            raise RuntimeError(CANARY)
        return ''
    k8s.get_pod_logs = AsyncMock(side_effect=get_pod_logs)
    response = client.post('/rca/analyze', json={'namespace': 'test'})
    assert response.status_code == 200, response.text
    result = response.json()['data']
    assert result['status'] == 'degraded'
    assert result['collection']['sources']['logs']['status'] == 'partial'
    assert result['collection']['sources']['logs']['succeeded'] == 1
    assert result['collection']['sources']['logs']['failed'] == 1
    assert CANARY not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('method,args', [
    ('get_pods', ('offline',)),
    ('get_events', ('offline',)),
    ('get_pod', ('offline', 'pod')),
    ('get_pod_logs', ('offline', 'pod')),
])
async def test_real_kubernetes_adapter_strict_mode_does_not_swallow_errors(method, args):
    from app.services.kubernetes import KubernetesService

    service = object.__new__(KubernetesService)
    service._ensure_initialized = Mock(return_value=True)
    service._sdk_call = AsyncMock(side_effect=RuntimeError(CANARY))
    service.core_v1 = Mock()
    # Preserve the default contract for callers that don't opt in.
    assert await getattr(service, method)(*args) in ([], None)
    with pytest.raises(RuntimeError):
        await getattr(service, method)(*args, raise_errors=True)


def test_invalid_old_cache_is_ignored_and_failed_analysis_is_not_cached(monkeypatch):
    client, prometheus, _ = setup_service(monkeypatch, True, True, True)
    service = rca.get_rca_service.return_value
    service._cache_manager = Mock()
    service._get_from_cache = AsyncMock(return_value={'success': True, 'root_causes': []})
    service._save_to_cache = AsyncMock()
    response = client.post('/rca/analyze', json={'namespace': 'test'})
    assert response.json()['data']['status'] == 'failed'
    prometheus.query_range.assert_awaited()
    service._save_to_cache.assert_not_awaited()


@pytest.mark.parametrize('failures,status', [
    ((False, False, False), 'completed'),
    ((True, False, False), 'degraded'),
    ((True, True, True), 'failed'),
])
def test_quick_diagnosis_asgi_preserves_collection_status(monkeypatch, failures, status):
    client, _, _ = setup_service(monkeypatch, *failures)
    response = client.post('/rca/quick-diagnosis', json={'namespace': 'test'})
    assert response.status_code == 200, response.text
    result = response.json()['data']
    assert result['status'] == status
    assert result['collection']['status'] == {'completed': 'complete', 'degraded': 'partial', 'failed': 'failed'}[status]
    assert CANARY not in response.text


def test_malformed_event_reports_partial(monkeypatch):
    client, _, k8s = setup_service(monkeypatch)
    k8s.get_events.return_value = [None, {'type': 'Normal'}]
    response = client.post('/rca/analyze', json={'namespace': 'test'})
    assert response.status_code == 200, response.text
    source = response.json()['data']['collection']['sources']['events']
    assert source['status'] == 'partial'
    assert source['failed'] == 1
    assert source['error_category'] == 'collection_failed'
    assert source['succeeded'] == 1


def test_event_limit_is_reported_as_incomplete(monkeypatch):
    client, _, k8s = setup_service(monkeypatch)
    service = rca.get_rca_service.return_value
    service._engine.events_collector.max_events_limit = 1
    k8s.get_events.return_value = [{'type': 'Normal'}]
    response = client.post('/rca/analyze', json={'namespace': 'test'})
    source = response.json()['data']['collection']['sources']['events']
    assert source['status'] == 'partial'
    assert source['failed'] == 1
    assert source['error_category'] == 'truncated'
    assert response.json()['data']['collection']['coverage'] is None


def test_log_tail_limit_reports_unknown_coverage(monkeypatch):
    client, _, k8s = setup_service(monkeypatch)
    service = rca.get_rca_service.return_value
    service._engine.logs_collector.error_lines = 1
    k8s.get_pods.return_value = [{'metadata': {'name': 'pod'}}]
    k8s.get_pod = AsyncMock(return_value={'spec': {'containers': [{'name': 'one'}]}})
    k8s.get_pod_logs = AsyncMock(return_value='2026-10-29T10:00:00Z INFO fixture')
    response = client.post('/rca/analyze', json={'namespace': 'test'})
    assert response.status_code == 200, response.text
    collection = response.json()['data']['collection']
    assert collection['status'] == 'partial'
    assert collection['sources']['logs']['error_category'] == 'truncated'
    assert collection['coverage'] is None
