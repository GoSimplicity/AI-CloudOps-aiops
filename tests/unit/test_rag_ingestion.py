"""知识写入必须可检索，索引失败不得报告成功"""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

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

    def pipeline(self):
        self.operations = []
        return self

    def set(self, key, value):
        self.operations.append(("set", key, value))
        return self

    def get(self, key):
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

    service = OptimizedAssistantService()
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
    rag.vector_store = SimpleNamespace(clear=AsyncMock(return_value={"success": True}), get_document_count=AsyncMock(return_value=1))
    rag.clear_cache = AsyncMock(return_value={"success": True})
    rag.upload_knowledge = AsyncMock(side_effect=[{"success": True, "document_count": 1}, {"success": False}])
    assert (await rag.refresh_knowledge_base())["success"] is False


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
