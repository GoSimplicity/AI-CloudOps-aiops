#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

import hashlib
import logging
import pickle
import time
from typing import Any, Dict, List, Optional

import numpy as np
import redis
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

logger = logging.getLogger(__name__)


class RedisVectorStore:
    """Redis向量存储"""

    def __init__(self, 
                 redis_client: redis.Redis,
                 collection_name: str = "default",
                 embedding_model: Optional[Embeddings] = None):
        self.redis_client = redis_client
        self.collection_name = collection_name
        self.embedding_model = embedding_model
        self.doc_prefix = f"doc:{collection_name}"
        self.vector_prefix = f"vec:{collection_name}"
        self.metadata_prefix = f"meta:{collection_name}"
        
        logger.info(f"Redis向量存储初始化完成: {collection_name}")

    def _generate_doc_id(self, content: str) -> str:
        """生成文档ID"""
        return hashlib.md5(content.encode()).hexdigest()

    async def add_documents(self, documents: List[Document]) -> List[str]:
        """添加文档"""
        try:
            if not documents:
                return []

            doc_ids = []
            for doc in documents:
                doc_id = self._generate_doc_id(doc.page_content)
                doc_ids.append(doc_id)

                # 存储文档内容
                doc_key = f"{self.doc_prefix}:{doc_id}"
                self.redis_client.set(doc_key, doc.page_content)

                # 存储元数据
                meta_key = f"{self.metadata_prefix}:{doc_id}"
                self.redis_client.hset(meta_key, mapping=doc.metadata)

                # 生成并存储向量
                if self.embedding_model:
                    try:
                        embedding = await self.embedding_model.aembed_query(doc.page_content)
                        vector_key = f"{self.vector_prefix}:{doc_id}"
                        vector_bytes = pickle.dumps(np.array(embedding))
                        self.redis_client.set(vector_key, vector_bytes)
                    except Exception as e:
                        logger.warning(f"生成向量失败: {e}")

            logger.info(f"添加了 {len(doc_ids)} 个文档")
            return doc_ids

        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            return []

    async def similarity_search(self, 
                               query: str, 
                               k: int = 4,
                               score_threshold: float = 0.0) -> List[Document]:
        """相似性搜索"""
        try:
            if not self.embedding_model:
                return await self._keyword_search(query, k)

            # 生成查询向量
            query_embedding = await self.embedding_model.aembed_query(query)
            query_vector = np.array(query_embedding)

            # 获取所有文档向量并计算相似度
            similarities = []
            vector_keys = self.redis_client.keys(f"{self.vector_prefix}:*")
            
            for vector_key in vector_keys:
                try:
                    doc_id = vector_key.decode().split(':')[-1]
                    vector_bytes = self.redis_client.get(vector_key)
                    if vector_bytes:
                        doc_vector = pickle.loads(vector_bytes)
                        similarity = self._cosine_similarity(query_vector, doc_vector)
                        if similarity >= score_threshold:
                            similarities.append((doc_id, similarity))
                except Exception as e:
                    logger.warning(f"计算相似度失败: {e}")
                    continue

            # 排序并获取top-k
            similarities.sort(key=lambda x: x[1], reverse=True)
            top_similarities = similarities[:k]

            # 构建结果文档
            results = []
            for doc_id, score in top_similarities:
                doc_key = f"{self.doc_prefix}:{doc_id}"
                meta_key = f"{self.metadata_prefix}:{doc_id}"
                
                content = self.redis_client.get(doc_key)
                metadata = self.redis_client.hgetall(meta_key)
                
                if content:
                    metadata_dict = {k.decode(): v.decode() for k, v in metadata.items()}
                    metadata_dict['similarity_score'] = score
                    
                    doc = Document(
                        page_content=content.decode(),
                        metadata=metadata_dict
                    )
                    results.append(doc)

            return results

        except Exception as e:
            logger.error(f"相似性搜索失败: {e}")
            return []

    async def _keyword_search(self, query: str, k: int) -> List[Document]:
        """关键字搜索（备用方法）"""
        try:
            results = []
            doc_keys = self.redis_client.keys(f"{self.doc_prefix}:*")
            
            query_lower = query.lower()
            matches = []
            
            for doc_key in doc_keys:
                content = self.redis_client.get(doc_key)
                if content:
                    content_str = content.decode().lower()
                    if query_lower in content_str:
                        doc_id = doc_key.decode().split(':')[-1]
                        # 简单的匹配得分
                        score = content_str.count(query_lower) / len(content_str.split())
                        matches.append((doc_id, score))
            
            # 排序并获取top-k
            matches.sort(key=lambda x: x[1], reverse=True)
            
            for doc_id, score in matches[:k]:
                doc_key = f"{self.doc_prefix}:{doc_id}"
                meta_key = f"{self.metadata_prefix}:{doc_id}"
                
                content = self.redis_client.get(doc_key)
                metadata = self.redis_client.hgetall(meta_key)
                
                if content:
                    metadata_dict = {k.decode(): v.decode() for k, v in metadata.items()}
                    metadata_dict['keyword_score'] = score
                    
                    doc = Document(
                        page_content=content.decode(),
                        metadata=metadata_dict
                    )
                    results.append(doc)
                    
            return results
            
        except Exception as e:
            logger.error(f"关键字搜索失败: {e}")
            return []

    def _cosine_similarity(self, vec1: np.ndarray, vec2: np.ndarray) -> float:
        """计算余弦相似度"""
        try:
            dot_product = np.dot(vec1, vec2)
            norm_a = np.linalg.norm(vec1)
            norm_b = np.linalg.norm(vec2)
            
            if norm_a == 0 or norm_b == 0:
                return 0.0
            
            return dot_product / (norm_a * norm_b)
        except Exception:
            return 0.0

    def delete_documents(self, doc_ids: List[str]):
        """删除文档"""
        try:
            pipe = self.redis_client.pipeline()
            for doc_id in doc_ids:
                pipe.delete(f"{self.doc_prefix}:{doc_id}")
                pipe.delete(f"{self.vector_prefix}:{doc_id}")
                pipe.delete(f"{self.metadata_prefix}:{doc_id}")
            pipe.execute()
            logger.info(f"删除了 {len(doc_ids)} 个文档")
        except Exception as e:
            logger.error(f"删除文档失败: {e}")

    def get_document_count(self) -> int:
        """获取文档数量"""
        try:
            return len(self.redis_client.keys(f"{self.doc_prefix}:*"))
        except Exception as e:
            logger.error(f"获取文档数量失败: {e}")
            return 0

    def clear_collection(self):
        """清空集合"""
        try:
            keys = self.redis_client.keys(f"*{self.collection_name}:*")
            if keys:
                self.redis_client.delete(*keys)
            logger.info(f"已清空集合: {self.collection_name}")
        except Exception as e:
            logger.error(f"清空集合失败: {e}")

    def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        try:
            start_time = time.time()
            self.redis_client.ping()
            response_time = (time.time() - start_time) * 1000
            
            return {
                "status": "healthy",
                "redis_connected": True,
                "response_time_ms": round(response_time, 2),
                "document_count": self.get_document_count(),
                "collection": self.collection_name
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "redis_connected": False,
                "error": str(e),
                "collection": self.collection_name
            }


