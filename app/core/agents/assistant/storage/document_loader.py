#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
import logging
import re
from pathlib import Path
from typing import List

from langchain.text_splitter import MarkdownHeaderTextSplitter
from langchain_core.documents import Document

logger = logging.getLogger("aiops.assistant.document_loader")


class DocumentLoader:
    """仅支持 txt 和 md 的文档加载器"""

    def __init__(self, knowledge_base_path: str):
        self.knowledge_base_path = Path(knowledge_base_path)

        # 仅保留 txt 和 markdown
        self.supported_extensions = {
            ".txt": self._load_text_file,
            ".md": self._load_markdown_file,
            ".markdown": self._load_markdown_file,
        }

    def load_documents(self) -> List[Document]:
        """加载所有支持的文档"""
        logger.info(f"正在检查知识库路径: {self.knowledge_base_path}")

        if not self.knowledge_base_path.exists():
            logger.warning(f"知识库路径不存在: {self.knowledge_base_path}")
            self.knowledge_base_path.mkdir(parents=True, exist_ok=True)

            alt_path = self.knowledge_base_path.parent / "data" / "knowledge_base"
            if alt_path.exists():
                logger.info(f"使用替代知识库路径: {alt_path}")
                self.knowledge_base_path = alt_path
            else:
                return []

        all_files = list(self.knowledge_base_path.rglob("*"))
        supported_files = [
            f
            for f in all_files
            if f.is_file() and f.suffix.lower() in self.supported_extensions
        ]
        supported_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        logger.info(f"发现 {len(supported_files)} 个支持的文件")

        documents = []
        for file_path in supported_files:
            try:
                loader_func = self.supported_extensions[file_path.suffix.lower()]
                documents.extend(loader_func(file_path))
            except Exception as e:
                logger.error(f"加载文件失败 {file_path}: {e}")

        cleaned_documents = self._clean_documents(documents)
        logger.info(f"总共加载 {len(cleaned_documents)} 个有效文档")
        return cleaned_documents

    def _clean_documents(self, documents: List[Document]) -> List[Document]:
        """清理和优化文档"""
        cleaned = []
        for doc in documents:
            content = doc.page_content.strip()
            if len(content) < 10:
                continue

            content = re.sub(r"\n\s*\n\s*\n", "\n\n", content)
            content = re.sub(r"[ \t]+", " ", content)

            doc.page_content = content
            if doc.metadata:
                doc.metadata.update(
                    {
                        "content_length": len(content),
                        "word_count": len(content.split()),
                        "line_count": len(content.splitlines()),
                    }
                )
            cleaned.append(doc)
        return cleaned

    def _load_text_file(self, file_path: Path) -> List[Document]:
        """加载纯文本文件"""
        try:
            content = file_path.read_text(encoding="utf-8").strip()
            if not content:
                return []
            return [
                Document(
                    page_content=content,
                    metadata={
                        "source": str(file_path),
                        "filename": file_path.name,
                        "filetype": "text",
                        "modified_time": file_path.stat().st_mtime,
                        "file_size": file_path.stat().st_size,
                    },
                )
            ]
        except Exception as e:
            logger.error(f"加载文本文件失败 {file_path}: {e}")
            return []

    def _load_markdown_file(self, file_path: Path) -> List[Document]:
        """加载 Markdown 文件"""
        try:
            content = file_path.read_text(encoding="utf-8").strip()
            if not content:
                return []

            headers_to_split_on = [
                ("#", "Header 1"),
                ("##", "Header 2"),
                ("###", "Header 3"),
            ]

            try:
                md_docs = MarkdownHeaderTextSplitter(
                    headers_to_split_on=headers_to_split_on
                ).split_text(content)

                for doc in md_docs:
                    doc.metadata.update(
                        {
                            "source": str(file_path),
                            "filename": file_path.name,
                            "filetype": "markdown",
                            "modified_time": file_path.stat().st_mtime,
                            "file_size": file_path.stat().st_size,
                        }
                    )
                return md_docs
            except Exception:
                # 降级为单文档
                return [
                    Document(
                        page_content=content,
                        metadata={
                            "source": str(file_path),
                            "filename": file_path.name,
                            "filetype": "markdown",
                            "modified_time": file_path.stat().st_mtime,
                            "file_size": file_path.stat().st_size,
                        },
                    )
                ]
        except Exception as e:
            logger.error(f"加载 Markdown 文件失败 {file_path}: {e}")
            return []
