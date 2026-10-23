"""真实ASGI路由必须在有界文件和身份校验后才初始化知识索引"""

from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
import pytest

from app.api.middleware.auth import setup_auth
from app.api.middleware.error_handler import setup_error_handlers
from app.api.routes import assistant
from app.common.exceptions import SessionAccessError
from app.common.identity import principal_scope
from app.config.settings import config
from app.services.assistant_service import OptimizedAssistantService

KEY = "offline-knowledge-boundary-service-key"


@pytest.fixture
def cold_knowledge(monkeypatch, tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "existing.md").write_text("原有文档，非法请求不能触发它的重新嵌入")
    monkeypatch.setattr(config.rag, "knowledge_base_path", str(knowledge))
    monkeypatch.setattr(config.security, "api_key", KEY)
    service = OptimizedAssistantService()
    service.MAX_KNOWLEDGE_FILE_SIZE = 32
    embedding = AsyncMock()
    index = AsyncMock(return_value={"success": True, "document_count": 1})

    async def initialize():
        await embedding((knowledge / "existing.md").read_text())
        service._assistant = SimpleNamespace(upload_knowledge=index)

    initializer = AsyncMock(side_effect=initialize)
    monkeypatch.setattr(service, "_do_initialize", initializer)
    initialize_call = AsyncMock(wraps=service.initialize)
    monkeypatch.setattr(service, "initialize", initialize_call)
    monkeypatch.setattr(assistant, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    setup_auth(app)
    setup_error_handlers(app)
    app.include_router(assistant.router, prefix="/assistant")
    with TestClient(app) as client:
        yield client, service, initialize_call, initializer, embedding, index, knowledge


def headers():
    return {"X-API-Key": KEY, "X-CloudOps-User-ID": "1"}


def assert_rejected_without_dependency(state, response, status=400):
    _, service, initialize, initializer, embedding, index, knowledge = state
    assert response.status_code == status
    assert response.json()["code"] == status
    initialize.assert_not_awaited()
    initializer.assert_not_awaited()
    embedding.assert_not_awaited()
    index.assert_not_awaited()
    assert not service.is_initialized()
    assert [file.name for file in knowledge.iterdir()] == ["existing.md"]


@pytest.mark.parametrize("filename,content", [
    ("too-large.md", b"x" * 33),
    ("word.doc", b"plain text pretending to be Word"),
    ("word.docx", b"zip-less ASCII pretending Word"),
    ("unsupported.pdf", b"pdf text"),
    ("not-utf8.txt", b"\xff"),
    ("empty.md", b""),
    ("empty-bom.md", b"\xef\xbb\xbf"),
    ("nul.txt", b"valid\x00content"),
    ("whitespace.txt", b" \r\n\t"),
    ("../escape.md", b"valid content"),
])
def test_invalid_upload_is_rejected_before_cold_initialization(cold_knowledge, filename, content):
    client, *_ = cold_knowledge
    response = client.post("/assistant/upload-knowledge-file", headers=headers(), files={"file": (filename, content, "text/plain")})
    assert_rejected_without_dependency(cold_knowledge, response)


@pytest.mark.parametrize("payload", [
    {},
    {"title": "x", "content": "   ", "file_name": "empty.md"},
    {"title": 7, "content": "text", "file_name": "guide.md"},
    {"title": 0, "content": "text", "file_name": "guide.md"},
    {"title": "x", "content": "text", "file_name": ""},
    {"content": "text", "metadata": False},
    {"title": "x", "content": ["text"], "file_name": "guide.md"},
    {"title": "x", "content": "x" * 33, "file_name": "large.md"},
    {"title": "x", "content": "text", "file_name": "word.docx"},
    {"title": "x", "content": "text", "file_name": "../escape.md"},
    {"title": "x", "content": "text", "file_name": "bad\n.md"},
    {"title": "x", "content": "中" * 12, "file_name": "utf8-large.md"},
    {"title": "x", "content": "\ud800", "file_name": "invalid-utf8.md"},
    {"title": "x", "content": "text", "file_name": "a" * 256 + ".md"},
    {"content": "text", "metadata": ["invalid object"]},
])
def test_invalid_document_is_rejected_before_cold_initialization(cold_knowledge, payload):
    client, *_ = cold_knowledge
    response = client.post("/assistant/add-document", headers={**headers(), "Content-Type": "application/json"}, content=json.dumps(payload).encode("ascii"))
    assert_rejected_without_dependency(cold_knowledge, response)


@pytest.mark.parametrize("method", ["upload_knowledge_file", "add_document"])
async def test_direct_service_without_principal_cannot_read_or_initialize(cold_knowledge, method):
    _, service, initialize, initializer, embedding, index, _ = cold_knowledge
    file = UploadFile(BytesIO(b"valid content"), filename="guide.md")
    file.read = AsyncMock(wraps=file.read)
    value = file if method == "upload_knowledge_file" else {"title": "x", "content": "text", "file_name": "guide.md"}
    with principal_scope(None), pytest.raises(SessionAccessError) as raised:
        await getattr(service, method)(value)
    assert raised.value.status_code == 401
    file.read.assert_not_awaited()
    initialize.assert_not_awaited()
    initializer.assert_not_awaited()
    embedding.assert_not_awaited()
    index.assert_not_awaited()


@pytest.mark.parametrize("path", ["upload-knowledge-file", "add-document"])
def test_user_header_without_service_auth_never_initializes(cold_knowledge, path):
    client, *_ = cold_knowledge
    if path == "upload-knowledge-file":
        response = client.post("/assistant/" + path, headers={"X-CloudOps-User-ID": "1"}, files={"file": ("guide.md", b"valid")})
    else:
        response = client.post("/assistant/" + path, headers={"X-CloudOps-User-ID": "1"}, json={"content": "valid"})
    assert_rejected_without_dependency(cold_knowledge, response, 401)


@pytest.mark.parametrize("extension", ["md", "txt"])
def test_valid_upload_initializes_then_indexes_once(cold_knowledge, extension):
    client, _, initialize, initializer, embedding, index, knowledge = cold_knowledge
    response = client.post("/assistant/upload-knowledge-file", headers=headers(), files={"file": ("guide." + extension, "有效的UTF-8文档".encode(), "text/plain")})
    assert response.status_code == 200 and response.json()["data"]["uploaded"] is True
    initialize.assert_awaited_once()
    initializer.assert_awaited_once()
    embedding.assert_awaited_once()
    index.assert_awaited_once()
    assert (knowledge / ("guide." + extension)).read_text() == "有效的UTF-8文档"


@pytest.mark.parametrize("method", ["upload", "document"])
def test_symlink_target_is_rejected_before_initialization(cold_knowledge, method):
    client, _, initialize, initializer, embedding, index, knowledge = cold_knowledge
    target = knowledge.parent / "owned-sentinel.txt"
    target.write_text("must remain unchanged")
    (knowledge / "link.md").symlink_to(target)
    if method == "upload":
        response = client.post("/assistant/upload-knowledge-file", headers=headers(), files={"file": ("link.md", b"text")})
    else:
        response = client.post("/assistant/add-document", headers=headers(), json={"title": "x", "content": "text", "file_name": "link.md"})
    assert response.status_code == 400
    assert target.read_text() == "must remain unchanged"
    initialize.assert_not_awaited()
    initializer.assert_not_awaited()
    embedding.assert_not_awaited()
    index.assert_not_awaited()


@pytest.mark.parametrize("extension", ["md", "txt"])
def test_valid_document_initializes_then_indexes_once(cold_knowledge, extension):
    client, _, initialize, initializer, embedding, index, knowledge = cold_knowledge
    response = client.post("/assistant/add-document", headers=headers(), json={"title": "文档", "content": "内容", "file_name": "added." + extension})
    assert response.status_code == 200 and response.json()["data"]["added"] is True
    initialize.assert_awaited_once()
    initializer.assert_awaited_once()
    embedding.assert_awaited_once()
    index.assert_awaited_once()
    assert "内容" in (knowledge / ("added." + extension)).read_text()
