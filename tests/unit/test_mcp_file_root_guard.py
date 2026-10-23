"""SEC-33：MCP_FILE_ROOT 启动检查，以及文件工具报错不暴露 resolve 后的绝对路径"""

import logging
import os
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
        file_boundary.reset_file_root_cache()  # root 在进程内固定，循环内换 root 需要先清缓存
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
    file_boundary.reset_file_root_cache()
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


# ---------- SEC-33 P3：root 固定（TOCTOU）、$KUBECONFIG、root 外符号链接 ----------


def test_file_root_is_pinned_after_first_resolution(monkeypatch, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    link = tmp_path / "root-link"
    link.symlink_to(first)
    monkeypatch.setenv("MCP_FILE_ROOT", str(link))
    ensure_file_root_safe("test")
    assert file_boundary.file_root() == first.resolve()
    link.unlink()
    link.symlink_to(second)  # 检查之后替换 root 上的符号链接
    monkeypatch.setenv("MCP_FILE_ROOT", str(tmp_path))  # 也改环境变量
    assert file_boundary.file_root() == first.resolve()
    assert file_boundary.resolve_tool_path("x.md") == str(first.resolve() / "x.md")
    file_boundary.reset_file_root_cache()
    assert file_boundary.file_root() == tmp_path.resolve()


def test_ensure_file_root_safe_repins_and_lazy_pin_without_startup(monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("MCP_FILE_ROOT", str(a))
    assert file_boundary.file_root() == a.resolve()  # 未经启动检查：首次使用时固定
    monkeypatch.setenv("MCP_FILE_ROOT", str(b))
    assert file_boundary.file_root() == a.resolve()
    ensure_file_root_safe("test")  # 启动检查重新解析并固定，且检查的就是固定后的那个 root
    assert file_boundary.file_root() == b.resolve()


def test_relative_file_root_resolved_against_cwd_at_pin_time(monkeypatch, tmp_path):
    (tmp_path / "kb").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_FILE_ROOT", "kb")
    ensure_file_root_safe("test")
    monkeypatch.chdir(PROJECT_ROOT)
    assert file_boundary.file_root() == (tmp_path / "kb").resolve()


def test_every_kubeconfig_entry_is_sensitive(monkeypatch, fake_home, tmp_path):
    one = tmp_path / "cfg-one" / "config"
    two = tmp_path / "cfg-two" / "nested" / "admin.conf"
    for path in (one, two):
        path.parent.mkdir(parents=True)
        path.write_text("offline\n")
    monkeypatch.setenv("KUBECONFIG", os.pathsep.join([str(one), "", "  ", str(two)]))
    assert file_root_violations(one.parent) == ["$KUBECONFIG[0]"]
    assert file_root_violations(tmp_path / "cfg-two") == ["$KUBECONFIG[1]"]
    assert file_root_violations(two.parent) == ["$KUBECONFIG[1]"]
    assert {"$KUBECONFIG[0]", "$KUBECONFIG[1]"} <= set(file_root_violations(tmp_path))  # tmp_path 同时包含 fake_home
    assert file_root_violations(tmp_path / "cfg-one" / "other") == []
    monkeypatch.setenv("MCP_FILE_ROOT", str(two.parent))
    with pytest.raises(UnsafeFileRootError) as refused:
        ensure_file_root_safe("production")
    assert "$KUBECONFIG[1]" in str(refused.value)
    assert str(tmp_path) not in str(refused.value) and "admin.conf" not in str(refused.value)


def test_unset_or_blank_kubeconfig_adds_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    assert file_boundary._kubeconfig_locations() == []
    monkeypatch.setenv("KUBECONFIG", os.pathsep)
    assert file_boundary._kubeconfig_locations() == []


@pytest.fixture
def linked_root(monkeypatch, tmp_path):
    base = tmp_path / "kb"
    base.mkdir()
    (base / "inside.md").write_text("in\n")
    (base / "sub").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.bin"
    secret.write_bytes(b"x" * 4321)
    os.utime(secret, (1_000_000_000, 1_000_000_000))
    os.utime(outside, (1_100_000_000, 1_100_000_000))
    (base / "out-file").symlink_to(secret)
    (base / "out-dir").symlink_to(outside)
    (base / "dangling").symlink_to(tmp_path / "missing")
    (base / "in-link").symlink_to(base / "inside.md")
    monkeypatch.setenv("MCP_FILE_ROOT", str(base))
    return base, secret, outside


async def test_list_directory_does_not_leak_outside_symlink_target_metadata(linked_root):
    base, secret, outside = linked_root
    listing = await FileListTool().execute({"directory_path": "."})
    items = {item["name"]: item for item in listing["items"]}
    for name in ("out-file", "out-dir", "dangling"):
        assert items[name]["type"] == "symlink"
        assert items[name]["size_bytes"] == 0
        assert items[name]["modified_time"] == os.lstat(base / name).st_mtime
        assert items[name]["modified_time"] not in (1_000_000_000, 1_100_000_000)
    assert items["in-link"] == {"name": "in-link", "type": "file", "size_bytes": 3, "modified_time": os.stat(base / "inside.md").st_mtime}
    assert items["sub"]["type"] == "directory"
    assert items["inside.md"]["size_bytes"] == 3
    assert "4321" not in repr(listing)


async def test_get_file_stats_total_excludes_outside_symlinks(linked_root):
    stats = await FileStatsTool().execute({"path": "."})
    assert stats["total_size_bytes"] == 6  # inside.md + in-link，各 3 字节；root 外目标 4321 字节不计
