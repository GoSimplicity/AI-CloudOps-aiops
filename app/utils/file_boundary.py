"""Restrict model-accessible files to a dedicated, configured data directory."""

import logging
import os
from pathlib import Path
import threading
from typing import Any, List, Optional

DEFAULT_FILE_ROOT = "data/knowledge_base"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger("aiops.mcp.file_boundary")


# SEC-33 P3：MCP_FILE_ROOT 的 realpath 在启动时（ensure_file_root_safe）解析一次并固定，
# 之后每次调用直接复用，避免 root 本身（或其路径上的符号链接）在检查后被替换（TOCTOU）
_pinned_root: Optional[Path] = None
_pin_lock = threading.Lock()


def _resolve_file_root() -> Path:
    return Path(os.path.realpath(os.getenv("MCP_FILE_ROOT", DEFAULT_FILE_ROOT)))


def pin_file_root() -> Path:
    """重新解析 MCP_FILE_ROOT 并固定（启动检查时调用）"""
    global _pinned_root
    with _pin_lock:
        _pinned_root = _resolve_file_root()
        return _pinned_root


def file_root() -> Path:
    """返回已固定的 root；未经启动检查直接使用工具时，首次调用解析并固定"""
    global _pinned_root
    root = _pinned_root
    if root is None:
        with _pin_lock:
            if _pinned_root is None:
                _pinned_root = _resolve_file_root()
            root = _pinned_root
    return root


def reset_file_root_cache() -> None:
    """仅供测试：清除已固定的 root，下次 file_root() 重新读取 MCP_FILE_ROOT"""
    global _pinned_root
    with _pin_lock:
        _pinned_root = None


def resolve_tool_path(value: str) -> str:
    root = file_root()
    requested = Path(value)
    path = requested.resolve() if requested.is_absolute() else (root / requested).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Path is outside MCP_FILE_ROOT")
    return str(path)


def display_path(raw: Any, resolved: Optional[str] = None) -> str:
    """对外展示的路径：MCP_FILE_ROOT 内用相对路径，否则回显用户传入的原值；绝不返回 resolve 后的绝对路径"""
    if resolved is not None:
        try:
            relative = Path(resolved).relative_to(file_root())
            return relative.as_posix() or "."
        except ValueError:
            pass
    return raw if isinstance(raw, str) else type(raw).__name__


# SEC-33：MCP_FILE_ROOT 不能是 /，也不能包含或位于以下系统/凭据/配置目录
_SYSTEM_SENSITIVE = ("/etc", "/root", "/proc", "/sys", "/dev", "/boot", "/run/secrets", "/var/run/secrets", "/var/lib/kubelet")
_HOME_SENSITIVE = ("~/.kube", "~/.ssh", "~/.aws", "~/.azure", "~/.config", "~/.docker", "~/.gnupg")
_PROJECT_SENSITIVE = ("config", ".env")


def _kubeconfig_locations() -> List[tuple]:
    """$KUBECONFIG 可能是 os.pathsep 分隔的多个文件，每个都算敏感位置；标签只写序号，不回显路径"""
    entries = [entry for entry in os.environ.get("KUBECONFIG", "").split(os.pathsep) if entry.strip()]
    return [(f"$KUBECONFIG[{index}]", Path(os.path.realpath(os.path.expanduser(entry)))) for index, entry in enumerate(entries)]


def _sensitive_locations() -> List[tuple]:
    items = [(label, Path(os.path.realpath(label))) for label in _SYSTEM_SENSITIVE]
    items += [(label, Path(os.path.realpath(os.path.expanduser(label)))) for label in _HOME_SENSITIVE]
    items += [(f"<项目>/{name}", Path(os.path.realpath(_PROJECT_ROOT / name))) for name in _PROJECT_SENSITIVE]
    items += _kubeconfig_locations()
    return items


def file_root_violations(root: Optional[Path] = None) -> List[str]:
    """返回 MCP_FILE_ROOT 命中的敏感位置标签（为空表示安全）"""
    root = Path(os.path.realpath(root if root is not None else file_root()))
    if root == Path(root.anchor):
        return ["/"]
    hits = []
    for label, location in _sensitive_locations():
        if root == location or root.is_relative_to(location) or location.is_relative_to(root):
            hits.append(label)
    return hits


class UnsafeFileRootError(RuntimeError):
    """生产环境 MCP_FILE_ROOT 指向根目录或包含/位于凭据、配置目录时拒绝启动"""


def ensure_file_root_safe(env: Any) -> None:
    """生产环境（envmode.is_production）拒绝启动；非生产只告警"""
    from app.config.envmode import is_production

    hits = file_root_violations(pin_file_root())
    if not hits:
        return
    message = "MCP_FILE_ROOT 不能是 / 或包含/位于系统、凭据、配置目录（命中：" + ", ".join(hits) + f"）；请改为专用数据目录（默认 {DEFAULT_FILE_ROOT}）"
    if is_production(env):
        raise UnsafeFileRootError(message)
    logger.warning("%s；当前为非生产环境，仅告警", message)
