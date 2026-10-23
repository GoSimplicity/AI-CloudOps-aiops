"""升级后的余弦相似度契约不能复用旧缓存，也不清理其他集合"""

import fnmatch
import hashlib
import pickle
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


from app.core.agents.enterprise_assistant import RAGAssistant
from app.core.vector import redis_vector_store


class MemoryRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.values[key] = value

    def scan(self, cursor, match, count):
        return 0, [key for key in self.values if fnmatch.fnmatch(key, match)]

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)


def make_store(monkeypatch, client, name):
    monkeypatch.setattr(redis_vector_store.redis, "ConnectionPool", Mock())
    monkeypatch.setattr(redis_vector_store.redis, "Redis", Mock(return_value=client))
    monkeypatch.setattr(redis_vector_store.EnhancedRedisVectorStore, "_initialize", lambda self: None)
    store = redis_vector_store.EnhancedRedisVectorStore({}, name, SimpleNamespace(), vector_dim=2)
    store._closed = True
    return store


def test_vector_cache_never_hits_legacy_distance_results(monkeypatch):
    client = MemoryRedis()
    client.values["query_cache:question"] = pickle.dumps(["legacy-l2-result"])
    store = make_store(monkeypatch, client, "collection-a")
    assert store.query_cache.get("question") is None
    assert "query_cache:question" in client.values


async def test_cache_clear_preserves_other_collections_and_legacy_keys(monkeypatch):
    client = MemoryRedis()
    first = make_store(monkeypatch, client, "collection-a")
    second = make_store(monkeypatch, client, "collection-b")
    client.values["query_cache:question"] = pickle.dumps(["legacy"])
    first.query_cache.set("question", ["first"])
    second.query_cache.set("question", ["second"])
    assert first.query_cache.get("question") == ["first"]
    assert second.query_cache.get("question") == ["second"]
    await first.query_cache.clear()
    assert first.query_cache.get("question") is None
    assert second.query_cache.get("question") == ["second"]
    assert "query_cache:question" in client.values


async def test_rag_answer_cache_does_not_return_preupgrade_answer():
    old_key = hashlib.md5("question".encode()).hexdigest()
    cache = SimpleNamespace(get=Mock(side_effect=lambda key: {"answer": "old-l2-answer"} if key == old_key else None), set=Mock())
    rag = RAGAssistant(SimpleNamespace(collection_name="offline"), None, cache_manager=cache)
    rag.graph = SimpleNamespace(ainvoke=AsyncMock(return_value={"answer": "new-cosine-answer", "confidence": 0.1, "sources": []}))
    result = await rag.get_answer("question")
    assert result["answer"] == "new-cosine-answer"
    assert result["cache_hit"] is False
    rag.graph.ainvoke.assert_awaited_once()
