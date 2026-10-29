"""知识库刷新构建新版本时保留当前知识"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.agents.enterprise_assistant import RAGAssistant


class Stage:
    def __init__(self):
        self.docs = []
        self.wait = None
        self.error = None

    async def add_documents(self, docs):
        if self.wait:
            await self.wait.wait()
        if self.error:
            raise self.error
        self.docs.extend(doc.page_content for doc in docs)
        return [f"doc-{len(self.docs)}"]

    async def add_md_documents(self, contents, _metadata):
        if self.wait:
            await self.wait.wait()
        if self.error:
            raise self.error
        self.docs.extend(contents)
        return [f"doc-{len(self.docs)}"]


class Store:
    def __init__(self):
        self.docs = ["old"]
        self.version = 0
        self.stage = Stage()
        self.captured_version = None
        self.discards = 0

    async def create_refresh_stage(self):
        self.captured_version = self.version
        return self.stage

    async def publish_refresh_stage(self, stage):
        if self.captured_version != self.version:
            raise RuntimeError("concurrent write")
        self.docs = list(stage.docs)
        self.version += 1

    async def discard_refresh_stage(self, stage):
        assert stage is self.stage
        self.discards += 1


def assistant(path, store):
    rag = object.__new__(RAGAssistant)
    rag.config = SimpleNamespace(rag=SimpleNamespace(knowledge_base_path=str(path)))
    rag.vector_store = store
    rag.clear_cache = AsyncMock(return_value={"success": True})
    return rag


async def test_missing_directory_keeps_existing_index(tmp_path):
    store = Store()
    result = await assistant(tmp_path / "missing", store).refresh_knowledge_base()
    assert result["success"] is False
    assert store.docs == ["old"]
    assert store.captured_version is None


@pytest.mark.parametrize("failure", [RuntimeError("embedding failed"), OSError("storage failed")])
async def test_failed_build_never_publishes_and_cleans_stage(tmp_path, failure):
    (tmp_path / "a.txt").write_text("new")
    store = Store()
    store.stage.error = failure
    result = await assistant(tmp_path, store).refresh_knowledge_base()
    assert result["success"] is False
    assert store.docs == ["old"]
    assert store.discards == 1


async def test_cancellation_preserves_current_index(tmp_path):
    (tmp_path / "a.txt").write_text("new")
    store = Store()
    store.stage.wait = asyncio.Event()
    operation = asyncio.create_task(assistant(tmp_path, store).refresh_knowledge_base())
    for _ in range(100):
        if store.captured_version is not None:
            break
        await asyncio.sleep(0)
    assert store.captured_version is not None
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert store.docs == ["old"]
    assert store.discards == 1


async def test_concurrent_upload_refuses_overwriting_newer_data(tmp_path):
    (tmp_path / "a.txt").write_text("new")
    store = Store()
    store.stage.wait = asyncio.Event()
    operation = asyncio.create_task(assistant(tmp_path, store).refresh_knowledge_base())
    for _ in range(100):
        if store.captured_version is not None:
            break
        await asyncio.sleep(0)
    assert store.captured_version is not None
    store.docs.append("concurrent")
    store.version += 1
    store.stage.wait.set()
    result = await operation
    assert result["success"] is False
    assert store.docs == ["old", "concurrent"]
    assert store.discards == 1


async def test_success_publishes_complete_stage(tmp_path):
    (tmp_path / "a.txt").write_text("new")
    store = Store()
    result = await assistant(tmp_path, store).refresh_knowledge_base()
    assert result["success"] is True
    assert store.docs == ["new"]
    assert store.discards == 1


async def test_vector_search_error_is_not_converted_to_empty_results():
    from app.core.agents.enterprise_assistant import DocumentRetriever
    from app.core.vector.redis_vector_store import VectorSearchError

    strategy = SimpleNamespace(enable_cache=False, initial_k=2, final_k=2, min_similarity=0.1)
    store = SimpleNamespace(similarity_search=AsyncMock(side_effect=VectorSearchError("scan timed out")))
    retriever = DocumentRetriever(store, strategy, SimpleNamespace(retrieval_cache_size=10))
    with pytest.raises(VectorSearchError):
        await retriever.retrieve(["question"])


async def test_indeterminate_publish_is_reported_without_retry_claim(tmp_path):
    from app.core.vector.redis_vector_store import RefreshPublishOutcomeUnknown

    (tmp_path / "a.txt").write_text("new")
    store = Store()
    store.publish_refresh_stage = AsyncMock(side_effect=RefreshPublishOutcomeUnknown("sensitive upstream"))
    result = await assistant(tmp_path, store).refresh_knowledge_base()
    assert result["success"] is False
    assert result["published"] is None
    assert result["outcome_unknown"] is True
    assert result["retry_safe"] is False
    assert "sensitive upstream" not in str(result)
    assert store.discards == 1


@pytest.mark.parametrize(
    ("published", "unknown"), [(True, False), (None, True)]
)
def test_refresh_http_exposes_safe_commit_outcome(monkeypatch, published, unknown):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from app.api.middleware.error_handler import custom_http_exception_handler
    from app.api.routes import assistant as assistant_route
    from app.services.assistant_service import OptimizedAssistantService

    service = OptimizedAssistantService()
    service._initialized = True
    service._assistant = SimpleNamespace(refresh_knowledge_base=AsyncMock(return_value={
        "success": False, "published": published, "outcome_unknown": unknown,
        "message": "sensitive upstream", "retry_safe": False,
    }))
    monkeypatch.setattr(assistant_route, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    app.add_exception_handler(HTTPException, custom_http_exception_handler)
    app.include_router(assistant_route.router, prefix="/assistant")
    response = TestClient(app).post("/assistant/refresh")
    assert response.status_code == 409
    body = response.json()
    assert body["data"]["error_code"] == "KNOWLEDGE_REFRESH_UNCERTAIN"
    assert body["data"]["details"] == {
        "published": published, "outcome_unknown": unknown, "retry_safe": False,
    }
    assert "sensitive upstream" not in str(body)


async def test_published_cache_failure_requires_state_check(tmp_path):
    (tmp_path / "a.txt").write_text("new")
    store = Store()
    rag = assistant(tmp_path, store)
    rag.clear_cache = AsyncMock(return_value={"success": False})
    result = await rag.refresh_knowledge_base()
    assert result["success"] is False
    assert result["published"] is True
    assert result["outcome_unknown"] is False
    assert result["retry_safe"] is False
    assert store.docs == ["new"]
    assert store.discards == 1


async def test_retriever_cache_tracks_knowledge_version():
    from langchain_core.documents import Document

    from app.core.agents.enterprise_assistant import DocumentRetriever

    revision = {"value": "1"}
    store = SimpleNamespace(
        get_revision=AsyncMock(side_effect=lambda: revision["value"]),
        similarity_search=AsyncMock(side_effect=[[(Document(page_content="old"), 0.9)], [(Document(page_content="new"), 0.9)]]),
    )
    strategy = SimpleNamespace(enable_cache=True, cache_ttl=60, initial_k=2, final_k=2, min_similarity=0.1)
    retriever = DocumentRetriever(store, strategy, SimpleNamespace(retrieval_cache_size=10))
    assert (await retriever.retrieve(["query"]))[0].page_content == "old"
    assert (await retriever.retrieve(["query"]))[0].page_content == "old"
    revision["value"] = "2"
    assert (await retriever.retrieve(["query"]))[0].page_content == "new"
    assert store.similarity_search.await_count == 2


async def test_retriever_fails_when_version_changes_during_search():
    from langchain_core.documents import Document

    from app.core.agents.enterprise_assistant import DocumentRetriever
    from app.core.vector.redis_vector_store import VectorSearchError

    revision = {"value": "1"}
    async def search(**kwargs):
        revision["value"] = "2"
        return [(Document(page_content="stale"), 0.9)]
    store = SimpleNamespace(get_revision=AsyncMock(side_effect=lambda: revision["value"]), similarity_search=search)
    strategy = SimpleNamespace(enable_cache=True, cache_ttl=60, initial_k=2, final_k=2, min_similarity=0.1)
    retriever = DocumentRetriever(store, strategy, SimpleNamespace(retrieval_cache_size=10))
    with pytest.raises(VectorSearchError):
        await retriever.retrieve(["query"])
    assert not retriever._cache


async def test_answer_cache_tracks_version_and_fails_on_unknown_revision():
    revision = {"value": "1"}
    values = {}
    store = SimpleNamespace(collection_name="offline", get_revision=AsyncMock(side_effect=lambda: revision["value"]))
    cache = SimpleNamespace(get=lambda key: values.get(key), set=lambda key, value, ttl: values.update({key: value}))
    rag = RAGAssistant(store, None, cache_manager=cache)
    rag.graph = SimpleNamespace(ainvoke=AsyncMock(side_effect=[
        {"answer": "old", "confidence": 0.9}, {"answer": "new", "confidence": 0.9},
    ]))
    assert (await rag.get_answer("query"))["answer"] == "old"
    assert (await rag.get_answer("query"))["cache_hit"] is True
    revision["value"] = "2"
    assert (await rag.get_answer("query"))["answer"] == "new"
    assert rag.graph.ainvoke.await_count == 2
    store.get_revision = AsyncMock(side_effect=RuntimeError("offline secret"))
    result = await rag.get_answer("query")
    assert result["success"] is False
    assert "offline secret" not in str(result)


async def test_answer_does_not_return_or_cache_when_revision_changes_during_generation():
    revision = {"value": "1"}
    values = {}
    store = SimpleNamespace(collection_name="offline", get_revision=AsyncMock(side_effect=lambda: revision["value"]))
    cache = SimpleNamespace(get=lambda key: values.get(key), set=lambda key, value, ttl: values.update({key: value}))
    rag = RAGAssistant(store, None, cache_manager=cache)
    async def generate(*args, **kwargs):
        revision["value"] = "2"
        return {"answer": "stale", "confidence": 0.9}
    rag.graph = SimpleNamespace(ainvoke=generate)
    result = await rag.get_answer("query")
    assert result["success"] is False
    assert "stale" not in str(result)
    assert not values


async def test_legacy_unversioned_store_does_not_reuse_answer_cache():
    values = {}
    cache = SimpleNamespace(get=lambda key: values.get(key), set=lambda key, value, ttl: values.update({key: value}))
    rag = RAGAssistant(SimpleNamespace(collection_name="legacy"), None, cache_manager=cache)
    rag.graph = SimpleNamespace(ainvoke=AsyncMock(side_effect=[
        {"answer": "first", "confidence": 0.9}, {"answer": "second", "confidence": 0.9},
    ]))
    assert (await rag.get_answer("query"))["answer"] == "first"
    assert (await rag.get_answer("query"))["answer"] == "second"
    assert rag.graph.ainvoke.await_count == 2
    assert not values


async def test_answer_cached_hit_rejects_concurrent_version_change():
    revision = {"value": "1"}
    values = {}
    def cache_get(key):
        value = values.get(key)
        if value:
            revision["value"] = "2"
        return value
    cache = SimpleNamespace(get=cache_get, set=lambda key, value, ttl: values.update({key: value}))
    store = SimpleNamespace(collection_name="offline", get_revision=AsyncMock(side_effect=lambda: revision["value"]))
    rag = RAGAssistant(store, None, cache_manager=cache)
    rag.graph = SimpleNamespace(ainvoke=AsyncMock(return_value={"answer": "old", "confidence": 0.9}))
    assert (await rag.get_answer("query"))["success"] is True
    result = await rag.get_answer("query")
    assert result["success"] is False
    assert "old" not in str(result)
    assert rag.graph.ainvoke.await_count == 1


async def test_failed_revision_check_does_not_pollute_session_history():
    revision = {"value": "1"}
    store = SimpleNamespace(collection_name="offline", get_revision=AsyncMock(side_effect=lambda: revision["value"]))
    def cache_write(key, value, ttl):
        revision["value"] = "2"
    cache = SimpleNamespace(get=lambda key: None, set=cache_write)
    rag = RAGAssistant(store, None, cache_manager=cache)
    rag.context_manager.update_context("session", {"recent_queries": ["previous"]})
    previous = list(rag.context_manager.get_session_details("session")["conversation_history"])
    rag.graph = SimpleNamespace(ainvoke=AsyncMock(return_value={"answer": "stale answer", "confidence": 0.9}))
    result = await rag.get_answer("new question", "session")
    assert result["success"] is False
    assert rag.context_manager.get_session_details("session")["conversation_history"] == previous
    assert list(rag.context_manager.get_context("session")["recent_queries"]) == ["previous"]


async def test_failed_generation_does_not_pollute_session_history():
    store = SimpleNamespace(collection_name="offline", get_revision=AsyncMock(return_value="1"))
    rag = RAGAssistant(store, None)
    rag.context_manager.update_context("session", {"recent_queries": ["previous"]})
    previous = list(rag.context_manager.get_session_details("session")["conversation_history"])
    rag.graph = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("controlled failure")))
    result = await rag.get_answer("new question", "session")
    assert result["success"] is False
    assert rag.context_manager.get_session_details("session")["conversation_history"] == previous
    assert list(rag.context_manager.get_context("session")["recent_queries"]) == ["previous"]


async def test_cancelled_generation_does_not_pollute_session_history():
    store = SimpleNamespace(collection_name="offline", get_revision=AsyncMock(return_value="1"))
    rag = RAGAssistant(store, None)
    rag.context_manager.update_context("session", {"recent_queries": ["previous"]})
    previous = list(rag.context_manager.get_session_details("session")["conversation_history"])
    started, release = asyncio.Event(), asyncio.Event()
    async def generate(*args, **kwargs):
        started.set()
        await release.wait()
    rag.graph = SimpleNamespace(ainvoke=generate)
    operation = asyncio.create_task(rag.get_answer("new question", "session"))
    await started.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert rag.context_manager.get_session_details("session")["conversation_history"] == previous
    assert list(rag.context_manager.get_context("session")["recent_queries"]) == ["previous"]


async def test_successful_generation_commits_user_and_assistant_turn_together():
    store = SimpleNamespace(collection_name="offline", get_revision=AsyncMock(return_value="1"))
    rag = RAGAssistant(store, None)
    rag.graph = SimpleNamespace(ainvoke=AsyncMock(return_value={"answer": "answer", "confidence": 0.9}))
    result = await rag.get_answer("question", "session")
    assert result["success"] is True
    history = rag.context_manager.get_session_details("session")["conversation_history"]
    assert [(item["type"], item["content"]) for item in history] == [("user", "question"), ("assistant", "answer")]


@pytest.mark.parametrize("failure", ["revision_change", "generation_error"])
async def test_real_memory_saver_does_not_reuse_failed_turn(failure):
    from langchain_core.documents import Document

    from app.common.identity import INTERNAL_PRINCIPAL, principal_context
    from app.core.agents.enterprise_assistant import QueryType

    revision = {"value": "1"}
    store = SimpleNamespace(collection_name="offline", get_revision=AsyncMock(side_effect=lambda: revision["value"]))
    rag = RAGAssistant(store, None)
    rag.query_processor.analyze_and_expand = lambda question: (QueryType.GENERAL, [question], 1.0, {})
    rag.retriever.retrieve = AsyncMock(side_effect=[
        [Document(page_content="failed-only-document")],
        [Document(page_content="current-document")],
    ])
    rag.reranker.rerank = AsyncMock(side_effect=lambda _question, docs, **_kwargs: docs)
    calls = []
    async def generate(question, documents, query_type, context):
        calls.append((question, [d.page_content for d in documents], list(context.get("recent_queries", []))))
        if question == "failed question":
            if failure == "generation_error":
                raise RuntimeError("controlled generation failure")
            revision["value"] = "2"
            return {"answer": "failed-only-answer", "confidence": 0.9}
        return {"answer": "current-answer", "confidence": 0.9}
    rag.generator.generate = generate

    first = await rag.get_answer("failed question", "session")
    assert first["success"] is False
    assert rag.context_manager.get_session_details("session")["conversation_history"] == []
    second = await rag.get_answer("current question", "session")
    assert second["success"] is True
    assert second["answer"] == "current-answer"
    assert calls[1] == ("current question", ["current-document"], [])
    assert "failed-only" not in str(second)
    history = rag.context_manager.get_session_details("session")["conversation_history"]
    assert [(item["type"], item["content"]) for item in history] == [("user", "current question"), ("assistant", "current-answer")]
    thread = f"{principal_context.get() or INTERNAL_PRINCIPAL}:session"
    checkpoint = await rag.graph.aget_state({"configurable": {"thread_id": thread}})
    assert checkpoint.values["question"] == "current question"
    assert checkpoint.values["answer"] == "current-answer"
    assert "failed-only" not in str(checkpoint.values)


async def test_retrieval_logs_do_not_include_question_or_document_content(caplog):
    import logging

    from langchain_core.documents import Document

    from app.core.agents.enterprise_assistant import AnswerGenerator, QueryType

    question = "private-question-canary"
    document = "private-document-canary"
    store = SimpleNamespace(
        collection_name="offline", get_revision=AsyncMock(return_value="1"),
        similarity_search=AsyncMock(return_value=[(Document(page_content=document), 0.9)]),
    )
    rag = RAGAssistant(store, None)
    rag.reranker.rerank = AsyncMock(side_effect=lambda _question, docs, **_kwargs: docs)
    rag.generator.generate = AsyncMock(return_value={"answer": "safe", "confidence": 0.9})
    with caplog.at_level(logging.DEBUG):
        result = await rag.get_answer(question, "session")
        await AnswerGenerator(None).generate(question, [], QueryType.GENERAL)
    assert result["success"] is True
    assert question not in caplog.text
    assert document not in caplog.text


async def test_retrieval_error_logs_do_not_include_exception_content(caplog):
    import logging

    from app.core.agents.enterprise_assistant import DocumentRetriever

    secret = "private-provider-error-canary"
    store = SimpleNamespace(get_revision=AsyncMock(return_value="1"), similarity_search=AsyncMock(side_effect=RuntimeError(secret)))
    strategy = SimpleNamespace(enable_cache=False, initial_k=2, final_k=2, min_similarity=0.1)
    retriever = DocumentRetriever(store, strategy, SimpleNamespace(retrieval_cache_size=10))
    with caplog.at_level(logging.DEBUG):
        assert await retriever.retrieve(["query"]) == []
    assert secret not in caplog.text


@pytest.mark.parametrize("operation", ["add-document", "upload-knowledge-file"])
@pytest.mark.parametrize("outcome", ["unknown", "cache_failure", "vector_cache_failure"])
def test_document_write_http_reports_uncertain_or_published_state(monkeypatch, tmp_path, operation, outcome):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from app.api.middleware.error_handler import custom_http_exception_handler
    from app.api.routes import assistant as assistant_route
    from app.config.settings import config
    from app.core.vector.redis_vector_store import RefreshPublishedCacheError, RefreshPublishOutcomeUnknown
    from app.services.assistant_service import OptimizedAssistantService

    monkeypatch.setattr(config.rag, "knowledge_base_path", str(tmp_path))
    service = OptimizedAssistantService()
    service._ensure_ready = AsyncMock()
    rag = object.__new__(RAGAssistant)
    if outcome == "unknown":
        rag.vector_store = SimpleNamespace(add_documents=AsyncMock(side_effect=RefreshPublishOutcomeUnknown("private upstream canary")))
    elif outcome == "vector_cache_failure":
        rag.vector_store = SimpleNamespace(add_documents=AsyncMock(side_effect=RefreshPublishedCacheError("private cache canary")))
    else:
        rag.vector_store = SimpleNamespace(add_documents=AsyncMock(return_value=["id"]))
    rag.clear_cache = AsyncMock(return_value={"success": False})
    service._assistant = rag
    monkeypatch.setattr(assistant_route, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    app.add_exception_handler(HTTPException, custom_http_exception_handler)
    app.include_router(assistant_route.router, prefix="/assistant")
    with TestClient(app) as client:
        if operation == "add-document":
            response = client.post("/assistant/add-document", json={"title": "test", "content": "body", "file_name": "test.txt"})
        else:
            response = client.post("/assistant/upload-knowledge-file", files={"file": ("test.txt", b"body", "text/plain")})
    assert response.status_code == 409
    body = response.json()
    assert body["data"]["error_code"] == "KNOWLEDGE_WRITE_UNCERTAIN"
    assert body["data"]["details"] == {
        "published": None if outcome == "unknown" else True,
        "outcome_unknown": outcome == "unknown", "retry_safe": False, "file_saved": True,
    }
    assert "private upstream canary" not in str(body)
    assert "private cache canary" not in str(body)
    assert (tmp_path / "test.txt").is_file()


@pytest.mark.parametrize("raises", [False, True])
async def test_cache_clear_redacts_provider_message_and_exception(caplog, raises):
    import logging

    secret = "private-cache-provider-canary"
    def clear_all():
        if raises:
            raise RuntimeError(secret)
        return {"success": False, "message": secret}
    rag = object.__new__(RAGAssistant)
    rag.retriever = SimpleNamespace(_cache={})
    rag.reranker = SimpleNamespace(_rerank_cache={})
    rag.cache_manager = SimpleNamespace(clear_all=clear_all)
    rag.vector_store = SimpleNamespace()
    rag._stats = {}
    with caplog.at_level(logging.DEBUG):
        result = await rag.clear_cache()
    assert result["success"] is False
    assert secret not in caplog.text
    assert secret not in str(result)


async def test_unexpected_cache_clear_failure_never_exposes_traceback(caplog, capsys):
    import logging

    secret = "private-outer-error-canary"
    class BrokenRetriever:
        @property
        def _cache(self):
            raise RuntimeError(secret)
    rag = object.__new__(RAGAssistant)
    rag.retriever = BrokenRetriever()
    with caplog.at_level(logging.DEBUG):
        result = await rag.clear_cache()
    assert result["success"] is False
    assert secret not in str(result)
    assert secret not in caplog.text
    assert secret not in capsys.readouterr().err


async def test_failed_answer_does_not_expose_provider_exception(caplog):
    import logging

    secret = "private-answer-provider-canary"
    rag = RAGAssistant(SimpleNamespace(collection_name="offline", get_revision=AsyncMock(return_value="1")), None)
    rag.graph = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError(secret)))
    with caplog.at_level(logging.DEBUG):
        result = await rag.get_answer("question")
    assert result["success"] is False
    assert secret not in str(result)
    assert secret not in caplog.text


@pytest.mark.parametrize("operation", ["add-document", "upload-knowledge-file"])
def test_successful_document_api_returns_filename_without_logging_it(monkeypatch, tmp_path, caplog, operation):
    import logging

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import assistant as assistant_route
    from app.config.settings import config
    from app.services.assistant_service import OptimizedAssistantService

    filename = "private-document-name-canary.txt"
    monkeypatch.setattr(config.rag, "knowledge_base_path", str(tmp_path))
    service = OptimizedAssistantService()
    service._ensure_ready = AsyncMock()
    service._assistant = SimpleNamespace(upload_knowledge=AsyncMock(return_value={"success": True, "document_count": 1}))
    monkeypatch.setattr(assistant_route, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    app.include_router(assistant_route.router, prefix="/assistant")
    with caplog.at_level(logging.DEBUG, logger="aiops.services.assistant"), TestClient(app) as client:
        if operation == "add-document":
            response = client.post("/assistant/add-document", json={"title": "test", "content": "body", "file_name": filename})
        else:
            response = client.post("/assistant/upload-knowledge-file", files={"file": (filename, b"body", "text/plain")})
    assert response.status_code == 200
    assert filename in str(response.json())  # 授权调用方仍能读取实际文件名
    assert filename not in "\n".join(record.getMessage() for record in caplog.records if record.name == "aiops.services.assistant")


@pytest.mark.parametrize("failure", ["exception", "returned_failure"])
def test_clear_cache_http_never_reports_failure_as_success(monkeypatch, caplog, failure):
    import logging

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from app.api.middleware.error_handler import custom_http_exception_handler
    from app.api.routes import assistant as assistant_route
    from app.services.assistant_service import OptimizedAssistantService

    secret = "private-cache-failure-canary"
    service = OptimizedAssistantService()
    service._initialized = True
    service._assistant = SimpleNamespace(clear_cache=AsyncMock(
        side_effect=RuntimeError(secret) if failure == "exception" else None,
        return_value={"success": False, "message": secret},
    ))
    monkeypatch.setattr(assistant_route, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    app.add_exception_handler(HTTPException, custom_http_exception_handler)
    app.include_router(assistant_route.router, prefix="/assistant")
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        response = client.post("/assistant/clear-cache")
    assert response.status_code == 502
    assert secret not in str(response.json())
    assert secret not in caplog.text


def test_clear_cache_http_success_preserves_contract(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import assistant as assistant_route
    from app.services.assistant_service import OptimizedAssistantService

    service = OptimizedAssistantService()
    service._initialized = True
    service._assistant = SimpleNamespace(clear_cache=AsyncMock(return_value={"success": True, "cleared_items": 3}))
    monkeypatch.setattr(assistant_route, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    app.include_router(assistant_route.router, prefix="/assistant")
    response = TestClient(app).post("/assistant/clear-cache")
    assert response.status_code == 200
    assert response.json()["data"]["cleared"] is True
    assert response.json()["data"]["cleared_items"] == 3


def test_assistant_initialization_failure_http_redacts_provider_exception(monkeypatch, caplog):
    import logging

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from app.api.middleware.error_handler import custom_http_exception_handler
    from app.api.routes import assistant as assistant_route
    from app.services.assistant_service import OptimizedAssistantService

    secret = "private-initialization-canary"
    service = OptimizedAssistantService()
    service._do_initialize = AsyncMock(side_effect=RuntimeError(secret))
    monkeypatch.setattr(assistant_route, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    app.add_exception_handler(HTTPException, custom_http_exception_handler)
    app.include_router(assistant_route.router, prefix="/assistant")
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        response = client.post("/assistant/clear-cache")
    assert response.status_code == 503
    assert response.json()["data"]["error_code"] == "SERVICE_UNAVAILABLE"
    assert secret not in str(response.json())
    assert secret not in caplog.text


def test_unexpected_route_exception_is_not_logged_or_returned(monkeypatch, caplog):
    import logging

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from app.api.middleware.error_handler import custom_http_exception_handler
    from app.api.routes import assistant as assistant_route

    secret = "private-route-exception-canary"
    service = SimpleNamespace(initialize=AsyncMock(), clear_cache=AsyncMock(side_effect=RuntimeError(secret)))
    monkeypatch.setattr(assistant_route, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    app.add_exception_handler(HTTPException, custom_http_exception_handler)
    app.include_router(assistant_route.router, prefix="/assistant")
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        response = client.post("/assistant/clear-cache")
    assert response.status_code == 500
    assert secret not in str(response.json())
    assert secret not in caplog.text


async def test_websocket_unexpected_failure_does_not_echo_exception():
    from app.models.assistant_models import AssistantRequest
    from app.services.assistant_service import OptimizedAssistantService

    secret = "private-websocket-provider-canary"
    service = OptimizedAssistantService()
    service.get_answer = AsyncMock(side_effect=RuntimeError(secret))
    websocket = SimpleNamespace(send_json=AsyncMock(), close=AsyncMock())
    await service.handle_ws_answer(websocket, AssistantRequest(question="question", mode=1))
    payload = websocket.send_json.await_args.args[0]
    assert payload["success"] is False
    assert secret not in str(payload)


async def test_base_service_errors_are_redacted_in_health_and_timeout(caplog):
    import logging

    from app.common.exceptions import ServiceUnavailableError
    from app.services.assistant_service import OptimizedAssistantService

    secret = "private-base-service-canary"
    service = OptimizedAssistantService()
    service._do_health_check = AsyncMock(side_effect=RuntimeError(secret))
    async def broken_operation():
        raise RuntimeError(secret)
    with caplog.at_level(logging.DEBUG):
        assert await service.health_check() is False
        with pytest.raises(ServiceUnavailableError) as failure:
            await service.execute_with_timeout(broken_operation, operation_name="test")
    assert secret not in str(failure.value.details)
    assert secret not in caplog.text
    assert failure.value.error_code == "SERVICE_UNAVAILABLE"


async def test_websocket_validation_rejects_without_echoing_input(caplog):
    import logging

    from app.services.assistant_service import WebSocketManager

    secret = "private-ws-input-canary"
    websocket = SimpleNamespace(send_json=AsyncMock())
    manager = WebSocketManager(None)
    with caplog.at_level(logging.DEBUG):
        assert await manager._parse_message(secret, websocket) is None
    payload = websocket.send_json.await_args.args[0]
    assert payload["error"] == "参数校验失败"
    assert secret not in str(payload)
    assert secret not in caplog.text


async def test_refresh_rejects_symlink_without_reading_outside(tmp_path):
    directory = tmp_path / "knowledge"
    directory.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private-outside-canary")
    (directory / "linked.txt").symlink_to(outside)
    store = Store()
    result = await assistant(directory, store).refresh_knowledge_base()
    assert result["success"] is False
    assert "private-outside-canary" not in str(result)
    assert store.docs == ["old"]
    assert store.stage.docs == []
    assert store.discards == 1


async def test_refresh_rejects_file_replaced_after_lstat(tmp_path, monkeypatch):
    import os

    directory = tmp_path / "knowledge"
    directory.mkdir()
    file_path = directory / "safe.txt"
    file_path.write_text("safe")
    outside = tmp_path / "outside.txt"
    outside.write_text("private-outside-canary")
    original_open = os.open
    swapped = False
    def race(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "safe.txt" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            file_path.rename(directory / "original.bak")
            file_path.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", race)
    store = Store()
    result = await assistant(directory, store).refresh_knowledge_base()
    assert swapped
    assert result["success"] is False
    assert "private-outside-canary" not in str(result)
    assert store.docs == ["old"]
    assert store.stage.docs == []
    assert store.discards == 1


async def test_refresh_rejects_oversized_file_and_preserves_old_index(tmp_path):
    oversized = tmp_path / "large.txt"
    with oversized.open("wb") as stream:
        stream.truncate(10 * 1024 * 1024 + 1)
    store = Store()
    result = await assistant(tmp_path, store).refresh_knowledge_base()
    assert result["success"] is False
    assert store.docs == ["old"]
    assert store.discards == 1


def test_refresh_published_but_stage_cleanup_failed_returns_409(monkeypatch, tmp_path):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from app.api.middleware.error_handler import custom_http_exception_handler
    from app.api.routes import assistant as assistant_route
    from app.services.assistant_service import OptimizedAssistantService

    (tmp_path / "file.txt").write_text("new")
    store = Store()
    store.discard_refresh_stage = AsyncMock(side_effect=RuntimeError("private-cleanup-canary"))
    rag = assistant(tmp_path, store)
    service = OptimizedAssistantService()
    service._initialized = True
    service._assistant = rag
    monkeypatch.setattr(assistant_route, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    app.add_exception_handler(HTTPException, custom_http_exception_handler)
    app.include_router(assistant_route.router, prefix="/assistant")
    response = TestClient(app).post("/assistant/refresh")
    assert response.status_code == 409
    details = response.json()["data"]["details"]
    assert details == {"published": True, "outcome_unknown": False, "retry_safe": False}
    assert "private-cleanup-canary" not in str(response.json())
    assert store.docs == ["new"]


async def test_refresh_rejects_configured_root_symlink_swapped_during_read(tmp_path, monkeypatch):
    import os

    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    configured = tmp_path / "knowledge-link"
    original.mkdir()
    replacement.mkdir()
    (original / "safe.txt").write_text("original-content")
    (replacement / "safe.txt").write_text("private-outside-canary")
    configured.symlink_to(original, target_is_directory=True)
    inode = (original / "safe.txt").stat().st_ino
    real_read = os.read
    swapped = False
    def race(fd, size):
        nonlocal swapped
        if not swapped and os.fstat(fd).st_ino == inode:
            swapped = True
            configured.unlink()
            configured.symlink_to(replacement, target_is_directory=True)
        return real_read(fd, size)
    monkeypatch.setattr(os, "read", race)
    store = Store()
    result = await assistant(configured, store).refresh_knowledge_base()
    assert swapped
    assert result["success"] is False
    assert store.docs == ["old"]
    assert store.stage.docs == []
    assert store.discards == 1
    assert "private-outside-canary" not in str(result)
