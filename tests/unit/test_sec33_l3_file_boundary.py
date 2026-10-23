#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SEC-33 L3：文件边界加固（P3-a O_NOFOLLOW 缺失降级、P3-b 非目录 stats 走父目录 fd、P3-B nlink 与固定 root fd）"""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.mcp.server.tools.file_tool import FileListTool, FileReadTool, FileStatsTool
from app.utils import file_boundary

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def root(monkeypatch, tmp_path):
    base = tmp_path / "kb"
    (base / "sub").mkdir(parents=True)
    (base / "a.md").write_text("inside\n")
    monkeypatch.setenv("MCP_FILE_ROOT", str(base))
    file_boundary.reset_file_root_cache()
    return base


# ---------------------------------------------------------------- P3-a：平台缺少 O_NOFOLLOW


@pytest.mark.parametrize(
    "tool, params",
    [
        (FileReadTool, {"file_path": "a.md"}),
        (FileListTool, {"directory_path": "."}),
        (FileStatsTool, {"path": "a.md"}),
        (FileStatsTool, {"path": "sub"}),
    ],
)
async def test_file_tools_refuse_when_o_nofollow_is_missing(root, monkeypatch, tool, params):
    monkeypatch.delattr(os, "O_NOFOLLOW")
    with pytest.raises(RuntimeError) as failed:
        await tool().execute(params)
    assert "O_NOFOLLOW" in str(failed.value)
    assert str(root) not in str(failed.value)


def test_open_in_root_refuses_without_o_nofollow(root, monkeypatch):
    monkeypatch.delattr(os, "O_NOFOLLOW")
    with pytest.raises(file_boundary.NoFollowUnsupportedError):
        file_boundary.open_in_root(str(root / "a.md"))


def test_missing_o_nofollow_does_not_break_import_or_startup_check(tmp_path):
    """子进程里删掉 os.O_NOFOLLOW 后再导入：模块导入与 MCP 启动检查（ensure_file_root_safe）不受影响，只有文件工具拒绝"""
    base = tmp_path / "kb"
    base.mkdir()
    (base / "a.md").write_text("x\n")
    script = (
        "import asyncio, os\n"
        "del os.O_NOFOLLOW\n"
        "from app.utils import file_boundary\n"
        "from app.mcp.server.tools.file_tool import FileReadTool\n"
        "file_boundary.ensure_file_root_safe('development')\n"
        "try:\n"
        "    asyncio.run(FileReadTool().execute({'file_path': 'a.md'}))\n"
        "except RuntimeError as error:\n"
        "    print('REFUSED', 'O_NOFOLLOW' in str(error))\n"
        "else:\n"
        "    print('READ')\n"
    )
    env = dict(os.environ, MCP_FILE_ROOT=str(base), PYTHONPATH=str(PROJECT_ROOT))
    done = subprocess.run([sys.executable, "-c", script], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "REFUSED True"


# ---------------------------------------------------------------- P3-b：非目录 stats 走父目录 fd + follow_symlinks=False


def _spy_leaf_opens(monkeypatch, leaf):
    opened = []
    real_open = os.open

    def spy(path, flags, *args, **kwargs):
        if os.fspath(path) == leaf or os.path.basename(os.fspath(path)) == leaf:
            opened.append(path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    return opened


async def test_stats_of_regular_file_never_opens_the_file(root, monkeypatch):
    opened = _spy_leaf_opens(monkeypatch, "a.md")
    result = await FileStatsTool().execute({"path": "a.md"})
    assert result["type"] == "file" and result["size_bytes"] == len("inside\n")
    assert opened == []


async def test_stats_of_fifo_does_not_open_it(root, monkeypatch):
    os.mkfifo(root / "pipe")
    opened = _spy_leaf_opens(monkeypatch, "pipe")
    result = await FileStatsTool().execute({"path": "pipe"})
    assert result["type"] == "file" and result["size_bytes"] == 0
    assert opened == []


async def test_stats_of_unreadable_file_works_without_opening(root):
    target = root / "secret.md"
    target.write_text("abc")
    target.chmod(0)
    try:
        result = await FileStatsTool().execute({"path": "secret.md"})
    finally:
        target.chmod(0o600)
    assert result["size_bytes"] == 3 and result["permissions"] == "000"


async def test_stats_of_unix_socket_works_without_opening(root):
    import socket

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(root / "sock"))
        result = await FileStatsTool().execute({"path": "sock"})
    finally:
        server.close()
    assert result["exists"] is True and result["type"] == "file"


async def test_stats_rejects_file_replaced_by_other_inode(root, monkeypatch):
    from app.mcp.server.tools import file_tool

    staged = root / "staged.md"
    staged.write_text("x" * 4321)
    original = file_tool.validated_identity

    def wrapped(path):
        identity = original(path)
        os.replace(staged, root / "a.md")
        return identity

    monkeypatch.setattr(file_tool, "validated_identity", wrapped)
    with pytest.raises(RuntimeError) as failed:
        await FileStatsTool().execute({"path": "a.md"})
    assert "校验后发生变化" in str(failed.value) and "4321" not in str(failed.value)


async def test_stats_rejects_file_swapped_to_symlink_via_lstat(root, monkeypatch, tmp_path):
    from app.mcp.server.tools import file_tool

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"z" * 7777)
    original = file_tool.validated_identity

    def wrapped(path):
        identity = original(path)
        os.rename(root / "a.md", root / "a.md.orig")
        os.symlink(outside, root / "a.md")
        return identity

    monkeypatch.setattr(file_tool, "validated_identity", wrapped)
    with pytest.raises(RuntimeError) as failed:
        await FileStatsTool().execute({"path": "a.md"})
    assert "校验后发生变化" in str(failed.value) and "7777" not in str(failed.value)


