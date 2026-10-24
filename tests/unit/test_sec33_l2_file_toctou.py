#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SEC-33 L2 P3-2：路径校验后把文件/中间目录换成指向 MCP_FILE_ROOT 外的符号链接，文件工具必须拒绝且不泄露内容"""

import os

import pytest

from app.mcp.server.tools import file_tool
from app.mcp.server.tools.file_tool import FileListTool, FileReadTool, FileStatsTool
from app.utils import file_boundary

MARKER = "sec33-l2-P32-OUTSIDE-MARKER-55120"


@pytest.fixture
def tree(monkeypatch, tmp_path):
    base = tmp_path / "kb"
    (base / "sub").mkdir(parents=True)
    (base / "a.md").write_text("inside\n")
    (base / "sub" / "a.md").write_text("inside-sub\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.md").write_text(MARKER * 7 + "\n")
    (outside / "big.bin").write_bytes(b"z" * 9871)
    monkeypatch.setenv("MCP_FILE_ROOT", str(base))
    return base, outside


def swap_after_validation(monkeypatch, swap):
    """包装 resolve_tool_path：校验通过、返回之前执行 swap（模拟并发替换）"""
    original = file_tool.resolve_tool_path

    def wrapped(value):
        resolved = original(value)
        swap()
        return resolved

    monkeypatch.setattr(file_tool, "resolve_tool_path", wrapped)


def replace_with_link(path, target):
    os.rename(path, str(path) + ".orig")
    os.symlink(target, path)


async def test_read_file_swapped_to_symlink_after_validation_is_refused(tree, monkeypatch):
    base, outside = tree
    swap_after_validation(monkeypatch, lambda: replace_with_link(base / "a.md", outside / "a.md"))
    with pytest.raises(RuntimeError) as failed:
        await FileReadTool().execute({"file_path": "a.md"})
    assert "校验后发生变化" in str(failed.value)
    assert MARKER not in str(failed.value) and str(outside) not in str(failed.value)


async def test_read_file_intermediate_dir_swapped_to_symlink_is_refused(tree, monkeypatch):
    base, outside = tree
    swap_after_validation(monkeypatch, lambda: replace_with_link(base / "sub", outside))
    with pytest.raises(RuntimeError) as failed:
        await FileReadTool().execute({"file_path": "sub/a.md"})
    assert "校验后发生变化" in str(failed.value)
    assert MARKER not in str(failed.value)


async def test_read_file_replaced_by_other_inode_is_refused(tree, monkeypatch):
    base, outside = tree
    staged = base / "staged.md"
    staged.write_text(MARKER + "\n")
    original = file_tool.validated_identity

    def wrapped(path):
        identity = original(path)
        os.replace(staged, base / "a.md")  # 同名普通文件被替换：O_NOFOLLOW 拦不住，靠 dev/ino 比对
        return identity

    monkeypatch.setattr(file_tool, "validated_identity", wrapped)
    with pytest.raises(RuntimeError) as failed:
        await FileReadTool().execute({"file_path": "a.md"})
    assert "校验后发生变化" in str(failed.value)
    assert MARKER not in str(failed.value)


async def test_list_directory_swapped_to_symlink_is_refused(tree, monkeypatch):
    base, outside = tree
    swap_after_validation(monkeypatch, lambda: replace_with_link(base / "sub", outside))
    with pytest.raises(RuntimeError) as failed:
        await FileListTool().execute({"directory_path": "sub"})
    assert "校验后发生变化" in str(failed.value)
    assert "big.bin" not in str(failed.value)


async def test_get_file_stats_swapped_to_symlink_is_refused(tree, monkeypatch):
    base, outside = tree
    swap_after_validation(monkeypatch, lambda: replace_with_link(base / "sub", outside))
    with pytest.raises(RuntimeError) as failed:
        await FileStatsTool().execute({"path": "sub"})
    assert "校验后发生变化" in str(failed.value)
    assert "9871" not in str(failed.value)


async def test_get_file_stats_file_swapped_to_symlink_is_refused(tree, monkeypatch):
    base, outside = tree
    swap_after_validation(monkeypatch, lambda: replace_with_link(base / "a.md", outside / "big.bin"))
    with pytest.raises(RuntimeError) as failed:
        await FileStatsTool().execute({"path": "a.md"})
    assert "校验后发生变化" in str(failed.value)
    assert "9871" not in str(failed.value)


def test_validated_identity_refuses_symlink(tree):
    base, outside = tree
    (base / "link").symlink_to(outside / "a.md")
    with pytest.raises(file_boundary.PathChangedError):
        file_boundary.validated_identity(str(base / "link"))
    assert file_boundary.validated_identity(str(base / "a.md")).st_ino == os.lstat(base / "a.md").st_ino


