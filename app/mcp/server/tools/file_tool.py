#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 文件操作工具
"""

import os
from pathlib import Path
import stat as stat_mode
from typing import Any, Dict, Tuple

from app.utils.file_boundary import display_path, file_root, resolve_tool_path

from ..mcp_server import BaseTool


class FileToolError(RuntimeError):
    """文件工具对外错误：只含相对路径或用户传入的原值，不含 resolve 后的绝对路径"""


def _failure(prefix: str, raw: Any, resolved: Any, error: Exception) -> FileToolError:
    shown = display_path(raw, resolved)
    if isinstance(error, FileToolError):
        return FileToolError(f"{prefix}: {error}")
    if isinstance(error, ValueError) and str(error) == "Path is outside MCP_FILE_ROOT":
        return FileToolError(f"{prefix}: 路径超出 MCP_FILE_ROOT（{shown}）")
    # OSError/UnicodeError 等的文本可能含绝对路径，只返回异常类型
    return FileToolError(f"{prefix}: {type(error).__name__}（{shown}）")


def _entry_stat(item_path: str) -> Tuple[str, os.stat_result]:
    """目录项的 (类型, stat)。SEC-33 P3：符号链接只有目标仍在 MCP_FILE_ROOT 内时才跟随；
    指向 root 外（或悬空）的链接只做 lstat、类型记为 symlink，不泄露目标的 size / mtime"""
    if os.path.islink(item_path):
        target = os.path.realpath(item_path)
        if not Path(target).is_relative_to(file_root()) or not os.path.exists(target):
            return "symlink", os.lstat(item_path)
        item_path = target
    info = os.stat(item_path)
    return ("directory" if stat_mode.S_ISDIR(info.st_mode) else "file"), info


class FileReadTool(BaseTool):
    """读取文件内容的工具"""

    def __init__(self):
        super().__init__(name="read_file", description="读取指定路径的文件内容")

    def get_parameters(self) -> Dict[str, Any]:
        """获取工具参数定义"""
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "要读取的文件路径"},
                "encoding": {
                    "type": "string",
                    "description": "文件编码，默认为utf-8",
                    "default": "utf-8",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "最大读取行数，0表示读取全部",
                    "default": 0,
                },
            },
            "required": ["file_path"],
        }

    async def execute(self, parameters: Dict[str, Any]) -> Any:
        """执行工具"""
        raw = parameters.get("file_path")
        file_path = None
        try:
            file_path = resolve_tool_path(raw)
            shown = display_path(raw, file_path)
            encoding = parameters.get("encoding", "utf-8")
            max_lines = parameters.get("max_lines", 0)

            if not os.path.exists(file_path):
                raise FileToolError(f"文件不存在: {shown}")

            if not os.path.isfile(file_path):
                raise FileToolError(f"路径不是文件: {shown}")

            if os.path.getsize(file_path) > 1024 * 1024:
                raise FileToolError("File exceeds the 1 MiB tool limit")

            with open(file_path, "r", encoding=encoding) as f:
                if max_lines > 0:
                    lines = []
                    for i, line in enumerate(f):
                        if i >= max_lines:
                            break
                        lines.append(line)
                    content = "".join(lines)
                else:
                    content = f.read()

            return {
                "file_path": shown,
                "size_bytes": os.path.getsize(file_path),
                "content": content,
                "lines_count": len(content.splitlines()),
            }

        except Exception as e:
            raise _failure("读取文件失败", raw, file_path, e) from None


class FileListTool(BaseTool):
    """列出目录内容的工具"""

    def __init__(self):
        super().__init__(
            name="list_directory", description="列出指定目录的文件和子目录"
        )

    def get_parameters(self) -> Dict[str, Any]:
        """获取工具参数定义"""
        return {
            "type": "object",
            "properties": {
                "directory_path": {
                    "type": "string",
                    "description": "要列出的目录路径，默认为当前目录",
                },
                "show_hidden": {
                    "type": "boolean",
                    "description": "是否显示隐藏文件",
                    "default": False,
                },
            },
            "required": [],
        }

    async def execute(self, parameters: Dict[str, Any]) -> Any:
        """执行工具"""
        raw = parameters.get("directory_path", ".")
        directory_path = None
        try:
            directory_path = resolve_tool_path(raw)
            shown = display_path(raw, directory_path)
            show_hidden = parameters.get("show_hidden", False)

            if not os.path.exists(directory_path):
                raise FileToolError(f"目录不存在: {shown}")

            if not os.path.isdir(directory_path):
                raise FileToolError(f"路径不是目录: {shown}")

            items = []
            for item in os.listdir(directory_path):
                if not show_hidden and item.startswith("."):
                    continue

                item_type, info = _entry_stat(os.path.join(directory_path, item))
                items.append(
                    {
                        "name": item,
                        "type": item_type,
                        # root 外的符号链接 size 记 0（lstat 的 size 是目标路径长度），mtime 为链接自身
                        "size_bytes": info.st_size if item_type == "file" else 0,
                        "modified_time": info.st_mtime,
                    }
                )

            return {
                "directory_path": shown,
                "items": items,
                "total_count": len(items),
            }

        except Exception as e:
            raise _failure("列出目录失败", raw, directory_path, e) from None


class FileStatsTool(BaseTool):
    """获取文件/目录统计信息的工具"""

    def __init__(self):
        super().__init__(
            name="get_file_stats", description="获取文件或目录的详细统计信息"
        )

    def get_parameters(self) -> Dict[str, Any]:
        """获取工具参数定义"""
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要获取统计信息的路径"}
            },
            "required": ["path"],
        }

    async def execute(self, parameters: Dict[str, Any]) -> Any:
        """执行工具"""
        raw = parameters.get("path")
        path = None
        try:
            path = resolve_tool_path(raw)
            shown = display_path(raw, path)

            if not os.path.exists(path):
                raise FileToolError(f"路径不存在: {shown}")

            stat = os.stat(path)

            result = {
                "path": shown,
                "exists": True,
                "type": "directory" if os.path.isdir(path) else "file",
                "size_bytes": stat.st_size,
                "modified_time": stat.st_mtime,
                "created_time": stat.st_ctime,
                "access_time": stat.st_atime,
                "permissions": oct(stat.st_mode)[-3:],
            }

            if os.path.isdir(path):
                total_files = 0
                total_size = 0
                for root, _, files in os.walk(path):
                    total_files += len(files)
                    for file in files:
                        try:
                            item_type, info = _entry_stat(os.path.join(root, file))
                        except OSError:
                            continue
                        if item_type == "file":  # root 外的符号链接不计入，避免泄露目标大小
                            total_size += info.st_size

                result.update(
                    {"total_files": total_files, "total_size_bytes": total_size}
                )

            return result

        except Exception as e:
            raise _failure("获取文件统计信息失败", raw, path, e) from None
