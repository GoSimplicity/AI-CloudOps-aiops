"""Per-dependency on-demand connection with bounded, shared recovery attempts."""

import asyncio
from collections.abc import Awaitable, Callable
import logging
import time
from typing import Any


class RecoveryUnavailable(RuntimeError):
    """The source is unavailable without exposing its connection details."""


class RecoveringClient:
    def __init__(
        self,
        factory: Callable[[], Awaitable[Any]],
        *,
        retry_interval: float = 5.0,
        wait_timeout: float = 5.0,
    ) -> None:
        self._factory = factory
        self._retry_interval = retry_interval
        self._wait_timeout = wait_timeout
        self._client: Any = None
        self._attempt: asyncio.Task | None = None
        self._next_retry = 0.0
        self._closed = False
        self._logger = logging.getLogger("aiops.rca.recovery")

    async def _connect(self) -> None:
        try:
            client = await self._factory()
            if not self._closed:
                self._client = client
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._logger.warning("RCA数据源连接失败: %s", type(error).__name__)
        finally:
            self._next_retry = time.monotonic() + self._retry_interval

    async def _get_client(self) -> Any:
        if self._closed:
            raise RecoveryUnavailable("数据源不可用")
        if self._client is not None:
            return self._client
        if self._attempt is None or self._attempt.done():
            if time.monotonic() < self._next_retry:
                raise RecoveryUnavailable("数据源不可用")
            # No await occurs between checking and assigning, so all waiters on
            # this event loop share the same attempt.
            self._attempt = asyncio.create_task(self._connect())
        try:
            await asyncio.wait_for(asyncio.shield(self._attempt), self._wait_timeout)
        except asyncio.TimeoutError:
            raise RecoveryUnavailable("数据源连接超时") from None
        if self._client is None or self._closed:
            raise RecoveryUnavailable("数据源不可用")
        return self._client

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            return bool(await client.health_check())
        except RecoveryUnavailable:
            return False

    async def query_range(self, *args, **kwargs):
        return await (await self._get_client()).query_range(*args, **kwargs)

    async def query_instant(self, *args, **kwargs):
        return await (await self._get_client()).query_instant(*args, **kwargs)

    async def get_available_metrics(self, *args, **kwargs):
        return await (await self._get_client()).get_available_metrics(*args, **kwargs)

    async def get_metric_metadata(self, *args, **kwargs):
        return await (await self._get_client()).get_metric_metadata(*args, **kwargs)

    async def get_pods(self, *args, **kwargs):
        return await (await self._get_client()).get_pods(*args, **kwargs)

    async def get_events(self, *args, **kwargs):
        return await (await self._get_client()).get_events(*args, **kwargs)

    async def get_pod(self, *args, **kwargs):
        return await (await self._get_client()).get_pod(*args, **kwargs)

    async def get_pod_logs(self, *args, **kwargs):
        return await (await self._get_client()).get_pod_logs(*args, **kwargs)

    async def close(self) -> None:
        self._closed = True
        if self._attempt is not None and not self._attempt.done():
            self._attempt.cancel()
            await asyncio.gather(self._attempt, return_exceptions=True)
        self._client = None
