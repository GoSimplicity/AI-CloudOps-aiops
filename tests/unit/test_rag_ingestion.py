"""知识写入必须可检索，索引失败不得报告成功"""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import UploadFile
from langchain_core.documents import Document
import numpy as np
import pytest

from app.common.exceptions import AssistantError
from app.config.settings import config
from app.core.agents.enterprise_assistant import DocumentRetriever, RAGAssistant, RetrievalStrategy
from app.core.vector.redis_vector_store import IndexManager
from app.services.assistant_service import OptimizedAssistantService


class MemoryIndexClient:
    def __init__(self):
        self.values = {}
        self.operations = []

    def hget(self, key, field):
        return self.values.get((key, field))

    def pipeline(self):
        self.operations = []
        return self

    def set(self, key, value):
        self.operations.append(("set", key, value))
        return self

    def get(self, key):
        if key.endswith(":active_generation") or key.endswith(":revision"):
            return self.values.get(key)
        self.operations.append(("get", key, None))
        return self

    def execute(self):
        results = []
        for kind, key, value in self.operations:
            if kind == "set":
                self.values[key] = value
                results.append(True)
            else:
                results.append(self.values.get(key))
        return results


async def test_hnsw_scores_identical_vector_above_unrelated_vector():
    manager = IndexManager(MemoryIndexClient(), "offline")
    manager.create_index(2, "HNSW")
    await manager.update_index(["relevant", "unrelated"], [np.array([1, 0]), np.array([0, 1])])
    found = await manager.search_vectors(np.array([1, 0], dtype=np.float32), 2)
    assert found[0][0] == "relevant"
    assert found[0][1] == pytest.approx(1.0, abs=1e-5)
    assert found[0][1] > found[1][1]


@pytest.mark.parametrize("method", ["add_document", "upload_knowledge_file"])
@pytest.mark.parametrize("outcome", [{"success": False}, RuntimeError("offline embedding failure")])
async def test_failed_index_write_never_reports_upload_success(monkeypatch, tmp_path, method, outcome):
    monkeypatch.setattr(config.rag, "knowledge_base_path", str(tmp_path))
    service = OptimizedAssistantService()
    service._ensure_ready = AsyncMock()
    upload = AsyncMock(side_effect=outcome if isinstance(outcome, Exception) else None, return_value=outcome)
    service._assistant = SimpleNamespace(upload_knowledge=upload)
    payload = {"title": "new-doc", "content": "恢复码 ORBIT-7426", "file_name": "guide.md"}
    args = payload if method == "add_document" else UploadFile(BytesIO(b"test"), filename="guide.md")
    with pytest.raises(AssistantError):
        await getattr(service, method)(args)


async def test_upload_invalidates_retriever_cache_before_next_query():
    corpus = [Document(page_content="old-document")]
    store = SimpleNamespace()

    async def search(**kwargs):
        return [(doc, 1.0) for doc in corpus]

    async def add_documents(documents):
        corpus.extend(documents)
        return ["new-id"]

    store.similarity_search = search
    store.add_documents = add_documents
    rag = object.__new__(RAGAssistant)
    rag.vector_store = store
    rag.retriever = DocumentRetriever(store, RetrievalStrategy())
    rag.reranker = SimpleNamespace(_rerank_cache={})
    rag.cache_manager = None
    rag._stats = {}
    initial = await rag.retriever.retrieve(["release-e2e"])
    assert len(initial) == 1
    result = await rag.upload_knowledge({"content": "release-e2e 恢复码 ORBIT-7426"})
    assert result["success"] is True
    found = await rag.retriever.retrieve(["release-e2e"])
    assert any("ORBIT-7426" in doc.page_content for doc in found)


async def test_empty_vector_ids_are_an_ingestion_failure():
    rag = object.__new__(RAGAssistant)
    rag.vector_store = SimpleNamespace(add_documents=AsyncMock(return_value=[]))
    result = await rag.upload_knowledge({"content": "new content"})
    assert result["success"] is False


