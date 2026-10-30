"""真实刷新路径的线程读取、预算、取消与stage采样后身份复核。"""

import asyncio
import os
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.core.agents.enterprise_assistant import RAGAssistant
from app.utils import knowledge_snapshot as snapshot


class Stage:
    def __init__(self):
        self.docs = []

    async def add_documents(self, docs):
        self.docs.extend(doc.page_content for doc in docs)
        return [f"doc-{index}" for index in range(len(docs))]

    async def add_md_documents(self, contents, _metadata):
        self.docs.extend(contents)
        return [f"md-{index}" for index in range(len(contents))]


class Store:
    def __init__(self):
        self.docs = ["old-active-generation"]
        self.version = 0
        self.source_version = None
        self.stage = Stage()
        self.creates = 0
        self.publishes = 0
        self.discards = 0
        self.after_capture = None

    async def create_refresh_stage(self):
        self.creates += 1
        self.source_version = self.version
        if self.after_capture:
            self.after_capture()
        return self.stage

    async def publish_refresh_stage(self, stage):
        assert self.source_version == self.version
        self.publishes += 1
        self.docs = list(stage.docs)

    async def discard_refresh_stage(self, stage):
        assert stage is self.stage
        self.discards += 1


def assistant(directory, **limits):
    rag = object.__new__(RAGAssistant)
    rag.config = SimpleNamespace(rag=SimpleNamespace(knowledge_base_path=str(directory), **limits))
    rag.vector_store = Store()
    rag.clear_cache = AsyncMock(return_value={"success": True})
    return rag


def blocked_file_read(monkeypatch, file):
    inode = file.stat().st_ino
    entered, release, finished = Event(), Event(), Event()
    original = os.read

    def read(fd, count):
        if os.fstat(fd).st_ino == inode and not entered.is_set():
            entered.set()
            try:
                if not release.wait(timeout=3):
                    raise RuntimeError("self-owned test read barrier timed out")
            finally:
                finished.set()
        return original(fd, count)

    monkeypatch.setattr(snapshot.os, "read", read)
    return entered, release, finished


async def test_slow_file_io_does_not_starve_event_loop_or_refresh_heartbeat(tmp_path, monkeypatch):
    file = tmp_path / "safe.txt"
    file.write_text("new-content")
    entered, release, _ = blocked_file_read(monkeypatch, file)
    rag = assistant(tmp_path)
    task = asyncio.create_task(rag.refresh_knowledge_base())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 2)
        # While the actual file read is blocked, a heartbeat/timer still runs.
        assert await asyncio.wait_for(asyncio.sleep(0.01, result="heartbeat"), 1) == "heartbeat"
        assert not task.done()
        assert not release.is_set()
        assert rag.vector_store.creates == 0
        release.set()
        assert (await task)["success"] is True
        assert rag.vector_store.docs == ["new-content"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_preflight_reader_can_finish_but_never_create_stage_or_publish(tmp_path, monkeypatch):
    file = tmp_path / "safe.txt"
    file.write_text("new-content")
    entered, release, _ = blocked_file_read(monkeypatch, file)
    import app.core.agents.enterprise_assistant as module

    finished = Event()
    original = module.read_knowledge_snapshot

    def tracked(*args):
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(module, "read_knowledge_snapshot", tracked)
    rag = assistant(tmp_path)
    task = asyncio.create_task(rag.refresh_knowledge_base())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rag.vector_store.creates == rag.vector_store.publishes == rag.vector_store.discards == 0
        release.set()
        assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 2), 2)
        await asyncio.sleep(0)
        assert rag.vector_store.docs == ["old-active-generation"]
        assert rag.vector_store.stage.docs == []
        rag.clear_cache.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failure", ["files", "bytes", "entries", "utf8", "read"])
