#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 多Agent 模块（vector_store_manager）
"""

import logging
import os
import threading
from typing import List

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.core.vector.redis_vector_store import (
    VectorStoreManager as BaseVectorStoreManager,
)

logger = logging.getLogger("aiops.assistant.vector_store_manager")


class VectorStoreManager:
    """向量存储管理器（使用底层Redis实现）"""

    def __init__(
        self, vector_db_path: str, collection_name: str, embedding_model: Embeddings
    ):
        self.vector_db_path = vector_db_path
        self.collection_name = collection_name
        self.embedding_model = embedding_model
        self._lock = threading.Lock()

        # Redis配置由底层管理器自行读取 settings，不在此重复处理

        # 初始化底层Redis向量存储管理器
        self.redis_manager = BaseVectorStoreManager(
            vector_db_path=vector_db_path,
            collection_name=collection_name,
            embedding_model=embedding_model,
        )

        self.retriever = None
        os.makedirs(vector_db_path, exist_ok=True)

    async def add_documents(self, documents: List[Document]) -> bool:
        """添加文档到向量库（追加）。"""
        try:
            added_ids = await self.redis_manager.add_documents(documents)
            return bool(added_ids)
        except Exception as e:
            logger.error(f"添加文档到向量库失败: {e}")
            return False

    def load_existing_db(self) -> bool:
        """加载现有向量数据库"""
        try:
            with self._lock:
                logger.info(f"检查Redis向量存储，集合: {self.collection_name}")

                # 检查Redis向量存储健康状态
                health = self.redis_manager.health_check()
                if (
                    health.get("status") == "healthy"
                    and health.get("document_count", 0) > 0
                ):
                    logger.info(
                        f"Redis向量存储加载成功，包含 {health['document_count']} 个文档"
                    )
                    return True
                else:
                    logger.info("Redis向量存储为空或不可用")
                    return False

        except Exception as e:
            logger.error(f"加载Redis向量存储失败: {e}")
            return False

    async def create_vector_store(self, documents: List[Document]) -> bool:
        """创建/追加向量数据库（分批异步）。"""
        if not self.redis_manager:
            logger.error("Redis管理器未初始化，无法创建向量存储")
            return False

        try:
            batch_size = 50
            total = 0
            for i in range(0, len(documents), batch_size):
                batch = documents[i : i + batch_size]
                logger.info(
                    f"添加文档批次 {i // batch_size + 1}/{(len(documents) + batch_size - 1) // batch_size}"
                )
                added_ids = await self.redis_manager.add_documents(batch)
                total += len(added_ids)

            logger.info(f"成功添加 {total} 个文档到向量存储")
            return total > 0
        except Exception as e:
            logger.error(f"创建/追加向量存储失败: {e}")
            return False

    def get_retriever(self, **kwargs):
        """获取检索器（底层Redis检索器），支持透传参数如 k/score_threshold。

        注意：这里每次根据传入参数创建新的检索器，避免参数被上一次缓存的实例固定。
        """
        return self.redis_manager.get_retriever(**kwargs)

    async def similarity_search(
        self, query: str, k: int = 8, score_threshold: float = 0.05
    ) -> List[Document]:
        """统一相似性搜索接口。"""
        try:
            return await self.redis_manager.similarity_search(
                query, k=k, score_threshold=score_threshold
            )
        except Exception as e:
            logger.error(f"相似性搜索失败: {e}")
            return []

    async def keyword_search(self, query: str, k: int = 4) -> List[Document]:
        """关键字检索透传到底层（用于Hybrid模式）。"""
        try:
            return await self.redis_manager.keyword_search(query, k)
        except Exception as e:
            logger.error(f"关键字搜索失败: {e}")
            return []

    def delete_by_record_id(self, record_id: str) -> int:
        """按数据库记录ID删除对应向量文档。"""
        try:
            if not getattr(self.redis_manager, "vector_store", None):
                return 0
            return int(
                self.redis_manager.vector_store.delete_by_record_id(str(record_id))
            )
        except Exception as e:
            logger.error(f"按记录ID删除失败: {e}")
            return 0

    def delete_by_title(self, title: str) -> int:
        """按标题删除（用于早期未带record_id的文档清理）。"""
        try:
            if not getattr(self.redis_manager, "vector_store", None):
                return 0
            return int(self.redis_manager.vector_store.delete_by_title(title))
        except Exception as e:
            logger.error(f"按标题删除失败: {e}")
            return 0
