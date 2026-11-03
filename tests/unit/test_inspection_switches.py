"""巡检开关、内部周期任务生命周期与真实ASGI拒绝合同，全部使用采集替身。"""

import asyncio
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from app.api.middleware.error_handler import setup_error_handlers
from app.api.routes import inspection
from app.common.exceptions import FeatureDisabledError
from app.config.settings import config
from app.models.inspection_models import InspectionRunRequest
from app.services.factory import ServiceFactory
from app.services.inspection_service import InspectionService

COLLECTORS = ("pods", "events", "nodes", "services", "endpoints", "resource_quotas", "pvcs", "prometheus")


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(config.inspection, "enabled", True)
    monkeypatch.setattr(config.inspection, "scheduler_enabled", False)
    svc = InspectionService()
    monkeypatch.setattr(svc, "_do_initialize", AsyncMock())
    for name in COLLECTORS:
        result = {"queries": [], "series": []} if name == "prometheus" else []
        monkeypatch.setattr(svc, f"_collect_{name}", AsyncMock(return_value=result))
    return svc


def assert_no_collection(service):
    for name in COLLECTORS:
        getattr(service, f"_collect_{name}").assert_not_awaited()


async def wait_for_task(service, task_id):
    for _ in range(20):
        status = await service.get_task_status(task_id)
        if status["status"] != "running":
            return status
        await asyncio.sleep(0)
    pytest.fail("巡检测试任务没有完成")


def api_app(service, monkeypatch):
    monkeypatch.setattr(inspection, "inspection_service", service)
    app = FastAPI()
    app.include_router(inspection.router, prefix="/inspection")
    setup_error_handlers(app)
    return app


@pytest.mark.parametrize("operation", ["initialize", "run_inspection", "run_inspection_async", "start_scheduler"])
async def test_disabled_inspection_rejects_all_new_work_without_initialization(service, monkeypatch, operation):
    monkeypatch.setattr(config.inspection, "enabled", False)
    with pytest.raises(FeatureDisabledError) as raised:
        method = getattr(service, operation)
        await method(*([InspectionRunRequest()] if operation.startswith("run_") else []))
    assert raised.value.status_code == 403
    assert raised.value.error_code == "INSPECTION_FEATURE_DISABLED"
    assert raised.value.details == {"switch": "inspection.enabled"}
    service._do_initialize.assert_not_awaited()
    assert_no_collection(service)
    assert not service._tasks
    assert service._scheduler_task is None


@pytest.mark.parametrize("async_task", [False, True])
async def test_manual_inspection_works_while_internal_scheduler_disabled(service, async_task):
    await service.initialize()
    request = InspectionRunRequest()
    if async_task:
        submitted = await service.run_inspection_async(request)
        task = await wait_for_task(service, submitted["task_id"])
        assert task["status"] == "completed"
        result = await service.get_report(task["report_id"])
    else:
        result = await service.run_inspection(request)
    assert result["report_id"]
    service._collect_pods.assert_awaited_once()
    assert service._scheduler_task is None


async def test_queued_async_inspection_checks_enabled_before_collection(service, monkeypatch):
    await service.initialize()
    result = await service.run_inspection_async(InspectionRunRequest())
    monkeypatch.setattr(config.inspection, "enabled", False)
    status = await wait_for_task(service, result["task_id"])
    assert status["status"] == "failed"
    assert status["report_id"] is None
    assert_no_collection(service)
    assert not service._reports


async def test_disabling_during_collection_does_not_publish_a_success_report(service, monkeypatch):
    await service.initialize()
    started = asyncio.Event()
    release = asyncio.Event()

    async def pods(**kwargs):
        started.set()
        await release.wait()
        return []

    monkeypatch.setattr(service, "_collect_pods", pods)
    running = asyncio.create_task(service.run_inspection(InspectionRunRequest()))
    await started.wait()
    monkeypatch.setattr(config.inspection, "enabled", False)
    release.set()
    with pytest.raises(FeatureDisabledError):
        await running
    assert not service._reports


