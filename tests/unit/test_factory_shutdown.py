"""工厂关闭应等待初始化取消完成，防止清理后再次分配资源"""

import asyncio
from unittest.mock import AsyncMock

from fastapi import FastAPI
import pytest

from app.services.base import BaseService
from app.services.factory import ServiceFactory


class SlowService(BaseService):
    def __init__(self):
        super().__init__("slow")
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaned = False

    async def _do_initialize(self):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            self.cancelled.set()
            raise

    async def _do_health_check(self):
        return True

    async def cleanup(self):
        assert self.cancelled.is_set()
        self.cleaned = True
        await super().cleanup()


async def test_factory_cancels_initialization_before_cleaning_instances():
    class Factory(ServiceFactory):
        _instances = {}
        _locks = {}
        _initialization_tasks = {}
        _closing = False

    service = await Factory.get_service("slow", SlowService)
    await service.started.wait()
    try:
        result = await Factory.cleanup_all()
        assert service.cancelled.is_set()
        assert service.cleaned is True
        assert result["success"] is True
        assert not Factory._instances
    finally:
        service.release.set()
        await asyncio.sleep(0)


async def test_lifespan_cleans_factory_when_application_context_raises(monkeypatch):
    from app import main

    monkeypatch.setenv("AIOPS_PREWARM", "false")
    cleanup = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(main.ServiceFactory, "cleanup_all", cleanup)
    monkeypatch.setattr(main, "cleanup_resources", AsyncMock())
    with pytest.raises(RuntimeError, match="application failure"):
        async with main.lifespan(FastAPI()):
            raise RuntimeError("application failure")
    cleanup.assert_awaited_once()


async def test_lifespan_cleans_partial_startup_when_prewarm_fails(monkeypatch):
    from app import main

    monkeypatch.setenv("AIOPS_PREWARM", "true")
    monkeypatch.setattr(main, "_register_managed_services", AsyncMock())
    monkeypatch.setattr(main.startup_service, "warmup_services", AsyncMock(side_effect=RuntimeError("prewarm failure")))
    cleanup = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(main.ServiceFactory, "cleanup_all", cleanup)
    monkeypatch.setattr(main, "cleanup_resources", AsyncMock())
    with pytest.raises(RuntimeError, match="prewarm failure"):
        async with main.lifespan(FastAPI()):
            pass
    cleanup.assert_awaited_once()


async def test_factory_rejects_new_work_during_cleanup_and_can_restart():
    from app.common.exceptions import ServiceUnavailableError

    class Factory(ServiceFactory):
        _instances = {}
        _locks = {}
        _initialization_tasks = {}
        _closing = False
        _cleanup_lock = asyncio.Lock()

    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def cleanup():
        cleaning.set()
        await release.wait()

    service = AsyncMock()
    service.cleanup.side_effect = cleanup
    Factory._instances["existing"] = service
    task = asyncio.create_task(Factory.cleanup_all())
    try:
        await cleaning.wait()
        with pytest.raises(ServiceUnavailableError):
            await Factory.get_service("new", SlowService)
        with pytest.raises(ServiceUnavailableError):
            await Factory.get_service("existing", SlowService)
    finally:
        release.set()
        await task
    assert Factory._closing is False
    assert not Factory._instances