def test_open_in_root_uses_nofollow_walk(tree):
    base, outside = tree
    (base / "link").symlink_to(outside / "a.md")
    with pytest.raises(file_boundary.PathChangedError):
        file_boundary.open_in_root(str(base / "link"))
    fd = file_boundary.open_in_root(str(base / "sub" / "a.md"))
    try:
        assert os.read(fd, 100) == b"inside-sub\n"
    finally:
        os.close(fd)


async def test_unswapped_paths_still_work(tree):
    base, _ = tree
    assert (await FileReadTool().execute({"file_path": "sub/a.md"}))["content"] == "inside-sub\n"
    listing = await FileListTool().execute({"directory_path": "sub"})
    assert [item["name"] for item in listing["items"]] == ["a.md"]
    stats = await FileStatsTool().execute({"path": "."})
    assert stats["total_files"] == 2 and stats["total_size_bytes"] == len("inside\n") + len("inside-sub\n")


# ---------------------------------------------------------------- SEC-33 L3 P3-K：评审变异 #23 / #26 / #27 / #32 的正式回归测试


def _finishes(target, timeout=5.0, unblock=None):
    import threading

    outcome = {}

    def run():
        try:
            outcome["value"] = target()
        except BaseException as error:  # noqa: BLE001 - 记录给断言
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    hung = thread.is_alive()
    if hung and unblock is not None:
        unblock()
        thread.join(timeout)
    return hung, outcome


def _unblock_fifo(path):
    def unblock():
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)

    return unblock


def test_open_in_root_does_not_hang_on_fifo(tree):
    """变异 #23：去掉 O_NONBLOCK 后打开 FIFO 会一直阻塞（没有写端）"""
    base, _ = tree
    fifo = base / "pipe"
    os.mkfifo(fifo)

    def open_and_close():
        os.close(file_boundary.open_in_root(str(fifo)))
        return "opened"

    hung, outcome = _finishes(open_and_close, unblock=_unblock_fifo(fifo))
    assert not hung, "open_in_root blocked on a FIFO"
    assert outcome.get("value") == "opened"


async def test_list_directory_symlink_to_fifo_does_not_hang(tree):
    """变异 #23（经 list_directory）：root 内指向 FIFO 的符号链接会被 _entry_stat_at 跟随打开"""
    import asyncio

    base, _ = tree
    fifo = base / "pipe"
    os.mkfifo(fifo)
    (base / "pipe-link").symlink_to(fifo)
    hung, outcome = _finishes(lambda: asyncio.run(FileListTool().execute({"directory_path": "."})), unblock=_unblock_fifo(fifo))
    assert not hung, "list_directory blocked on a FIFO"
    names = {item["name"] for item in outcome["value"]["items"]}
    assert {"pipe", "pipe-link"} <= names


async def test_read_file_rejects_non_regular_after_fstat(tree, monkeypatch):
    """变异 #26：去掉 fstat 后的 S_ISREG 检查。isfile 被绕过（模拟校验后类型变化）时必须按 fstat 拒绝"""
    base, _ = tree
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda path: True if str(path).endswith("/sub") else real_isfile(path))
    with pytest.raises(RuntimeError) as failed:
        await FileReadTool().execute({"file_path": "sub"})
    assert "路径不是文件: sub" in str(failed.value)


async def test_read_file_size_limit_is_exactly_one_mebibyte(tree):
    """变异 #27：1 MiB 上限被放宽（或差一）"""
    base, _ = tree
    (base / "exact.txt").write_bytes(b"a" * (1024 * 1024))
    (base / "over.txt").write_bytes(b"a" * (1024 * 1024 + 1))
    exact = await FileReadTool().execute({"file_path": "exact.txt"})
    assert exact["size_bytes"] == 1024 * 1024
    with pytest.raises(RuntimeError) as failed:
        await FileReadTool().execute({"file_path": "over.txt"})
    assert "1 MiB" in str(failed.value)


async def test_list_directory_symlink_target_swapped_after_check_is_not_followed(tree, monkeypatch):
    """变异 #32：_entry_stat_at 改用 os.open(target) 跟随符号链接。
    链接目标的 realpath 在 root 内通过检查后，中间目录被换成指向 root 外的符号链接：必须按 lstat 记为 symlink，不泄露外部文件大小"""
    base, outside = tree
    (base / "inner").mkdir()
    (base / "inner" / "big.bin").write_bytes(b"i")
    (base / "link").symlink_to(base / "inner" / "big.bin")
    real_exists = os.path.exists
    swapped = []

    def exists_then_swap(path):
        result = real_exists(path)
        if not swapped and str(path) == str(base / "inner" / "big.bin"):
            swapped.append(True)
            os.rename(base / "inner", base / "inner.orig")
            os.symlink(outside, base / "inner")
        return result

    monkeypatch.setattr(file_tool.os.path, "exists", exists_then_swap)
    result = await FileListTool().execute({"directory_path": "."})
    assert swapped
    entry = next(item for item in result["items"] if item["name"] == "link")
    assert entry["type"] == "symlink" and entry["size_bytes"] == 0
    assert all(item["size_bytes"] != 9871 for item in result["items"])