@pytest.mark.parametrize("async_task", [False, True])
async def test_disabled_run_api_is_403_without_initialization_or_task_creation(service, monkeypatch, async_task):
    monkeypatch.setattr(config.inspection, "enabled", False)
    async with AsyncClient(transport=ASGITransport(app=api_app(service, monkeypatch)), base_url="http://test") as client:
        response = await client.post("/inspection/run", json={"async": async_task})
    assert response.status_code == 403
    assert response.json()["data"]["error_code"] == "INSPECTION_FEATURE_DISABLED"
    assert response.json()["data"]["details"] == {"switch": "inspection.enabled"}
    service._do_initialize.assert_not_awaited()
    assert_no_collection(service)
    assert not service._tasks


async def test_disabled_inspection_keeps_report_and_status_reads(service, monkeypatch):
    await service.initialize()
    report = await service.run_inspection(InspectionRunRequest())
    for name in COLLECTORS:
        getattr(service, f"_collect_{name}").reset_mock()
    service._do_initialize.reset_mock()
    monkeypatch.setattr(config.inspection, "enabled", False)
    async with AsyncClient(transport=ASGITransport(app=api_app(service, monkeypatch)), base_url="http://test") as client:
        paths = ["/history", f"/report/{report['report_id']}", f"/report/{report['report_id']}/markdown", "/tasks/unknown", "/rules", "/profiles", "/config", "/scheduler/status"]
        for path in paths:
            response = await client.get(f"/inspection{path}")
            assert response.status_code == 200
            assert response.json()["code"] == 0
        history = (await client.get("/inspection/history")).json()["data"]
    assert history["items"][0]["report_id"] == report["report_id"]
    service._do_initialize.assert_not_awaited()
    assert_no_collection(service)


async def test_inspection_route_factory_does_not_start_background_initialization(monkeypatch):
    monkeypatch.setattr(inspection, "inspection_service", None)
    monkeypatch.setattr(ServiceFactory, "_instances", {})
    monkeypatch.setattr(ServiceFactory, "_locks", {})
    monkeypatch.setattr(ServiceFactory, "_initialization_tasks", {})
    monkeypatch.setattr(ServiceFactory, "_closing", False)
    initialize = AsyncMock()
    monkeypatch.setattr(InspectionService, "initialize", initialize)
    svc = await inspection.get_inspection_service()
    assert svc is await inspection.get_inspection_service()
    initialize.assert_not_awaited()
    assert not ServiceFactory._initialization_tasks


async def test_scheduler_disabled_start_is_403_without_initialization(service, monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=api_app(service, monkeypatch)), base_url="http://test") as client:
        response = await client.post("/inspection/scheduler/start")
    assert response.status_code == 403
    assert response.json()["data"]["details"] == {"switch": "inspection.scheduler.enabled"}
    service._do_initialize.assert_not_awaited()
    assert_no_collection(service)
    assert service._scheduler_task is None


async def test_enabled_scheduler_api_starts_and_disabled_status_cleans_worker(service, monkeypatch):
    monkeypatch.setattr(config.inspection, "scheduler_enabled", True)
    try:
        async with AsyncClient(transport=ASGITransport(app=api_app(service, monkeypatch)), base_url="http://test") as client:
            response = await client.post("/inspection/scheduler/start")
            assert response.status_code == 200
            assert response.json()["data"]["running"] is True
            worker = service._scheduler_task
            monkeypatch.setattr(config.inspection, "scheduler_enabled", False)
            response = await client.get("/inspection/scheduler/status")
            assert response.status_code == 200
            assert response.json()["data"] == {"running": False}
            assert worker.done()
            assert service._scheduler_task is None
    finally:
        await service.cleanup()


async def test_scheduler_start_is_concurrently_idempotent_and_stop_waits(service, monkeypatch):
    monkeypatch.setattr(config.inspection, "scheduler_enabled", True)
    try:
        results = await asyncio.gather(*(service.start_scheduler() for _ in range(8)))
        worker = service._scheduler_task
        assert worker is not None
        assert all(result["running"] for result in results)
        service._do_initialize.assert_awaited_once()
        await service.stop_scheduler()
        assert worker.done()
        assert service._scheduler_task is None
        assert await service.scheduler_status() == {"running": False}
        assert await service.stop_scheduler() == {"running": False}
    finally:
        await service.cleanup()


