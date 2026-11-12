"""#23 之后 main 上仍把异常原文或堆栈写入日志的三条路径：对外文案保持通用，日志只记类型。"""

import logging
from unittest.mock import AsyncMock, Mock

from langchain_core.documents import Document
import numpy as np

from app.api.routes import autofix as autofix_routes
from app.config.settings import config
from app.core.processors.md_document_processor import MDChunk, MDDocumentProcessor
from app.core.retrieval.hierarchical_retriever import (
    HierarchicalRetriever,
    QueryComplexity,
    RetrievalContext,
    RetrievalStage,
    StageResult,
)
from tests.unit.test_autofix_sec26_repair_bootstrap import REPAIR_BODY, make_app
from tests.unit.test_autofix_sec26_run_lockdown import make_service, mock_k8s

MARKER = "dependency-raw-detail-main-7f3a"


def _rendered(caplog, logger_name: str) -> str:
    parts = []
    for record in caplog.records:
        if logger_name and not record.name.startswith(logger_name):
            continue
        parts.append(record.getMessage())
        if record.exc_info:
            parts.append(logging.Formatter().formatException(record.exc_info))
    return "\n".join(parts)


def _assert_hidden(caplog, logger_name: str, *context: str, exc_name: str = "RuntimeError") -> None:
    matched = [record for record in caplog.records if record.name.startswith(logger_name)]
    assert matched
    assert all(record.exc_info is None for record in matched)
    text = _rendered(caplog, logger_name)
    assert MARKER not in text
    assert "Traceback" not in text
    assert exc_name in text
    for item in context:
        assert item in text


def _processor() -> MDDocumentProcessor:
    return MDDocumentProcessor(
        {
            "enable_parallel_processing": False,
            "enable_caching": False,
            "enable_metadata_enhancement": False,
        }
    )


class _ChunkEnhancer:
    def enhance_metadata(self, content, metadata):
        if metadata.get("chunk_index") == 1:
            raise RuntimeError(MARKER)
        return {"ok": True}

    def to_dict(self, enhanced):
        return {"semantic_tags": ["kept"], "word_count": 3}


class _DepthBoom:
    @property
    def technical_depth(self):
        raise RuntimeError(MARKER)


def test_chunk_metadata_and_weight_logs_hide_exception_text(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="aiops.md_processor")
    processor = _processor()
    processor.metadata_enhancer = _ChunkEnhancer()
    chunks = [MDChunk(chunk_id="c0", content="kept"), MDChunk(chunk_id="c1", content=MARKER)]

    processor._enhance_chunks_metadata(chunks, "document", {"source": "unit"})

    assert chunks[0].metadata["semantic_tags"] == ["kept"]
    assert "semantic_tags" not in chunks[1].metadata
    assert MARKER not in str(chunks[0].metadata)
    assert MARKER not in str(chunks[1].metadata)
    _assert_hidden(caplog, "aiops.md_processor", "块 1", "元数据增强失败")

    caplog.clear()
    chunk = MDChunk(chunk_id="c2", content="weight")
    processor._adjust_weights_from_enhancement(chunk, _DepthBoom())
    assert chunk.semantic_weight == 1.0
    assert chunk.structural_weight == 1.0
    _assert_hidden(caplog, "aiops.md_processor", "权重调整失败")


def _retriever() -> HierarchicalRetriever:
    store = Mock()
    store.collection_name = "docs"
    store.client = Mock()
    store.embedding_model = Mock()
    return HierarchicalRetriever(store)


async def test_hierarchical_search_failure_returns_empty_without_exception_text(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="aiops.hierarchical_retriever")
    retriever = _retriever()
    retriever.vector_store.embedding_model.embed_query.side_effect = RuntimeError(MARKER)

    assert await retriever.hierarchical_search("how to restart a deployment") == []
    _assert_hidden(caplog, "aiops.hierarchical_retriever", "层次化检索失败")


async def test_vector_search_failure_keeps_coarse_hits_and_hides_exception_text(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="aiops.hierarchical_retriever")
    retriever = _retriever()
    retriever.vector_store.similarity_search = AsyncMock(side_effect=RuntimeError(MARKER))
    kept = Document(page_content="coarse hit", metadata={"chunk_id": "coarse-1"})
    context = RetrievalContext(
        query="restart",
        query_embedding=np.array([1.0, 0.0], dtype=np.float32),
        query_complexity=QueryComplexity.SIMPLE,
    )
    coarse = StageResult(
        stage=RetrievalStage.COARSE,
        documents=[(kept, 0.5)],
        processing_time=0.0,
        candidates_count=1,
    )

    result = await retriever._fine_retrieval(context, coarse, 2)

    assert result.documents
    assert MARKER not in repr(result)
    assert result.documents[0][0].metadata["chunk_id"] == "coarse-1"
    _assert_hidden(caplog, "aiops.hierarchical_retriever", "向量搜索失败")