def test_add_document_api_reports_index_failure(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import assistant

    monkeypatch.setattr(config.rag, "knowledge_base_path", str(tmp_path))
    service = OptimizedAssistantService()
    service._initialized = True
    service._assistant = SimpleNamespace(upload_knowledge=AsyncMock(return_value={"success": False}))
    monkeypatch.setattr(assistant, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    app.include_router(assistant.router, prefix="/assistant")
    result = TestClient(app).post("/assistant/add-document", json={"title": "test", "content": "new content", "file_name": "test.md"})
    assert result.status_code == 502
    assert (tmp_path / "test.md").exists()


async def test_partial_store_does_not_update_search_index():
    from app.core.vector.redis_vector_store import EnhancedRedisVectorStore

    store = object.__new__(EnhancedRedisVectorStore)
    store._closed = True
    store._is_refresh_stage = True
    docs = [Document(page_content="one"), Document(page_content="two")]
    store._preprocess_documents = AsyncMock(return_value=docs)
    store._batch_embed = AsyncMock(return_value=[np.array([1, 0]), np.array([0, 1])])
    store._parallel_store = AsyncMock(return_value=["only-one-id"])
    store.index_manager = SimpleNamespace(update_index=AsyncMock())
    with pytest.raises(ValueError):
        await store.add_documents(docs)
    store.index_manager.update_index.assert_not_awaited()


async def test_cluster_statistics_remain_cumulative_and_reset_removes_documents():
    from app.core.retrieval.hierarchical_retriever import HierarchicalRetriever

    retriever = HierarchicalRetriever(SimpleNamespace())
    await retriever.initialize_clusters([Document(page_content="one")], [np.array([1, 0])])
    await retriever.initialize_clusters([Document(page_content="two")], [np.array([0, 1])])
    assert retriever.stats["total_documents"] == 2
    retriever.reset()
    assert retriever.stats["total_documents"] == 0
    assert retriever.cluster_manager.clusters == []


def test_rag_failed_result_is_not_a_successful_api_answer(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import assistant
    from app.services.assistant_sessions import AssistantSessions

    service = OptimizedAssistantService(sessions=AssistantSessions())
    service._initialized = True
    service._assistant = SimpleNamespace(get_answer=AsyncMock(return_value={"success": False, "answer": "provider failure"}))
    monkeypatch.setattr(assistant, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    app.include_router(assistant.router, prefix="/assistant")
    result = TestClient(app).post("/assistant/query", json={"question": "test", "mode": 1})
    assert result.status_code == 502


async def test_failed_answer_cache_invalidation_is_not_ingestion_success():
    rag = object.__new__(RAGAssistant)
    rag.vector_store = SimpleNamespace(add_documents=AsyncMock(return_value=["id"]))
    rag.retriever = SimpleNamespace(_cache={})
    rag.reranker = SimpleNamespace(_rerank_cache={})
    rag.cache_manager = SimpleNamespace(clear_all=lambda: {"success": False})
    rag._stats = {}
    result = await rag.upload_knowledge({"content": "new content"})
    assert result["success"] is False


async def test_answer_generation_failure_raises_instead_of_returning_error_text():
    from app.core.agents.enterprise_assistant import AnswerGenerator, QueryType

    generator = AnswerGenerator(SimpleNamespace(generate_response=AsyncMock(side_effect=RuntimeError("offline"))))
    with pytest.raises(AssistantError):
        await generator.generate("question", [Document(page_content="context")], QueryType.FACTUAL)


async def test_refresh_does_not_report_partial_indexing_as_success(tmp_path):
    (tmp_path / "one.md").write_text("one")
    (tmp_path / "two.md").write_text("two")
    rag = object.__new__(RAGAssistant)
    rag.config = SimpleNamespace(rag=SimpleNamespace(knowledge_base_path=str(tmp_path)))
    stage = SimpleNamespace(add_documents=AsyncMock(side_effect=[["one"], RuntimeError("index failed")]))
    rag.vector_store = SimpleNamespace(
        create_refresh_stage=AsyncMock(return_value=stage),
        publish_refresh_stage=AsyncMock(),
        discard_refresh_stage=AsyncMock(),
    )
    rag.clear_cache = AsyncMock(return_value={"success": True})
    assert (await rag.refresh_knowledge_base())["success"] is False
    assert stage.add_documents.await_count == 2
    rag.vector_store.publish_refresh_stage.assert_not_awaited()
    rag.vector_store.discard_refresh_stage.assert_awaited_once_with(stage)


def test_refresh_api_reports_failure(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import assistant

    service = OptimizedAssistantService()
    service._initialized = True
    service._assistant = SimpleNamespace(refresh_knowledge_base=AsyncMock(return_value={"success": False}))
    monkeypatch.setattr(assistant, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    app.include_router(assistant.router, prefix="/assistant")
    response = TestClient(app).post("/assistant/refresh")
    assert response.status_code == 502

async def test_local_index_mapping_is_isolated_between_instances():
    shared = MemoryIndexClient()
    first = IndexManager(shared, "offline")
    second = IndexManager(shared, "offline")
    first.create_index(2, "HNSW")
    second.create_index(2, "HNSW")
    await first.update_index(["old-A"], [np.array([1, 0])])
    await second.update_index(["new-B"], [np.array([0, 1])])
    assert (await first.search_vectors(np.array([1, 0], dtype=np.float32), 1))[0][0] == "old-A"
    assert (await second.search_vectors(np.array([0, 1], dtype=np.float32), 1))[0][0] == "new-B"
    assert not shared.values


@pytest.mark.parametrize("method", ["_semantic_search", "_semantic_search_with_embedding"])
async def test_semantic_search_includes_persisted_vectors_after_restart(method):
    from app.core.vector.redis_vector_store import EnhancedRedisVectorStore

    class VectorClient(MemoryIndexClient):
        def get(self, key):
            if key.endswith(":revision"):
                return self.values.get(key)
            return super().get(key)

        def scan(self, cursor, match, count):
            keys = sorted(key for key in self.values if key.startswith("offline:vec:"))
            return (1, keys[:1]) if cursor == 0 else (0, keys[1:])

    client = VectorClient()
    client.values = {
        "offline:vec:old-A": np.array([1, 0], dtype=np.float32).tobytes(),
        "offline:vec:new-B": np.array([0, 1], dtype=np.float32).tobytes(),
    }
    restarted = IndexManager(client, "offline")
    restarted.create_index(2, "HNSW")
    await restarted.update_index(["new-B"], [np.array([0, 1])])
    store = object.__new__(EnhancedRedisVectorStore)
    store._closed = True
    store.client = client
    store.collection_name = "offline"
    store.index_manager = restarted
    store.stats = {"error_count": 0}
    store._get_documents_bulk = AsyncMock(side_effect=lambda ids: {key: Document(page_content=key) for key in ids})
    result = await getattr(store, method)(np.array([1, 0], dtype=np.float32), 1)
    assert [(doc.page_content, score) for doc, score in result] == [("old-A", pytest.approx(1.0))]


async def test_semantic_search_fails_explicitly_on_incomplete_vector_read(monkeypatch):
    from app.core.vector import redis_vector_store as module

    class BrokenClient(MemoryIndexClient):
        def scan(self, cursor, match, count):
            return 0, ["offline:vec:broken"]

    store = object.__new__(module.EnhancedRedisVectorStore)
    store._closed = True
    store.client = BrokenClient()
    store.client.values["offline:vec:broken"] = b"bad"
    store.collection_name = "offline"
    store.stats = {"error_count": 0}
    with pytest.raises(module.VectorSearchError, match="维度"):
        await store._semantic_search(np.array([1, 0], dtype=np.float32), 1)


async def test_semantic_search_timeout_is_explicit(monkeypatch):
    import asyncio

    from app.core.vector import redis_vector_store as module

    store = object.__new__(module.EnhancedRedisVectorStore)
    store._closed = True
    store.stats = {"error_count": 0}
    async def delayed(*args):
        await asyncio.sleep(1)
    store._scan_semantic_vectors = delayed
    monkeypatch.setattr(module, "VECTOR_SCAN_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(module.VectorSearchError, match="超时"):
        await store._semantic_search(np.array([1, 0], dtype=np.float32), 1)

async def test_semantic_search_rejects_excess_vectors_instead_of_partial_results(monkeypatch):
    from app.core.vector import redis_vector_store as module

    class LargeClient(MemoryIndexClient):
        def scan(self, cursor, match, count):
            return 0, ["offline:vec:a", "offline:vec:b"]

    store = object.__new__(module.EnhancedRedisVectorStore)
    store._closed = True
    store.client = LargeClient()
    store.collection_name = "offline"
    store.stats = {"error_count": 0}
    monkeypatch.setattr(module, "VECTOR_SCAN_MAX_KEYS", 1)
    with pytest.raises(module.VectorSearchError, match="容量"):
        await store._semantic_search(np.array([1, 0], dtype=np.float32), 1)

@pytest.mark.parametrize("method", ["optimize_storage", "_cleanup_empty_term_sets", "_compress_sparse_term_sets"])
async def test_unsafe_index_maintenance_fails_closed_without_writing(method):
    from app.core.vector.redis_vector_store import EnhancedRedisVectorStore

    store = object.__new__(EnhancedRedisVectorStore)
    store._closed = True
    with pytest.raises(RuntimeError, match="revision事务"):
        await getattr(store, method)()

async def test_cancelled_stage_write_waits_for_redis_thread_before_cleanup():
    import asyncio
    import threading

    from app.core.vector.redis_vector_store import EnhancedRedisVectorStore

    started = threading.Event()
    finish = threading.Event()
    class Pipeline:
        def watch(self, *args): return self
        def get(self, *args): return None
        def hget(self, *args): return None
        def multi(self): return self
        def reset(self): return self
        def hset(self, *args, **kwargs): return self
        def set(self, *args, **kwargs): return self
        def sadd(self, *args, **kwargs): return self
        def incr(self, *args, **kwargs): return self
        def execute(self):
            started.set()
            if not finish.wait(5):
                raise RuntimeError("test timeout")
            return []

    store = object.__new__(EnhancedRedisVectorStore)
    store._closed = True
    store.collection_name = "owned-stage"
    store.client = SimpleNamespace(pipeline=Pipeline)
    operation = asyncio.create_task(store._store_batch([Document(page_content="one")], [np.array([1, 0])], 0))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
    finally:
        finish.set()


async def test_semantic_search_rejects_changes_during_scan():
    from app.core.vector.redis_vector_store import EnhancedRedisVectorStore, VectorSearchError

    class ChangingClient(MemoryIndexClient):
        revision = b"1"
        def get(self, key):
            if key.endswith(":revision"):
                return self.revision
            return super().get(key)
        def scan(self, cursor, match, count):
            return 0, ["offline:vec:old"]

    client = ChangingClient()
    client.values["offline:vec:old"] = np.array([1, 0], dtype=np.float32).tobytes()
    store = object.__new__(EnhancedRedisVectorStore)
    store._closed = True
    store.client = client
    store.collection_name = "offline"
    store.stats = {"error_count": 0}
    async def load_documents(ids):
        client.revision = b"2"
        return {"old": Document(page_content="old")}
    store._get_documents_bulk = load_documents
    with pytest.raises(VectorSearchError, match="查询期间发生变化"):
        await store._semantic_search(np.array([1, 0], dtype=np.float32), 1)

async def test_repeated_publish_cancellation_waits_until_transaction_thread_exits():
    import asyncio
    import threading

    from app.core.vector.redis_vector_store import EnhancedRedisVectorStore, RefreshPublishOutcomeUnknown

    started, finish = threading.Event(), threading.Event()
    store = object.__new__(EnhancedRedisVectorStore)
    store._closed = True
    store.collection_name = "owned"
    lease = SimpleNamespace(check=AsyncMock())
    stage = SimpleNamespace(collection_name="owned-refresh-" + "a" * 32, _refresh_origin="owned", _stage_lease=lease)
    async def publish(_lease):
        started.set()
        if not await asyncio.to_thread(finish.wait, 5):
            raise RuntimeError("test timeout")
    store._generation_gc = SimpleNamespace(protect_unknown=AsyncMock(), publish_cas=publish)
    operation = asyncio.create_task(store.publish_refresh_stage(stage))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        finish.set()
        with pytest.raises(RefreshPublishOutcomeUnknown):
            await operation
    finally:
        finish.set()

async def test_cancelled_stage_creation_closes_pool_after_constructor_finishes():
    import asyncio
    import threading

    from app.core.vector.redis_vector_store import EnhancedRedisVectorStore

    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    class SlowStore(EnhancedRedisVectorStore):
        def __init__(self, *args):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
            self._closed = False
        async def close(self):
            self._closed = True
            closed.set()

    store = object.__new__(SlowStore)
    store._closed = True
    store.client = SimpleNamespace(get=lambda key: None, hget=lambda key, field: None, scan=lambda *args, **kwargs: (0, []))
    lease = SimpleNamespace(source_generation="owned", source_revision=None, close=AsyncMock())
    store._generation_gc = SimpleNamespace(list_candidates=AsyncMock(return_value=[]), acquire_stage=AsyncMock(return_value=lease), abort_stage=AsyncMock(), collect=AsyncMock())
    store.collection_name = "owned"
    store.redis_config = {}
    store.embedding_model = None
    store.vector_dim = 2
    store.index_type = "FLAT"
    operation = asyncio.create_task(store.create_refresh_stage())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert closed.is_set()
    finally:
        release.set()


async def test_full_search_rejects_revision_change_before_cache_write():
    from app.core.vector.redis_vector_store import EnhancedRedisVectorStore, SearchConfig, VectorSearchError

    store = object.__new__(EnhancedRedisVectorStore)
    store._closed = False
    store.stats = {"search_count": 0, "cache_hits": 0, "error_count": 0}
    store.get_revision = AsyncMock(side_effect=["1", "2"])
    store._should_use_hierarchical_retrieval = lambda config: False
    store._standard_search = AsyncMock(return_value=[(Document(page_content="old"), 1.0)])
    cache = SimpleNamespace(get=lambda key: None, set=Mock())
    store.query_cache = cache
    with pytest.raises(VectorSearchError, match="查询期间发生变化"):
        await store.similarity_search("query", config=SearchConfig())
    cache.set.assert_not_called()
    store._closed = True

async def test_batch_search_cache_uses_generation_revision_and_propagates_failure():
    from app.core.vector.redis_vector_store import EnhancedRedisVectorStore, SearchConfig, VectorSearchError

    store = object.__new__(EnhancedRedisVectorStore)
    store._closed = True
    store._search_cache_scope = "a" * 32
    store.get_revision = AsyncMock(side_effect=["v1:1", "v1:1", "v2:1", "v2:1", "v2:1"])
    entries = {}
    store.query_cache = SimpleNamespace(get=entries.get, set=lambda key, result, ttl: entries.__setitem__(key, result))
    store._standard_search_with_embedding = AsyncMock(side_effect=[[(Document(page_content="old"), 1.0)], [(Document(page_content="new"), 1.0)], VectorSearchError("scan failed")])
    config = SearchConfig()
    first = await store._single_search_with_embedding("same", np.array([1, 0]), 1, config)
    second = await store._single_search_with_embedding("same", np.array([1, 0]), 1, config)
    assert first[0][0].page_content == "old"
    assert second[0][0].page_content == "new"
    assert len(entries) == 2
    with pytest.raises(VectorSearchError, match="scan failed"):
        await store._single_search_with_embedding("different", np.array([1, 0]), 1, config)


async def test_uncertain_publication_keeps_stage_but_closes_connection():
    from app.core.vector.redis_vector_store import EnhancedRedisVectorStore

    store = object.__new__(EnhancedRedisVectorStore)
    store._closed = True
    store.collection_name = "owned"
    store._generation_gc = SimpleNamespace(abort_stage=AsyncMock(), collect=AsyncMock())
    lease = SimpleNamespace(close=AsyncMock())
    stage = SimpleNamespace(collection_name="owned-refresh-" + "a" * 32, _refresh_origin="owned", _stage_lease=lease, _commit_started=True, close=AsyncMock(), clear=AsyncMock())
    await store.discard_refresh_stage(stage)
    stage.clear.assert_not_awaited()
    store._generation_gc.abort_stage.assert_not_awaited()
    store._generation_gc.collect.assert_not_awaited()
    stage.close.assert_awaited_once()
    lease.close.assert_awaited_once()

async def test_post_publish_lease_loss_reports_unknown_instead_of_safe_failure():
    from contextvars import ContextVar

    from app.core.vector.generation_gc import GenerationLeaseLost
    from app.core.vector.redis_vector_store import RefreshPublishOutcomeUnknown, _generation_scoped

    lease = AsyncMock()
    lease.generation = "owned"
    lease.__aenter__.return_value = lease
    lease.__aexit__.return_value = False
    lease.check.side_effect = GenerationLeaseLost("expired")
    store = SimpleNamespace(_generation_scope=ContextVar("test_generation", default=None), _generation_gc=SimpleNamespace(acquire_active=AsyncMock(return_value=lease)))
    @_generation_scoped
    async def add_documents(_store):
        return ["already-published"]
    with pytest.raises(RefreshPublishOutcomeUnknown, match="可能已生效"):
        await add_documents(store)
