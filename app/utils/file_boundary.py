"""Restrict model-accessible files to a dedicated, configured data directory."""

import logging
import os
from pathlib import Path
from typing import Any, List, Optional

DEFAULT_FILE_ROOT = "data/knowledge_base"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger("aiops.mcp.file_boundary")


def file_root() -> Path:
    return Path(os.getenv("MCP_FILE_ROOT", DEFAULT_FILE_ROOT)).resolve()


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


def _sensitive_locations() -> List[tuple]:
    items = [(label, Path(os.path.realpath(label))) for label in _SYSTEM_SENSITIVE]
    items += [(label, Path(os.path.realpath(os.path.expanduser(label)))) for label in _HOME_SENSITIVE]
    items += [(f"<项目>/{name}", Path(os.path.realpath(_PROJECT_ROOT / name))) for name in _PROJECT_SENSITIVE]
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

    hits = file_root_violations()
    if not hits:
        return
    message = "MCP_FILE_ROOT 不能是 / 或包含/位于系统、凭据、配置目录（命中：" + ", ".join(hits) + f"）；请改为专用数据目录（默认 {DEFAULT_FILE_ROOT}）"
    if is_production(env):
        raise UnsafeFileRootError(message)
    logger.warning("%s；当前为非生产环境，仅告警", message)