async def test_preflight_failures_have_no_stage_or_model_side_effects(tmp_path, monkeypatch, caplog, failure):
    (tmp_path / "first.txt").write_bytes(b"aa")
    (tmp_path / "second.txt").write_bytes(b"bb")
    limits = {}
    if failure == "files":
        limits["max_refresh_files"] = 1
    elif failure == "bytes":
        limits["max_refresh_total_bytes"] = 3
    elif failure == "entries":
        monkeypatch.setattr(snapshot, "MAX_DIRECTORY_ENTRIES", 2)
        (tmp_path / "ignored.bin").write_bytes(b"ignored")
    elif failure == "utf8":
        (tmp_path / "second.txt").write_bytes(b"\xffprivate-content-canary")
    elif failure == "read":
        original = os.read
        inode = (tmp_path / "first.txt").stat().st_ino

        def fail(fd, count):
            if os.fstat(fd).st_ino == inode:
                raise OSError("private-read-canary")
            return original(fd, count)

        monkeypatch.setattr(snapshot.os, "read", fail)
    rag = assistant(tmp_path, **limits)
    result = await rag.refresh_knowledge_base()
    assert result["success"] is False
    assert rag.vector_store.docs == ["old-active-generation"]
    assert rag.vector_store.creates == rag.vector_store.publishes == rag.vector_store.discards == 0
    assert rag.vector_store.stage.docs == []
    assert "private-" not in str(result) + caplog.text


@pytest.mark.parametrize("mutation", ["new-file", "changed-file", "empty-file-becomes-content", "symlink"])
async def test_stage_capture_revalidates_preflight_before_any_index_build(tmp_path, mutation):
    file = tmp_path / "safe.txt"
    file.write_text("before")
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    rag = assistant(tmp_path)

    def mutate():
        if mutation == "new-file":
            (tmp_path / "new.txt").write_text("after")
        elif mutation == "changed-file":
            file.write_text("after")
        elif mutation == "empty-file-becomes-content":
            empty.write_text("after")
        else:
            file.unlink()
            file.symlink_to(empty)

    rag.vector_store.after_capture = mutate
    result = await rag.refresh_knowledge_base()
    assert result["success"] is False
    assert rag.vector_store.creates == rag.vector_store.discards == 1
    assert rag.vector_store.publishes == 0
    assert rag.vector_store.stage.docs == []
    assert rag.vector_store.docs == ["old-active-generation"]


async def test_upload_during_preflight_cannot_be_overwritten_by_old_file_snapshot(tmp_path, monkeypatch):
    import app.core.agents.enterprise_assistant as module

    (tmp_path / "safe.txt").write_text("old snapshot")
    rag = assistant(tmp_path)
    read = module.read_knowledge_snapshot

    def concurrent_upload(*args):
        result = read(*args)
        (tmp_path / "uploaded.txt").write_text("new uploaded content")
        rag.vector_store.docs = ["committed concurrent upload"]
        rag.vector_store.version += 1
        return result

    monkeypatch.setattr(module, "read_knowledge_snapshot", concurrent_upload)
    result = await rag.refresh_knowledge_base()
    assert result["success"] is False
    assert rag.vector_store.docs == ["committed concurrent upload"]
    assert rag.vector_store.publishes == 0
    assert rag.vector_store.stage.docs == []


async def test_exact_byte_and_file_budget_accepts_complete_snapshot(tmp_path):
    (tmp_path / "a.txt").write_text("中", encoding="utf-8")
    (tmp_path / "b.txt").write_bytes(b"x")
    rag = assistant(tmp_path, max_refresh_files=2, max_refresh_total_bytes=4)
    result = await rag.refresh_knowledge_base()
    assert result["success"] is True
    assert rag.vector_store.docs == ["中", "x"]


@pytest.mark.parametrize("files,byte_count", [(0, 1), (1025, 1), (True, 1), (1, 0), (1, 256 * 1024 * 1024 + 1), (1, "32")])
async def test_invalid_runtime_budget_fails_closed_before_files_or_stage(tmp_path, files, byte_count):
    (tmp_path / "a.txt").write_text("safe")
    rag = assistant(tmp_path, max_refresh_files=files, max_refresh_total_bytes=byte_count)
    assert (await rag.refresh_knowledge_base())["success"] is False
    assert rag.vector_store.creates == 0


def test_settings_read_yaml_then_explicit_environment_limits(monkeypatch):
    monkeypatch.delenv("RAG_MAX_REFRESH_FILES", raising=False)
    monkeypatch.delenv("RAG_MAX_REFRESH_TOTAL_BYTES", raising=False)
    monkeypatch.setattr(settings, "CONFIG", {"rag": {"max_refresh_files": 2, "max_refresh_total_bytes": 100}})
    yaml = settings.RAGConfig()
    assert (yaml.max_refresh_files, yaml.max_refresh_total_bytes) == (2, 100)
    monkeypatch.setenv("RAG_MAX_REFRESH_FILES", "3")
    monkeypatch.setenv("RAG_MAX_REFRESH_TOTAL_BYTES", "200")
    env = settings.RAGConfig()
    assert (env.max_refresh_files, env.max_refresh_total_bytes) == (3, 200)