@pytest.mark.parametrize("switch", ["enabled", "scheduler_enabled"])
async def test_status_stops_and_waits_for_disabled_sleeping_scheduler(service, monkeypatch, switch):
    monkeypatch.setattr(config.inspection, "scheduler_enabled", True)
    ran = asyncio.Event()

    async def run(_request):
        ran.set()
        return {}

    monkeypatch.setattr(service, "run_inspection", run)
    await service.start_scheduler()
    worker = service._scheduler_task
    try:
        await ran.wait()
        monkeypatch.setattr(config.inspection, switch, False)
        assert await service.scheduler_status() == {"running": False}
        assert worker.done()
        assert service._scheduler_task is None
        monkeypatch.setattr(config.inspection, switch, True)
        assert (await service.start_scheduler())["running"] is True
        assert service._scheduler_task is not worker
        service._do_initialize.assert_awaited_once()
    finally:
        await service.cleanup()


async def test_disabling_while_scheduler_initializes_does_not_register_worker(service, monkeypatch):
    monkeypatch.setattr(config.inspection, "scheduler_enabled", True)
    started = asyncio.Event()
    release = asyncio.Event()

    async def initialize():
        started.set()
        await release.wait()

    monkeypatch.setattr(service, "_do_initialize", initialize)
    starting = asyncio.create_task(service.start_scheduler())
    await started.wait()
    monkeypatch.setattr(config.inspection, "scheduler_enabled", False)
    release.set()
    with pytest.raises(FeatureDisabledError):
        await starting
    assert service._scheduler_task is None
    assert service._scheduler_running is False
    assert_no_collection(service)


@pytest.mark.parametrize("switch", ["enabled", "scheduler_enabled"])
async def test_disabling_before_first_tick_prevents_collection(service, monkeypatch, switch):
    monkeypatch.setattr(config.inspection, "scheduler_enabled", True)
    await service.start_scheduler()
    worker = service._scheduler_task
    monkeypatch.setattr(config.inspection, switch, False)
    await worker
    assert_no_collection(service)
    assert service._scheduler_task is None
    assert service._scheduler_running is False


@pytest.mark.parametrize("switch", ["enabled", "scheduler_enabled"])
async def test_disabling_during_tick_terminates_worker_after_return(service, monkeypatch, switch):
    monkeypatch.setattr(config.inspection, "scheduler_enabled", True)

    async def run(_request):
        monkeypatch.setattr(config.inspection, switch, False)
        return {}

    monkeypatch.setattr(service, "run_inspection", run)
    await service.start_scheduler()
    worker = service._scheduler_task
    await worker
    assert service._scheduler_task is None
    assert service._scheduler_running is False


async def test_disabled_start_cleans_existing_worker_before_rejecting(service, monkeypatch):
    monkeypatch.setattr(config.inspection, "scheduler_enabled", True)
    await service.start_scheduler()
    worker = service._scheduler_task
    monkeypatch.setattr(config.inspection, "scheduler_enabled", False)
    with pytest.raises(FeatureDisabledError):
        await service.start_scheduler()
    assert worker.done()
    assert service._scheduler_task is None


async def test_cleanup_waits_for_running_tick_and_restart_has_new_owner(service, monkeypatch):
    monkeypatch.setattr(config.inspection, "scheduler_enabled", True)
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def run(_request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    monkeypatch.setattr(service, "run_inspection", run)
    await service.start_scheduler()
    old_worker = service._scheduler_task
    try:
        await entered.wait()
        stopped, started = await asyncio.gather(service.stop_scheduler(), service.start_scheduler())
        assert stopped == {"running": False}
        assert started["running"] is True
        assert cleaned.is_set()
        assert old_worker.done()
        assert service._scheduler_task is not old_worker
        assert service._scheduler_task is not None
        assert service._scheduler_running is True
    finally:
        worker = service._scheduler_task
        await service.cleanup()
        assert worker.done()
        assert service._scheduler_task is None
        assert service.is_initialized() is False


async def test_worker_cancelled_before_first_tick_can_be_restarted(service, monkeypatch):
    monkeypatch.setattr(config.inspection, "scheduler_enabled", True)
    await service.start_scheduler()
    old_worker = service._scheduler_task
    old_worker.cancel()
    await asyncio.gather(old_worker, return_exceptions=True)
    try:
        assert (await service.start_scheduler())["running"] is True
        assert service._scheduler_task is not old_worker
    finally:
        await service.cleanup()