async def test_document_embedding_failure_returns_none_without_exception_text(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="aiops.hierarchical_retriever")
    retriever = _retriever()
    retriever.vector_store.client.get.side_effect = RuntimeError(MARKER)
    document = Document(page_content=MARKER, metadata={"chunk_id": "embed-1"})

    assert await retriever._get_document_embedding(document) is None
    _assert_hidden(caplog, "aiops.hierarchical_retriever", "获取文档嵌入失败")


def _enable_debug(monkeypatch) -> None:
    monkeypatch.setattr(config, "debug", True)


def test_ready_route_stays_503_and_logs_type_only(monkeypatch, caplog) -> None:
    _enable_debug(monkeypatch)
    service = make_service(mock_k8s())
    monkeypatch.setattr(service, "initialize", AsyncMock(side_effect=RuntimeError(MARKER)))
    client = make_app(monkeypatch, service)

    with caplog.at_level(logging.DEBUG, logger="aiops"):
        response = client.get("/autofix/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["data"]["error_code"] == "SERVICE_UNAVAILABLE"
    assert body["data"]["detail"] == "服务 autofix 当前不可用"
    assert MARKER not in response.text
    _assert_hidden(caplog, "aiops.api.autofix", "自动修复就绪检查失败")


def test_repair_route_stays_500_and_logs_type_only(monkeypatch, caplog) -> None:
    _enable_debug(monkeypatch)
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    service = make_service(mock_k8s())
    monkeypatch.setattr(service, "fix_kubernetes_deployment", AsyncMock(side_effect=RuntimeError(MARKER)))
    client = make_app(monkeypatch, service)

    with caplog.at_level(logging.DEBUG, logger="aiops"):
        response = client.post("/autofix/repair", json=REPAIR_BODY)

    body = response.json()
    assert response.status_code == 500
    assert body["data"]["error_code"] == "AUTOFIX_INTERNAL_ERROR"
    assert body["data"]["detail"] == "自动修复内部错误: 修复操作失败"
    assert set(body["data"]) >= {"error_code", "detail"}
    assert MARKER not in response.text
    _assert_hidden(caplog, "aiops.api.autofix", "自动修复失败", "api")


def test_repair_notification_debug_drops_exception_text_and_stack(monkeypatch, caplog) -> None:
    _enable_debug(monkeypatch)
    monkeypatch.setattr(config.autofix, "repair_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["default"])
    service = make_service(mock_k8s())
    monkeypatch.setattr(
        service,
        "fix_kubernetes_deployment",
        AsyncMock(return_value={"success": True, "status": "planned", "actions_taken": []}),
    )
    monkeypatch.setattr(autofix_routes, "get_notification_service", AsyncMock(side_effect=RuntimeError(MARKER)))
    client = make_app(monkeypatch, service)

    with caplog.at_level(logging.DEBUG, logger="aiops"):
        response = client.post("/autofix/repair", json={**REPAIR_BODY, "auto_restart": True})

    body = response.json()
    assert response.status_code == 200
    assert body["data"]["deployment"] == "api"
    assert body["data"]["namespace"] == "default"
    assert "success" in body["data"]
    assert "actions_taken" in body["data"]
    assert MARKER not in response.text
    _assert_hidden(caplog, "aiops.api.autofix", "添加通知任务失败")


def test_bootstrap_route_stays_500_and_logs_type_only(monkeypatch, caplog) -> None:
    _enable_debug(monkeypatch)
    monkeypatch.setattr(config.autofix, "bootstrap_enabled", True)
    monkeypatch.setattr(config.autofix, "allowed_namespaces", ["test"])
    service = make_service(mock_k8s())
    monkeypatch.setattr(service, "initialize", AsyncMock(side_effect=RuntimeError(MARKER)))
    client = make_app(monkeypatch, service)

    with caplog.at_level(logging.DEBUG, logger="aiops"):
        response = client.post("/autofix/bootstrap")

    body = response.json()
    assert response.status_code == 500
    assert body["data"]["error_code"] == "AUTOFIX_INTERNAL_ERROR"
    assert body["data"]["detail"] == "自动修复内部错误: 引导故障资源失败"
    assert MARKER not in response.text
    _assert_hidden(caplog, "aiops.api.autofix", "引导故障资源失败")


def test_diagnose_route_stays_500_and_logs_type_only(monkeypatch, caplog) -> None:
    _enable_debug(monkeypatch)
    service = make_service(mock_k8s())
    monkeypatch.setattr(service, "initialize", AsyncMock())
    monkeypatch.setattr(service, "diagnose_kubernetes_issues", AsyncMock(side_effect=RuntimeError(MARKER)))
    client = make_app(monkeypatch, service)

    with caplog.at_level(logging.DEBUG, logger="aiops"):
        response = client.post("/autofix/diagnose", json={"deployment": "api", "namespace": "default"})

    body = response.json()
    assert response.status_code == 500
    assert body["data"]["error_code"] == "AUTOFIX_INTERNAL_ERROR"
    assert body["data"]["detail"] == "自动修复内部错误: 诊断操作失败"
    assert MARKER not in response.text
    _assert_hidden(caplog, "aiops.api.autofix", "诊断失败", "api")