class VectorStoreManager:
    """向量存储管理器"""

    def __init__(self, 
                 vector_db_path: str, 
                 collection_name: str, 
                 embedding_model: Embeddings):
        self.vector_db_path = vector_db_path
        self.collection_name = collection_name
        self.embedding_model = embedding_model
        self.redis_client = None
        self.vector_store = None
        
        self._initialize_redis()

    def _initialize_redis(self):
        """初始化Redis连接"""
        try:
            # 简化的Redis配置
            self.redis_client = redis.Redis(
                host='localhost',
                port=6379,
                decode_responses=False,
                socket_timeout=5.0,
                socket_connect_timeout=5.0
            )
            
            # 测试连接
            self.redis_client.ping()
            
            self.vector_store = RedisVectorStore(
                self.redis_client,
                self.collection_name,
                self.embedding_model
            )
            
            logger.info("Redis向量存储管理器初始化成功")
            
        except Exception as e:
            logger.error(f"Redis连接失败: {e}")
            raise

    async def add_documents(self, documents: List[Document]) -> List[str]:
        """添加文档"""
        if not self.vector_store:
            raise RuntimeError("向量存储未初始化")
        return await self.vector_store.add_documents(documents)

    async def similarity_search(self, 
                               query: str, 
                               k: int = 4, 
                               **kwargs) -> List[Document]:
        """相似性搜索"""
        if not self.vector_store:
            raise RuntimeError("向量存储未初始化")
        return await self.vector_store.similarity_search(query, k, **kwargs)

    def get_document_count(self) -> int:
        """获取文档数量"""
        if not self.vector_store:
            return 0
        return self.vector_store.get_document_count()

    def clear_store(self):
        """清空存储"""
        if self.vector_store:
            self.vector_store.clear_collection()

    def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        if not self.vector_store:
            return {"status": "unhealthy", "error": "向量存储未初始化"}
        return self.vector_store.health_check()

    def get_retriever(self, **kwargs):
        """获取检索器"""
        return RedisRetriever(self.vector_store, **kwargs)


class RedisRetriever:
    """Redis检索器"""

    def __init__(self, vector_store: RedisVectorStore, **kwargs):
        self.vector_store = vector_store
        self.search_kwargs = kwargs

    async def invoke(self, query: str) -> List[Document]:
        """执行检索"""
        return await self.vector_store.similarity_search(
            query, 
            k=self.search_kwargs.get('k', 4)
        )

    async def get_relevant_documents(self, query: str) -> List[Document]:
        """获取相关文档（LangChain兼容接口）"""
        return await self.invoke(query)