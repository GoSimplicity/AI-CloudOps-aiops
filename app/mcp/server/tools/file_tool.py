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

from app.utils.file_boundary import (
    NoFollowUnsupportedError,
    PathChangedError,
    display_path,
    file_root,
    open_in_root,
    resolve_tool_path,
    stat_in_root,
    validated_identity,
)

from ..mcp_server import BaseTool


class FileToolError(RuntimeError):
    """文件工具对外错误：只含相对路径或用户传入的原值，不含 resolve 后的绝对路径"""


def _failure(prefix: str, raw: Any, resolved: Any, error: Exception) -> FileToolError:
    shown = display_path(raw, resolved)
    if isinstance(error, FileToolError):
        return FileToolError(f"{prefix}: {error}")
    if isinstance(error, NoFollowUnsupportedError):
        return FileToolError(f"{prefix}: {error}（{shown}）")
    if isinstance(error, PathChangedError):
        return FileToolError(f"{prefix}: 路径在校验后发生变化（可能被替换为符号链接），已拒绝（{shown}）")
    if isinstance(error, ValueError) and str(error) == "Path is outside MCP_FILE_ROOT":
        return FileToolError(f"{prefix}: 路径超出 MCP_FILE_ROOT（{shown}）")
    # OSError/UnicodeError 等的文本可能含绝对路径，只返回异常类型
    return FileToolError(f"{prefix}: {type(error).__name__}（{shown}）")


def _entry_stat_at(dir_fd: int, dir_path: str, name: str) -> Tuple[str, os.stat_result]:
    """目录项的 (类型, stat)，相对已打开的目录 fd 做 lstat。SEC-33 P3：符号链接只有目标仍在 MCP_FILE_ROOT 内时才跟随，
    且目标经 open_in_root（O_NOFOLLOW 逐级打开）后取 fstat；指向 root 外（或悬空、打不开）的链接只返回 lstat、类型记为 symlink"""
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if stat_mode.S_ISLNK(info.st_mode):
        target = os.path.realpath(os.path.join(dir_path, name))
        if not Path(target).is_relative_to(file_root()) or not os.path.exists(target):
            return "symlink", info
        try:
            target_fd = open_in_root(target)
        except OSError:
            return "symlink", info
        try:
            info = os.fstat(target_fd)
        finally:
            os.close(target_fd)
    return ("directory" if stat_mode.S_ISDIR(info.st_mode) else "file"), info


def _stats_result(shown: str, info: os.stat_result, is_dir: bool) -> Dict[str, Any]:
    return {
        "path": shown,
        "exists": True,
        "type": "directory" if is_dir else "file",
        "size_bytes": info.st_size,
        "modified_time": info.st_mtime,
        "created_time": info.st_ctime,
        "access_time": info.st_atime,
        "permissions": oct(info.st_mode)[-3:],
    }


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

            # SEC-33 L2 P3-2：校验后按 fd 打开（逐级 O_NOFOLLOW + dev/ino 比对），大小与类型都以 fstat 为准
            fd = open_in_root(file_path, validated_identity(file_path))
            try:
                info = os.fstat(fd)
                if not stat_mode.S_ISREG(info.st_mode):
                    raise FileToolError(f"路径不是文件: {shown}")
                if info.st_size > 1024 * 1024:
                    raise FileToolError("File exceeds the 1 MiB tool limit")
                handle = open(fd, "r", encoding=encoding)
            except BaseException:
                os.close(fd)
                raise

            with handle as f:
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
                "size_bytes": info.st_size,
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
            dir_fd = open_in_root(directory_path, validated_identity(directory_path), directory=True)
            try:
                for item in os.listdir(dir_fd):
                    if not show_hidden and item.startswith("."):
                        continue

                    item_type, info = _entry_stat_at(dir_fd, directory_path, item)
                    items.append(
                        {
                            "name": item,
                            "type": item_type,
                            # root 外的符号链接 size 记 0（lstat 的 size 是目标路径长度），mtime 为链接自身
                            "size_bytes": info.st_size if item_type == "file" else 0,
                            "modified_time": info.st_mtime,
                        }
                    )
            finally:
                os.close(dir_fd)

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

            identity = validated_identity(path)
            is_dir = stat_mode.S_ISDIR(identity.st_mode)
            if not is_dir:
                # SEC-33 L3 P3-b：非目录不打开目标本身，经父目录 fd + follow_symlinks=False 取 stat 并比对 dev/ino
                return _stats_result(shown, stat_in_root(path, identity), False)

            fd = open_in_root(path, identity, directory=True)
            try:
                stat = os.fstat(fd)
                result = _stats_result(shown, stat, True)

                if is_dir:
                    total_files = 0
                    total_size = 0
                    # fwalk 基于目录 fd 遍历、不跟随符号链接目录
                    for root, _, files, root_fd in os.fwalk(".", dir_fd=fd):
                        total_files += len(files)
                        for file in files:
                            try:
                                item_type, info = _entry_stat_at(root_fd, os.path.join(path, root), file)
                            except OSError:
                                continue
                            if item_type == "file":  # root 外的符号链接不计入，避免泄露目标大小
                                total_size += info.st_size

                    result.update(
                        {"total_files": total_files, "total_size_bytes": total_size}
                    )
            finally:
                os.close(fd)

            return result

        except Exception as e:
            raise _failure("获取文件统计信息失败", raw, path, e) from None