@pytest.mark.parametrize("key,value", [("RAG_MAX_REFRESH_FILES", "0"), ("RAG_MAX_REFRESH_FILES", "1025"), ("RAG_MAX_REFRESH_TOTAL_BYTES", "0"), ("RAG_MAX_REFRESH_TOTAL_BYTES", "268435457")])
def test_invalid_explicit_settings_rejected_instead_of_falling_back(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        settings.RAGConfig()


async def test_cancelled_post_stage_verification_does_not_index_or_publish(tmp_path, monkeypatch):
    import app.core.agents.enterprise_assistant as module

    (tmp_path / "a.txt").write_text("new content")
    rag = assistant(tmp_path)
    entered, release, finished = Event(), Event(), Event()
    verify = module.verify_knowledge_snapshot

    def blocked(*args):
        entered.set()
        try:
            if not release.wait(timeout=3):
                raise RuntimeError("self-owned verification barrier timed out")
            return verify(*args)
        finally:
            finished.set()

    monkeypatch.setattr(module, "verify_knowledge_snapshot", blocked)
    task = asyncio.create_task(rag.refresh_knowledge_base())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 2), 2)
        assert rag.vector_store.creates == rag.vector_store.discards == 1
        assert rag.vector_store.publishes == 0
        assert rag.vector_store.docs == ["old-active-generation"]
        assert rag.vector_store.stage.docs == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_directory_replacement_after_stage_capture_is_rejected(tmp_path):
    directory = tmp_path / "knowledge"
    directory.mkdir()
    (directory / "a.txt").write_text("before")
    rag = assistant(directory)

    def replace_root():
        directory.rename(tmp_path / "previous")
        directory.mkdir()
        (directory / "a.txt").write_text("after")

    rag.vector_store.after_capture = replace_root
    assert (await rag.refresh_knowledge_base())["success"] is False
    assert rag.vector_store.creates == rag.vector_store.discards == 1
    assert rag.vector_store.publishes == 0
    assert rag.vector_store.stage.docs == []


async def test_hardlinked_file_is_rejected_before_stage_creation(tmp_path):
    directory = tmp_path / "knowledge"
    directory.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private outside content")
    os.link(outside, directory / "linked.txt")
    rag = assistant(directory)
    assert (await rag.refresh_knowledge_base())["success"] is False
    assert rag.vector_store.creates == 0
    assert rag.vector_store.docs == ["old-active-generation"]


async def test_stable_trusted_configured_root_link_remains_supported(tmp_path):
    directory = tmp_path / "knowledge"
    directory.mkdir()
    (directory / "a.txt").write_text("safe content")
    configured = tmp_path / "configured-link"
    configured.symlink_to(directory, target_is_directory=True)
    rag = assistant(configured)
    assert (await rag.refresh_knowledge_base())["success"] is True
    assert rag.vector_store.docs == ["safe content"]


async def test_direct_file_mutation_during_build_is_rejected_before_publish(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("before")
    rag = assistant(tmp_path)
    original = rag.vector_store.stage.add_documents

    async def changed(docs):
        ids = await original(docs)
        file.write_text("changed during model build")
        return ids

    rag.vector_store.stage.add_documents = changed
    assert (await rag.refresh_knowledge_base())["success"] is False
    assert rag.vector_store.publishes == 0
    assert rag.vector_store.docs == ["old-active-generation"]
    assert rag.vector_store.discards == 1


@pytest.mark.parametrize("value", [True, False, 1.5, "1.5", "", "private-invalid-canary"])
def test_invalid_yaml_budget_types_never_coerce_to_valid_integers(monkeypatch, value):
    monkeypatch.delenv("RAG_MAX_REFRESH_FILES", raising=False)
    monkeypatch.setattr(settings, "CONFIG", {"rag": {"max_refresh_files": value}})
    with pytest.raises(ValueError, match="预算必须为整数"):
        settings.RAGConfig()
