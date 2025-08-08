#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
向量存储管理器
"""

import logging
import os
import threading
import time
from typing import List

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.core.agents.assistant.models.config import assistant_config

# Redis向量存储
from app.core.vector.redis_vector_store import (
    OptimizedRedisVectorStore,
    RedisVectorStoreManager,
)

logger = logging.getLogger("aiops.assistant.vector_store_manager")


class VectorStoreManager:
    """向量存储管理器"""

    def __init__(
        self, vector_db_path: str, collection_name: str, embedding_model: Embeddings
    ):
        self.vector_db_path = vector_db_path
        self.collection_name = collection_name
        self.embedding_model = embedding_model
        self._lock = threading.Lock()

        # 获取Redis配置
        redis_config = {
            "host": assistant_config.redis_config["host"],
            "port": assistant_config.redis_config["port"],
            "db": assistant_config.redis_config["db"],
            "password": assistant_config.redis_config["password"],
            "connection_timeout": assistant_config.redis_config["connection_timeout"],
            "socket_timeout": assistant_config.redis_config["socket_timeout"],
            "max_connections": assistant_config.redis_config["max_connections"],
            "decode_responses": assistant_config.redis_config["decode_responses"],
        }

        # 动态获取嵌入维度
        try:
            test_embedding = embedding_model.embed_query("测试")
            vector_dim = len(test_embedding)
            logger.info(f"检测到嵌入维度: {vector_dim}")
        except Exception as e:
            logger.warning(f"无法检测嵌入维度，使用默认值1536: {e}")
            vector_dim = 1536

        # 初始化优化的Redis向量存储管理器
        self.redis_manager = RedisVectorStoreManager(
            redis_config=redis_config,
            collection_name=collection_name,
            embedding_dimensions=vector_dim,
            local_storage_path=vector_db_path,
        )

        # 使用优化的向量存储
        self.optimized_store = OptimizedRedisVectorStore(
            redis_config=redis_config,
            collection_name=collection_name,
            embedding_model=embedding_model,
            vector_dim=vector_dim,
            local_storage_path=vector_db_path,
            use_faiss=True,
            faiss_index_type="Flat",
        )

        self.retriever = None
        os.makedirs(vector_db_path, exist_ok=True)

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
                    # 初始化检索器
                    self.retriever = self.redis_manager.get_retriever()
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

    async def create_vector_store(
        self, documents: List[Document], use_memory: bool = False
    ) -> bool:
        """创建向量数据库"""
        if not self.redis_manager:
            logger.error("Redis管理器未初始化，无法创建向量存储")
            return False

        try:
            # 将嵌入模型传递给向量存储
            self.redis_manager.set_embedding_model(self.embedding_model)
            vector_store = self.redis_manager.get_vector_store()

            # 分批处理文档
            batch_size = 50
            for i in range(0, len(documents), batch_size):
                batch = documents[i : i + batch_size]
                logger.info(
                    f"添加文档批次 {i // batch_size + 1}/{(len(documents) + batch_size - 1) // batch_size}"
                )
                vector_store.add_documents(batch)

            logger.info(f"成功添加 {len(documents)} 个文档到向量存储")
            return True
        except Exception as e:
            logger.error(f"创建向量存储失败: {e}")
            return False

    def get_retriever(self):
        """获取检索器 - 改进版本"""
        if self.retriever is None:
            # 优化的检索器，提升召回率
            class OptimizedRetriever:
                def __init__(self, optimized_store, redis_manager):
                    self.optimized_store = optimized_store
                    self.redis_manager = redis_manager

                def get_relevant_documents(
                    self, query: str, **kwargs
                ) -> List[Document]:
                    try:
                        k = kwargs.get("k", 8)  # 增加默认检索数量
                        similarity_threshold = kwargs.get(
                            "similarity_threshold", 0.1
                        )  # 大幅降低阈值

                        logger.debug(
                            f"开始检索相关文档 - 查询: '{query}', k={k}, 阈值={similarity_threshold}"
                        )

                        # 首先尝试混合搜索
                        try:
                            results = self.optimized_store.hybrid_similarity_search(
                                query,
                                k=k,
                                semantic_weight=0.5,
                                lexical_weight=0.5,
                                similarity_threshold=similarity_threshold,
                            )

                            if results:
                                logger.debug(f"混合搜索找到 {len(results)} 个结果")
                                return [doc for doc, _ in results]
                            else:
                                logger.debug("混合搜索无结果，尝试纯语义搜索")

                        except Exception as e:
                            logger.warning(f"混合搜索失败: {e}，使用纯语义搜索")

                        # 降级到语义搜索
                        try:
                            results = self.optimized_store.similarity_search(
                                query,
                                k=k,
                                similarity_threshold=0.05,  # 极低阈值确保找到结果
                            )

                            if results:
                                logger.debug(f"语义搜索找到 {len(results)} 个结果")
                                return [doc for doc, _ in results]
                            else:
                                logger.debug("语义搜索无结果，尝试关键词搜索")

                        except Exception as e:
                            logger.warning(f"语义搜索失败: {e}，尝试关键词搜索")

                        # 最后尝试关键词搜索
                        try:
                            # 分解查询为关键词
                            query_words = [
                                word for word in query.split() if len(word) > 1
                            ]
                            all_results = []

                            for word in query_words[:3]:  # 只用前3个关键词
                                try:
                                    word_results = (
                                        self.optimized_store.similarity_search(
                                            word,
                                            k=max(2, k // 2),
                                            similarity_threshold=0.01,  # 最低阈值
                                        )
                                    )
                                    all_results.extend([doc for doc, _ in word_results])
                                except Exception:
                                    continue

                            # 去重
                            unique_results = []
                            seen_content = set()
                            for doc in all_results:
                                content_hash = hash(doc.page_content[:100])
                                if content_hash not in seen_content:
                                    seen_content.add(content_hash)
                                    unique_results.append(doc)
                                    if len(unique_results) >= k:
                                        break

                            logger.debug(
                                f"关键词搜索找到 {len(unique_results)} 个去重结果"
                            )
                            return unique_results

                        except Exception as e:
                            logger.error(f"关键词搜索也失败: {e}")
                            return []

                    except Exception as e:
                        logger.error(f"检索器完全失败: {e}")
                        return []

                def invoke(self, query) -> List[Document]:
                    # 兼容字符串查询和字典输入
                    if isinstance(query, dict):
                        query_str = query.get("query", "")
                    elif isinstance(query, str):
                        query_str = query
                    else:
                        query_str = str(query)
                    return self.get_relevant_documents(query_str)

            self.retriever = OptimizedRetriever(
                self.optimized_store, self.redis_manager
            )
        return self.retriever

    def search_documents(self, query: str, max_retries: int = 3) -> List[Document]:
        """搜索文档（优化版） - 提升召回率"""
        if not self.retriever:
            logger.warning("检索器未初始化")
            return []

        logger.debug(f"开始搜索文档: '{query}'")

        for attempt in range(max_retries):
            try:
                # 使用改进的检索器
                docs = self.retriever.get_relevant_documents(
                    query, k=10, similarity_threshold=0.05
                )

                if docs:
                    logger.debug(
                        f"第 {attempt + 1} 次尝试成功，搜索到 {len(docs)} 个文档"
                    )
                    return docs
                else:
                    logger.debug(f"第 {attempt + 1} 次尝试搜索返回空结果")

                    # 如果没有结果，尝试更宽松的查询
                    if attempt < max_retries - 1:
                        # 提取关键词重新搜索
                        keywords = [word for word in query.split() if len(word) > 2]
                        if keywords:
                            expanded_query = " ".join(keywords[:2])  # 用前两个关键词
                            logger.debug(f"尝试关键词查询: '{expanded_query}'")
                            docs = self.retriever.get_relevant_documents(
                                expanded_query, k=8, similarity_threshold=0.01
                            )
                            if docs:
                                logger.debug(f"关键词查询成功，找到 {len(docs)} 个文档")
                                return docs

            except Exception as e:
                logger.error(f"文档搜索失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(0.5)

        logger.warning(f"搜索 '{query}' 最终无结果")
        return []
