#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

import os
from typing import Any, Dict

from ..mcp_server import BaseTool


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
        try:
            file_path = parameters["file_path"]
            encoding = parameters.get("encoding", "utf-8")
            max_lines = parameters.get("max_lines", 0)

            if not os.path.exists(file_path):
                raise FileNotFoundError(f"文件不存在: {file_path}")

            if not os.path.isfile(file_path):
                raise ValueError(f"路径不是文件: {file_path}")

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
                "file_path": file_path,
                "size_bytes": os.path.getsize(file_path),
                "content": content,
                "lines_count": len(content.splitlines()),
            }

        except Exception as e:
            raise RuntimeError(f"读取文件失败: {str(e)}") from e


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
        try:
            directory_path = parameters.get("directory_path", ".")
            show_hidden = parameters.get("show_hidden", False)

            if not os.path.exists(directory_path):
                raise FileNotFoundError(f"目录不存在: {directory_path}")

            if not os.path.isdir(directory_path):
                raise ValueError(f"路径不是目录: {directory_path}")

            items = []
            for item in os.listdir(directory_path):
                if not show_hidden and item.startswith("."):
                    continue

                item_path = os.path.join(directory_path, item)
                is_dir = os.path.isdir(item_path)

                items.append(
                    {
                        "name": item,
                        "type": "directory" if is_dir else "file",
                        "size_bytes": os.path.getsize(item_path) if not is_dir else 0,
                        "modified_time": os.path.getmtime(item_path),
                    }
                )

            return {
                "directory_path": os.path.abspath(directory_path),
                "items": items,
                "total_count": len(items),
            }

        except Exception as e:
            raise RuntimeError(f"列出目录失败: {str(e)}") from e


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
        try:
            path = parameters["path"]

            if not os.path.exists(path):
                raise FileNotFoundError(f"路径不存在: {path}")

            stat = os.stat(path)

            result = {
                "path": os.path.abspath(path),
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
                        file_path = os.path.join(root, file)
                        try:
                            total_size += os.path.getsize(file_path)
                        except OSError:
                            pass

                result.update(
                    {"total_files": total_files, "total_size_bytes": total_size}
                )

            return result

        except Exception as e:
            raise RuntimeError(f"获取文件统计信息失败: {str(e)}") from e
