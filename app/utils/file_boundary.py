"""Restrict model-accessible files to a dedicated, configured data directory."""

import os
from pathlib import Path


def resolve_tool_path(value: str) -> str:
    root = Path(os.getenv("MCP_FILE_ROOT", "data/knowledge_base")).resolve()
    requested = Path(value)
    path = requested.resolve() if requested.is_absolute() else (root / requested).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Path is outside MCP_FILE_ROOT")
    return str(path)