async def test_stats_rejects_parent_swapped_to_symlink(root, monkeypatch, tmp_path):
    from app.mcp.server.tools import file_tool

    (root / "sub" / "b.md").write_text("in")
    outside = tmp_path / "outdir"
    outside.mkdir()
    (outside / "b.md").write_bytes(b"q" * 6543)
    original = file_tool.validated_identity

    def wrapped(path):
        identity = original(path)
        os.rename(root / "sub", root / "sub.orig")
        os.symlink(outside, root / "sub")
        return identity

    monkeypatch.setattr(file_tool, "validated_identity", wrapped)
    with pytest.raises(RuntimeError) as failed:
        await FileStatsTool().execute({"path": "sub/b.md"})
    assert "校验后发生变化" in str(failed.value) and "6543" not in str(failed.value)


async def test_stats_of_directory_still_aggregates(root):
    (root / "sub" / "b.md").write_text("12345")
    result = await FileStatsTool().execute({"path": "sub"})
    assert result["type"] == "directory" and result["total_files"] == 1 and result["total_size_bytes"] == 5


# ---------------------------------------------------------------- P3-B：普通文件 st_nlink==1，root 目录 fd 启动时固定

HARDLINK_MARKER = "sec33-l3-P3B-HARDLINK-MARKER-90417"


@pytest.fixture
def hardlink(root, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text(HARDLINK_MARKER * 3)
    os.link(outside, root / "linked.md")
    return root / "linked.md"


async def test_read_file_rejects_hardlink(hardlink):
    with pytest.raises(RuntimeError) as failed:
        await FileReadTool().execute({"file_path": "linked.md"})
    assert "硬链接" in str(failed.value)
    assert HARDLINK_MARKER not in str(failed.value)


async def test_stats_rejects_hardlink(hardlink):
    with pytest.raises(RuntimeError) as failed:
        await FileStatsTool().execute({"path": "linked.md"})
    assert "硬链接" in str(failed.value)
    assert str(len(HARDLINK_MARKER * 3)) not in str(failed.value)


async def test_list_and_directory_stats_hide_hardlink_size(hardlink, root):
    listed = await FileListTool().execute({"directory_path": "."})
    entry = next(item for item in listed["items"] if item["name"] == "linked.md")
    assert entry["size_bytes"] == 0
    stats = await FileStatsTool().execute({"path": "."})
    assert stats["total_size_bytes"] == len("inside\n")


def test_open_in_root_rejects_hardlinked_regular_file(hardlink):
    with pytest.raises(file_boundary.HardLinkError):
        file_boundary.open_in_root(str(hardlink))


async def test_single_link_files_still_readable(root):
    result = await FileReadTool().execute({"file_path": "a.md"})
    assert result["content"] == "inside\n"


ROOT_SWAP_MARKER = "sec33-l3-P3B-ROOT-SWAP-MARKER-31337"


async def test_root_replaced_after_startup_is_not_followed(root):
    """启动时固定 root 目录 fd：之后 root 路径被换成另一个目录（rename 旧目录 + 新建同名目录），工具不得读到新目录内容"""
    file_boundary.ensure_file_root_safe("development")
    assert (await FileReadTool().execute({"file_path": "a.md"}))["content"] == "inside\n"
    os.rename(root, str(root) + ".old")
    root.mkdir()
    (root / "a.md").write_text(ROOT_SWAP_MARKER)
    try:
        result = await FileReadTool().execute({"file_path": "a.md"})
    except RuntimeError as error:
        assert ROOT_SWAP_MARKER not in str(error)
    else:
        assert ROOT_SWAP_MARKER not in result["content"]
    listed = None
    try:
        listed = await FileListTool().execute({"directory_path": "."})
    except RuntimeError:
        pass
    if listed is not None:
        assert all(item["size_bytes"] != len(ROOT_SWAP_MARKER) for item in listed["items"])


def test_pin_file_root_holds_directory_fd(root):
    file_boundary.pin_file_root()
    fd = file_boundary.pinned_root_fd()
    assert fd is not None
    info = os.fstat(fd)
    assert (info.st_dev, info.st_ino) == (os.stat(root).st_dev, os.stat(root).st_ino)
    file_boundary.reset_file_root_cache()
    with pytest.raises(OSError):
        os.fstat(fd)
