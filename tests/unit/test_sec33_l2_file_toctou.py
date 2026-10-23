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
