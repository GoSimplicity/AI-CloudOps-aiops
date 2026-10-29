"""知识文件不能覆盖现有文件或跟随并发替换的符号链接"""
import os
import subprocess
import sys

import pytest

from app.common.exceptions import KnowledgeWriteError, ValidationError
from app.services.assistant_service import OptimizedAssistantService


def test_concurrent_processes_never_overwrite_same_name(tmp_path):
    program = '''
import sys
from app.services.assistant_service import OptimizedAssistantService
sys.stdin.read(1)
name, _ = OptimizedAssistantService()._save_knowledge_file(sys.argv[1], "same.txt", sys.argv[2])
print(name)
'''
    env = {**os.environ, "ENV": "test", "PYTHON_DOTENV_DISABLED": "1", "LLM_API_KEY": ""}
    children = [subprocess.Popen([sys.executable, "-c", program, str(tmp_path), f"content-{i}"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=env) for i in range(4)]
    try:
        for child in children:
            child.stdin.write("x")
            child.stdin.close()
            child.stdin = None
        names = []
        for child in children:
            output, error = child.communicate(timeout=30)
            assert child.returncode == 0, error
            names.append(output.strip().splitlines()[-1])
        assert len(set(names)) == 4
        assert {item.read_text() for item in tmp_path.iterdir()} == {f"content-{i}" for i in range(4)}
        assert not list(tmp_path.glob(".cloudops-upload-*"))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_symlink_inserted_before_atomic_link_never_changes_target(tmp_path, monkeypatch):
    directory = tmp_path / "knowledge"
    directory.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("untouched")
    original = os.link
    inserted = False
    def race(source, destination, **kwargs):
        nonlocal inserted
        if not inserted:
            inserted = True
            (directory / destination).symlink_to(target)
        return original(source, destination, **kwargs)
    monkeypatch.setattr(os, "link", race)
    with pytest.raises(ValidationError):
        OptimizedAssistantService()._save_knowledge_file(str(directory), "same.txt", "new")
    assert target.read_text() == "untouched"
    assert (directory / "same.txt").is_symlink()
    assert not list(directory.glob(".cloudops-upload-*"))


def test_root_directory_replaced_does_not_write_to_other_directory(tmp_path, monkeypatch):
    directory = tmp_path / "knowledge"
    outside = tmp_path / "outside"
    moved = tmp_path / "moved"
    directory.mkdir()
    outside.mkdir()
    original = os.link
    def race(source, destination, **kwargs):
        directory.rename(moved)
        directory.symlink_to(outside, target_is_directory=True)
        return original(source, destination, **kwargs)
    monkeypatch.setattr(os, "link", race)
    with pytest.raises(KnowledgeWriteError) as caught:
        OptimizedAssistantService()._save_knowledge_file(str(directory), "same.txt", "new")
    assert caught.value.details == {"published": False, "outcome_unknown": True, "retry_safe": False, "file_saved": None}
    assert list(outside.iterdir()) == []
    assert (moved / "same.txt").read_text() == "new"
    assert not list(moved.glob(".cloudops-upload-*"))


def test_partial_temporary_write_is_removed_without_touching_existing_file(tmp_path, monkeypatch):
    existing = tmp_path / "existing.txt"
    existing.write_text("untouched")
    real_write = os.write
    calls = 0
    def failing_write(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:1])
        raise OSError("controlled write failure")
    monkeypatch.setattr(os, "write", failing_write)
    with pytest.raises(OSError):
        OptimizedAssistantService()._save_knowledge_file(str(tmp_path), "new.txt", "hello")
    assert existing.read_text() == "untouched"
    assert not (tmp_path / "new.txt").exists()
    assert not list(tmp_path.glob(".cloudops-upload-*"))


async def test_configured_root_symlink_supports_upload_and_refresh(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.core.agents.enterprise_assistant import RAGAssistant

    real = tmp_path / "actual"
    real.mkdir()
    configured = tmp_path / "knowledge-link"
    configured.symlink_to(real, target_is_directory=True)
    name, path = OptimizedAssistantService()._save_knowledge_file(str(configured), "sample.txt", "safe content")
    assert name == "sample.txt"
    assert path == str(configured / "sample.txt")
    assert (real / name).read_text() == "safe content"
    stage = SimpleNamespace(add_documents=AsyncMock(return_value=["doc"]))
    store = SimpleNamespace(create_refresh_stage=AsyncMock(return_value=stage), publish_refresh_stage=AsyncMock(), discard_refresh_stage=AsyncMock())
    rag = object.__new__(RAGAssistant)
    rag.config = SimpleNamespace(rag=SimpleNamespace(knowledge_base_path=str(configured)))
    rag.vector_store = store
    rag.clear_cache = AsyncMock(return_value={"success": True})
    result = await rag.refresh_knowledge_base()
    assert result["success"] is True
    assert stage.add_documents.await_args.args[0][0].page_content == "safe content"


def test_configured_root_symlink_swapped_during_write_is_rejected(tmp_path, monkeypatch):
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    configured = tmp_path / "knowledge-link"
    original.mkdir()
    replacement.mkdir()
    configured.symlink_to(original, target_is_directory=True)
    real_link = os.link
    def race(source, destination, **kwargs):
        configured.unlink()
        configured.symlink_to(replacement, target_is_directory=True)
        return real_link(source, destination, **kwargs)
    monkeypatch.setattr(os, "link", race)
    with pytest.raises(KnowledgeWriteError) as failure:
        OptimizedAssistantService()._save_knowledge_file(str(configured), "sample.txt", "safe")
    assert failure.value.details["outcome_unknown"] is True
    assert list(replacement.iterdir()) == []
    assert (original / "sample.txt").read_text() == "safe"
