"""知识库传输及嵌入异常不能泄露正文、地址或凭据。"""

import logging
from types import SimpleNamespace

from langchain_core.documents import Document
import numpy as np
import pytest
import redis

from app.core.vector.redis_vector_store import EnhancedRedisVectorStore, QueryCache

MARKER = "private-body-credential-marker"


def store_for_test():
    store = object.__new__(EnhancedRedisVectorStore)
    store._closed = True
    store.collection_name = "fixture"
    store.stats = {"error_count": 0, "last_error": None}
    return store


async def test_redis_batch_error_logs_type_without_command_or_payload(caplog):
    class Pipeline:
        def watch(self, *args): return self
        def hget(self, *args): return None
        def multi(self): return self
        def hset(self, *args, **kwargs): return self
        def set(self, *args, **kwargs): return self
        def sadd(self, *args, **kwargs): return self
        def incr(self, *args, **kwargs): return self
        def execute(self):
            raise redis.ResponseError(f"HSET {MARKER} secret={MARKER}")

    store = store_for_test()
    store.client = SimpleNamespace(pipeline=Pipeline)
    with caplog.at_level(logging.DEBUG, logger="aiops.vector_store"):
        with pytest.raises(redis.ResponseError):
            await store._store_batch([Document(page_content=MARKER)], [np.array([1, 0])], 7)
    assert MARKER not in caplog.text
    assert "batch=7" in caplog.text
    assert "ResponseError" in caplog.text


async def test_embedding_failure_does_not_log_provider_error_or_text(caplog):
    store = store_for_test()
    def fail(_texts):
        raise RuntimeError(f"{MARKER} https://provider.invalid/{MARKER}")
    store.embedding_model = SimpleNamespace(embed_documents=fail)
    with caplog.at_level(logging.DEBUG, logger="aiops.vector_store"):
        assert await store._batch_embed([Document(page_content=MARKER)]) == []
    assert MARKER not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_redis_context_and_clear_response_do_not_expose_error(monkeypatch, caplog):
    store = store_for_test()
    store._closed = False
    store.pool = object()
    client = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(redis, "Redis", lambda **kwargs: client)
    with caplog.at_level(logging.DEBUG, logger="aiops.vector_store"):
        with pytest.raises(redis.ResponseError):
            async with store.get_redis_client():
                raise redis.ResponseError(MARKER)
        store._is_refresh_stage = True
        store._atomic_delete = lambda ids: (_ for _ in ()).throw(redis.ResponseError(MARKER))
        result = await store.clear()
    assert MARKER not in caplog.text
    assert MARKER not in str(store.stats)
    assert MARKER not in str(result)
    assert result["success"] is False
    store._closed = True


def test_query_cache_error_does_not_log_private_redis_response(caplog):
    def fail(_key):
        raise redis.ResponseError(MARKER)
    cache = QueryCache(SimpleNamespace(get=fail))
    with caplog.at_level(logging.DEBUG, logger="aiops.vector_store"):
        assert cache.get(MARKER) is None
    assert MARKER not in caplog.text

@pytest.mark.parametrize("failure", ["scan", "delete"])
async def test_query_cache_clear_propagates_redis_failure_without_sensitive_text(failure, caplog):
    from app.core.vector.redis_vector_store import CacheInvalidationError

    class CacheClient:
        def scan(self, *args, **kwargs):
            if failure == "scan":
                raise redis.ResponseError(MARKER)
            return 0, [b"cached-key"]
        def delete(self, *_keys):
            raise redis.ResponseError(MARKER)

    cache = QueryCache(CacheClient())
    cache._cache_stats["hits"] = 2
    with caplog.at_level(logging.DEBUG, logger="aiops.vector_store"):
        with pytest.raises(CacheInvalidationError, match="清理失败"):
            await cache.clear()
    assert MARKER not in caplog.text
    assert cache._cache_stats["hits"] == 2

@pytest.mark.parametrize("method", ["add_documents", "delete_documents", "clear"])
async def test_cleanup_failure_after_publication_preserves_published_status(method):
    from unittest.mock import AsyncMock

    from app.core.vector.redis_vector_store import RefreshPublishedCacheError

    store = store_for_test()
    stage = SimpleNamespace(
        _refresh_generation="fixture",
        add_documents=AsyncMock(return_value=["doc-id"]),
        delete_documents=AsyncMock(return_value=True),
    )
    store._clone_active_stage = AsyncMock(return_value=stage)
    store.create_refresh_stage = AsyncMock(return_value=stage)
    store.publish_refresh_stage = AsyncMock()
    store.discard_refresh_stage = AsyncMock(side_effect=RuntimeError(MARKER))
    store.query_cache = SimpleNamespace(invalidate_pattern=AsyncMock())
    args = {"add_documents": ([Document(page_content="new")],), "delete_documents": (["doc-id"],), "clear": ()}
    with pytest.raises(RefreshPublishedCacheError, match="已发布") as error:
        await getattr(store, method)(*args[method])
    assert MARKER not in str(error.value)
    store.publish_refresh_stage.assert_awaited_once_with(stage)
    store.query_cache.invalidate_pattern.assert_not_awaited()


async def test_cleanup_failure_does_not_replace_original_prepublication_error():
    from unittest.mock import AsyncMock

    store = store_for_test()
    stage = object()
    store.publish_refresh_stage = AsyncMock()
    store.discard_refresh_stage = AsyncMock(side_effect=RuntimeError(MARKER))
    async def fail(_stage):
        raise ValueError("safe original")
    with pytest.raises(ValueError, match="safe original"):
        await store._run_stage_operation(stage, fail)
    store.publish_refresh_stage.assert_not_awaited()


async def test_stage_connection_is_closed_even_if_lease_release_fails():
    from unittest.mock import AsyncMock

    store = store_for_test()
    lease = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError(MARKER)))
    stage = SimpleNamespace(
        collection_name="fixture-refresh-" + "a" * 32,
        _refresh_origin="fixture",
        _stage_lease=lease,
        _commit_started=True,
        close=AsyncMock(),
    )
    with pytest.raises(RuntimeError):
        await store.discard_refresh_stage(stage)
    stage.close.assert_awaited_once()
