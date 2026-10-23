"""上传读取在初始化模型前执行有界校验"""

from io import BytesIO
from unittest.mock import AsyncMock

from fastapi import UploadFile
import pytest

from app.common.exceptions import ValidationError
from app.config.settings import config
from app.services.assistant_service import OptimizedAssistantService


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setattr(config.rag, "knowledge_base_path", str(tmp_path))
    service = OptimizedAssistantService()
    service.MAX_KNOWLEDGE_FILE_SIZE = 32
    service._ensure_ready = AsyncMock()
    return service


async def test_oversized_upload_reads_at_most_limit_plus_one(service, tmp_path):
    upload = UploadFile(BytesIO(b"x" * 1000), filename="guide.md")
    upload.read = AsyncMock(wraps=upload.read)
    with pytest.raises(ValidationError):
        await service.upload_knowledge_file(upload)
    upload.read.assert_awaited_once_with(33)
    service._ensure_ready.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


async def test_upload_at_limit_is_preserved(service, tmp_path):
    upload = UploadFile(BytesIO(b"x" * 32), filename="guide.md")
    result = await service.upload_knowledge_file(upload)
    assert result["file_size"] == 32
    assert (tmp_path / "guide.md").read_bytes() == b"x" * 32
    service._ensure_ready.assert_awaited_once()


async def test_missing_upload_does_not_initialize_models(service):
    with pytest.raises(ValidationError):
        await service.upload_knowledge_file(None)
    service._ensure_ready.assert_not_awaited()
