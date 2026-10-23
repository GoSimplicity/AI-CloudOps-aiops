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
