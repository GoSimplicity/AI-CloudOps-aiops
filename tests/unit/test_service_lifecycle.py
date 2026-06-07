#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio

import pytest

from app.core.rca.base_collector import BaseDataCollector
from app.services.base import BaseService


class CountingService(BaseService):
    def __init__(self) -> None:
        super().__init__("counting")
        self.initialize_calls = 0

    async def _do_initialize(self) -> None:
        self.initialize_calls += 1
        await asyncio.sleep(0)

    async def _do_health_check(self) -> bool:
        return True


class CountingCollector(BaseDataCollector):
    def __init__(self) -> None:
        super().__init__("counting")
        self.initialize_calls = 0

    async def _do_initialize(self) -> None:
        self.initialize_calls += 1
        await asyncio.sleep(0)

    async def collect(self, namespace, start_time, end_time, **kwargs):
        return []

    async def health_check(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_base_service_initialize_is_idempotent() -> None:
    service = CountingService()

    await service.initialize()
    await service.initialize()

    assert service.is_initialized() is True
    assert service.initialize_calls == 1


@pytest.mark.asyncio
async def test_base_service_concurrent_initialize_runs_once() -> None:
    service = CountingService()

    await asyncio.gather(*(service.initialize() for _ in range(5)))

    assert service.is_initialized() is True
    assert service.initialize_calls == 1


@pytest.mark.asyncio
async def test_data_collector_initialize_is_idempotent() -> None:
    collector = CountingCollector()

    await collector.initialize()
    await collector.initialize()

    assert collector.is_initialized() is True
    assert collector.initialize_calls == 1


@pytest.mark.asyncio
async def test_data_collector_concurrent_initialize_runs_once() -> None:
    collector = CountingCollector()

    await asyncio.gather(*(collector.initialize() for _ in range(5)))

    assert collector.is_initialized() is True
    assert collector.initialize_calls == 1
