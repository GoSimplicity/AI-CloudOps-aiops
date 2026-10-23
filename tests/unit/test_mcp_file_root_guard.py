"""SEC-33：MCP_FILE_ROOT 启动检查，以及文件工具报错不暴露 resolve 后的绝对路径"""

import logging
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
import pytest

from app.mcp.server.tools.file_tool import FileListTool, FileReadTool, FileStatsTool
from app.utils import file_boundary
from app.utils.file_boundary import UnsafeFileRootError, ensure_file_root_safe, file_root_violations

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".kube").mkdir(parents=True)
    (home / ".ssh").mkdir()
    (home / "work" / "kb").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def unsafe_roots(home):
    return {
        "/": "/",
        "/etc": "/etc",
        "/etc/ssl": "/etc",
        "/run/secrets/kubernetes.io": "/run/secrets",
        "/var": "/var/lib/kubelet",  # 包含 kubelet 凭据目录
        str(home / ".kube"): "~/.kube",
        str(home / ".kube" / "cache"): "~/.kube",
        str(home): "~/.kube",  # 包含 ~/.kube
        str(home / ".ssh"): "~/.ssh",
        str(PROJECT_ROOT / "config"): "<项目>/config",
        str(PROJECT_ROOT): "<项目>/config",  # 包含项目配置目录与 .env
    }


@pytest.mark.parametrize("env", ["production", "prod", "", "staging"])
def test_production_refuses_unsafe_file_roots(monkeypatch, fake_home, env):
    for root, label in unsafe_roots(fake_home).items():
        monkeypatch.setenv("MCP_FILE_ROOT", root)
        assert label in file_root_violations(), root
        with pytest.raises(UnsafeFileRootError) as refused:
            ensure_file_root_safe(env)
        assert label in str(refused.value)


@pytest.mark.parametrize("env", ["test", "development", "dev"])
def test_non_production_only_warns(monkeypatch, fake_home, caplog, env):
    caplog.set_level(logging.WARNING, logger="aiops.mcp.file_boundary")
    monkeypatch.setenv("MCP_FILE_ROOT", "/")
    ensure_file_root_safe(env)
    assert "MCP_FILE_ROOT" in caplog.text and "仅告警" in caplog.text


@pytest.mark.parametrize("env", ["production", "test"])
def test_dedicated_data_directory_is_accepted_silently(monkeypatch, fake_home, caplog, env):
    caplog.set_level(logging.WARNING, logger="aiops.mcp.file_boundary")
    monkeypatch.setenv("MCP_FILE_ROOT", str(fake_home / "work" / "kb"))
    ensure_file_root_safe(env)
    monkeypatch.delenv("MCP_FILE_ROOT")
    monkeypatch.chdir(PROJECT_ROOT)
    assert file_root_violations() == []  # 默认 data/knowledge_base
    ensure_file_root_safe(env)
    assert caplog.text == ""


def test_symlink_into_sensitive_directory_is_detected(monkeypatch, fake_home, tmp_path):
    link = tmp_path / "innocent"
    link.symlink_to(fake_home / ".kube")
    monkeypatch.setenv("MCP_FILE_ROOT", str(link))
    with pytest.raises(UnsafeFileRootError):
        ensure_file_root_safe("production")


async def test_mcp_lifespan_refuses_unsafe_file_root_in_production(monkeypatch):
    from app.mcp import main as mcp_main

    monkeypatch.setattr(mcp_main.config, "env", "production")
    monkeypatch.setattr(mcp_main.config, "debug", False)
    monkeypatch.setenv("MCP_FILE_ROOT", "/")
    monkeypatch.setattr(mcp_main, "MCPServer", AsyncMock(side_effect=AssertionError("must not start")), raising=False)
    with pytest.raises(UnsafeFileRootError):
        async with mcp_main.lifespan(FastAPI()):
            pass


# ---------- 文件工具报错只显示相对路径或用户原值 ----------


@pytest.fixture
def root(monkeypatch, tmp_path):
    base = tmp_path / "secret-parent-dir-Q7" / "kb"
    (base / "docs").mkdir(parents=True)
    (base / "docs" / "a.md").write_text("hello\n")
    monkeypatch.setenv("MCP_FILE_ROOT", str(base))
    return base


def assert_no_absolute(text, base):
    assert str(base) not in text and "secret-parent-dir-Q7" not in text


@pytest.mark.parametrize(
    ("tool_cls", "key", "value", "expected"),
    [
        (FileReadTool, "file_path", "docs/missing.md", "docs/missing.md"),
        (FileReadTool, "file_path", "docs", "docs"),
        (FileReadTool, "file_path", "../outside.md", "../outside.md"),
        (FileListTool, "directory_path", "nope", "nope"),
        (FileListTool, "directory_path", "docs/a.md", "docs/a.md"),
        (FileStatsTool, "path", "ghost", "ghost"),
    ],
)
async def test_file_tool_errors_never_contain_resolved_absolute_path(root, tool_cls, key, value, expected):
    with pytest.raises(RuntimeError) as failed:
        await tool_cls().execute({key: value})
    assert expected in str(failed.value)
    assert_no_absolute(str(failed.value), root)
    assert failed.value.__cause__ is None and failed.value.__suppress_context__


async def test_absolute_input_inside_root_is_shown_relative(root):
    with pytest.raises(RuntimeError) as failed:
        await FileReadTool().execute({"file_path": str(root / "docs" / "gone.md")})
    assert "docs/gone.md" in str(failed.value)
    assert_no_absolute(str(failed.value), root)


async def test_os_error_text_with_absolute_path_is_not_propagated(root, monkeypatch):
    import builtins

    def boom(path, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(builtins, "open", boom)
    with pytest.raises(RuntimeError) as failed:
        await FileReadTool().execute({"file_path": "docs/a.md"})
    assert "PermissionError" in str(failed.value) and "docs/a.md" in str(failed.value)
    assert_no_absolute(str(failed.value), root)


async def test_successful_results_use_relative_paths(root):
    read = await FileReadTool().execute({"file_path": "docs/a.md"})
    listing = await FileListTool().execute({"directory_path": "docs"})
    stats = await FileStatsTool().execute({"path": "docs/a.md"})
    assert read["file_path"] == "docs/a.md" and read["content"] == "hello\n"
    assert listing["directory_path"] == "docs"
    assert stats["path"] == "docs/a.md"
    for result in (read, listing, stats):
        assert_no_absolute(repr(result), root)


def test_display_path_never_returns_resolved_absolute(root):
    assert file_boundary.display_path("x", str(root)) == "."
    assert file_boundary.display_path("/elsewhere/x", "/elsewhere/x") == "/elsewhere/x"
    assert file_boundary.display_path(None, None) == "NoneType"
