#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 脚本：enforce_headers
"""

"""批量标准化项目 Python 文件头部注释。

Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 为所有 .py 文件统一添加/替换四段式头部 docstring（固定模板）。
"""

import os
from pathlib import Path
from typing import Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PY_DIRS = [
    PROJECT_ROOT / "app",
    PROJECT_ROOT / "tests",
]


def split_shebang_and_body(text: str) -> Tuple[str, str]:
    """分离 shebang/coding 行与正文。"""
    lines = text.splitlines(keepends=True)
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.startswith("#!/") or line.lstrip().startswith("# -*-"):
            idx += 1
            continue
        break
    return ("".join(lines[:idx]), "".join(lines[idx:]))


def extract_existing_docstring(body: str) -> Tuple[str, str]:
    """提取现有顶层 docstring 及剩余正文。

    返回: (docstring_text, rest_body, triple_quote)
    """
    body_stripped = body.lstrip()
    offset = len(body) - len(body_stripped)
    if body_stripped.startswith('"""') or body_stripped.startswith("'''"):
        quote = body_stripped[:3]
        end_idx = body_stripped.find(quote, 3)
        if end_idx != -1:
            doc = body_stripped[3:end_idx]
            rest = body_stripped[end_idx + 3 :]
            # 保留原始左侧空白
            return (doc, body[:offset] + rest)
    return ("", body)


HEADER_DOCSTRING = (
    '"""\n'
    "Redis向量存储实现\n"
    "Author: Bamboo\n"
    "Email: bamboocloudops@gmail.com\n"
    "License: Apache 2.0\n"
    "Description: 基于Redis的向量存储和检索系统\n"
    '"""\n'
)


def build_header() -> str:
    return HEADER_DOCSTRING


def process_file(py_file: Path) -> bool:
    text = py_file.read_text(encoding="utf-8")
    shebang, body = split_shebang_and_body(text)
    doc, rest = extract_existing_docstring(body)
    header = build_header()
    new_body = header + rest.lstrip("\n")
    new_text = shebang + new_body

    if new_text != text:
        py_file.write_text(new_text, encoding="utf-8")
        return True
    return False


def main():
    changed = 0
    for base in PY_DIRS:
        for path, _, files in os.walk(base):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                py_path = Path(path) / fn
                # 跳过生成/迁移脚本等特殊文件可在此扩展
                try:
                    if process_file(py_path):
                        changed += 1
                except Exception:
                    continue
    print(f"Updated headers for {changed} files.")


if __name__ == "__main__":
    main()
