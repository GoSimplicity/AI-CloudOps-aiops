#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 向量存储（redis_vector_store）
"""

import hashlib
import logging
import pickle
import time
from typing import Any, Dict, List, Optional
import json

import numpy as np
import redis
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
import threading

logger = logging.getLogger(__name__)


class RedisVectorStore:
    """Redis向量存储"""

    def __init__(
        self,
        redis_client: redis.Redis,
        collection_name: str = "default",
        embedding_model: Optional[Embeddings] = None,
    ):
        self.redis_client = redis_client
        self.collection_name = collection_name
        self.embedding_model = embedding_model
        self.doc_prefix = f"doc:{collection_name}"
        self.vector_prefix = f"vec:{collection_name}"
        self.metadata_prefix = f"meta:{collection_name}"
        self.vector_index_key = f"{self.vector_prefix}:index"
        # 记录ID倒排索引前缀（用于DB记录的幂等更新与清理）
        self.rid_index_prefix = f"rid:{collection_name}"

        # 内存缓存（避免每次检索反复从Redis取向量）
        self._vector_cache: Dict[str, np.ndarray] = {}
        self._doc_ids_cache: List[str] = []
        self._cache_lock = threading.Lock()
        self._last_index_load_ts = 0.0

        logger.info(f"Redis向量存储初始化完成: {collection_name}")

    def _normalize_metadata_value(self, value: Any) -> str:
        """将元数据值规范化为可写入Redis的字符串。

        - 基本类型直接转字符串
        - bytes 尝试按 utf-8 解码，否则使用 repr
        - 复杂类型用 JSON 序列化（非ASCII不转义），无法序列化则用 str
        - None 记录为空字符串
        """
        try:
            if value is None:
                return ""
            if isinstance(value, (str, int, float, bool)):
                return str(value)
            if isinstance(value, bytes):
                try:
                    return value.decode("utf-8")
                except Exception:
                    return repr(value)
            # 复杂结构尽量用 JSON 表示，保持可读性
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)

    def _serialize_metadata(self, metadata: Dict[str, Any]) -> Dict[str, str]:
        """将 Document.metadata 转换为适合 hset(mapping=...) 的字典。"""
        if not metadata:
            return {}
        serialized: Dict[str, str] = {}
        for k, v in metadata.items():
            try:
                key_str = str(k)
            except Exception:
                key_str = json.dumps(k, ensure_ascii=False, default=str)
            serialized[key_str] = self._normalize_metadata_value(v)
        return serialized

    def _generate_doc_id(self, content: str) -> str:
        """生成文档ID"""
        return hashlib.md5(content.encode()).hexdigest()

    async def add_documents(self, documents: List[Document]) -> List[str]:
        """添加文档"""
        try:
            if not documents:
                return []

            doc_ids = []
            pipe = self.redis_client.pipeline()
            vectors_to_cache: Dict[str, np.ndarray] = {}
            for doc in documents:
                doc_id = self._generate_doc_id(doc.page_content)
                doc_ids.append(doc_id)

                # 存储文档内容
                doc_key = f"{self.doc_prefix}:{doc_id}"
                pipe.set(doc_key, doc.page_content)

                # 存储元数据
                meta_key = f"{self.metadata_prefix}:{doc_id}"
                pipe.hset(meta_key, mapping=self._serialize_metadata(doc.metadata))

                # 生成并存储向量
                if self.embedding_model:
                    try:
                        embedding = await self.embedding_model.aembed_query(
                            doc.page_content
                        )
                        vector_key = f"{self.vector_prefix}:{doc_id}"
                        vector_bytes = pickle.dumps(np.array(embedding))
                        pipe.set(vector_key, vector_bytes)
                        vectors_to_cache[doc_id] = np.array(embedding)
                    except Exception as e:
                        logger.warning(f"生成向量失败: {e}")

                # 维护索引集合
                pipe.sadd(self.vector_index_key, doc_id)

            # 一次性提交
            try:
                pipe.execute()
            except Exception as e:
                logger.warning(f"批量入库失败，改为逐条: {e}")
                for doc in documents:
                    try:
                        doc_id = self._generate_doc_id(doc.page_content)
                        self.redis_client.set(
                            f"{self.doc_prefix}:{doc_id}", doc.page_content
                        )
                        self.redis_client.hset(
                            f"{self.metadata_prefix}:{doc_id}",
                            mapping=self._serialize_metadata(doc.metadata),
                        )
                        if self.embedding_model:
                            embedding = await self.embedding_model.aembed_query(
                                doc.page_content
                            )
                            vector_bytes = pickle.dumps(np.array(embedding))
                            self.redis_client.set(
                                f"{self.vector_prefix}:{doc_id}", vector_bytes
                            )
                            vectors_to_cache[doc_id] = np.array(embedding)
                        self.redis_client.sadd(self.vector_index_key, doc_id)
                    except Exception as ex:
                        logger.warning(f"逐条入库失败: {ex}")

            # 更新内存缓存
            if vectors_to_cache:
                with self._cache_lock:
                    self._vector_cache.update(vectors_to_cache)
                    for did in doc_ids:
                        if did not in self._doc_ids_cache:
                            self._doc_ids_cache.append(did)

            logger.info(f"添加了 {len(doc_ids)} 个文档")
            return doc_ids

        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            return []

    async def similarity_search(
        self, query: str, k: int = 4, score_threshold: float = 0.0
    ) -> List[Document]:
        """相似性搜索（使用内存缓存+Redis索引提升性能）"""
        try:
            if not self.embedding_model:
                return await self._keyword_search(query, k)

            # 生成查询向量
            query_embedding = await self.embedding_model.aembed_query(query)
            query_vector = np.array(query_embedding)

            # 确保索引和向量缓存已加载
            self._ensure_index_loaded()

            doc_ids = list(self._doc_ids_cache)
            if not doc_ids:
                return []

            # 计算相似度
            similarities: List[tuple] = []
            with self._cache_lock:
                for doc_id in doc_ids:
                    doc_vector = self._vector_cache.get(doc_id)
                    if doc_vector is None:
                        # 缓存缺失时尝试从Redis取一次并放入缓存
                        try:
                            vector_bytes = self.redis_client.get(
                                f"{self.vector_prefix}:{doc_id}"
                            )
                            if vector_bytes:
                                doc_vector = pickle.loads(vector_bytes)
                                self._vector_cache[doc_id] = doc_vector
                        except Exception:
                            doc_vector = None
                    if doc_vector is None:
                        continue
                    similarity = self._cosine_similarity(query_vector, doc_vector)
                    if similarity >= score_threshold:
                        similarities.append((doc_id, similarity))

            # 排序并获取top-k
            similarities.sort(key=lambda x: x[1], reverse=True)
            top_similarities = similarities[:k]

            # 批量获取内容与元数据
            return self._build_documents_from_ids(top_similarities)

        except Exception as e:
            logger.error(f"相似性搜索失败: {e}")
            return []

    def _get_all_doc_ids(self) -> List[str]:
        try:
            if self.redis_client.exists(self.vector_index_key):
                raw_ids = self.redis_client.smembers(self.vector_index_key)
                return [
                    rid.decode() if isinstance(rid, bytes) else str(rid)
                    for rid in raw_ids
                ]
            return []
        except Exception:
            return []

    def _find_doc_ids_by_field(self, field: str, value: str) -> List[str]:
        """通过元数据字段精确匹配查找文档ID（全量扫描索引）。"""
        try:
            doc_ids = self._get_all_doc_ids()
            if not doc_ids:
                return []
            pipe = self.redis_client.pipeline()
            for doc_id in doc_ids:
                pipe.hget(f"{self.metadata_prefix}:{doc_id}", field)
            raw = pipe.execute()
            matched: List[str] = []
            for i, v in enumerate(raw):
                if v is None:
                    continue
                try:
                    vs = v.decode() if isinstance(v, bytes) else str(v)
                except Exception:
                    continue
                if vs == value:
                    matched.append(doc_ids[i])
            return matched
        except Exception:
            return []

    def delete_by_record_id(self, record_id: str) -> int:
        """按数据库记录ID删除对应的文档（若存在）。"""
        try:
            ids = self._find_doc_ids_by_field("record_id", str(record_id))
            if not ids:
                return 0
            self.delete_documents(ids)
            return len(ids)
        except Exception:
            return 0

    def delete_by_title(self, title: str) -> int:
        """按标题删除（用于迁移前数据清理的兜底操作）。"""
        try:
            ids = self._find_doc_ids_by_field("title", title)
            if not ids:
                return 0
            self.delete_documents(ids)
            return len(ids)
        except Exception:
            return 0

    def _build_documents_from_ids(self, ranked_ids: List[tuple]) -> List[Document]:
        """根据排名好的 (doc_id, score) 列表批量构建Document列表"""
        if not ranked_ids:
            return []
        doc_ids = [doc_id for doc_id, _ in ranked_ids]
        try:
            pipe = self.redis_client.pipeline()
            for doc_id in doc_ids:
                pipe.get(f"{self.doc_prefix}:{doc_id}")
                pipe.hgetall(f"{self.metadata_prefix}:{doc_id}")
            raw = pipe.execute()
            results: List[Document] = []
            for i, (doc_id, score) in enumerate(ranked_ids):
                content = raw[2 * i]
                metadata = raw[2 * i + 1] or {}
                if content:
                    metadata_dict = {
                        k.decode(): v.decode() for k, v in metadata.items()
                    }
                    metadata_dict["similarity_score"] = score
                    results.append(
                        Document(page_content=content.decode(), metadata=metadata_dict)
                    )
            return results
        except Exception as e:
            logger.warning(f"批量构建文档失败: {e}")
            results: List[Document] = []
            for doc_id, score in ranked_ids:
                content = self.redis_client.get(f"{self.doc_prefix}:{doc_id}")
                metadata = self.redis_client.hgetall(f"{self.metadata_prefix}:{doc_id}")
                if content:
                    metadata_dict = {
                        k.decode(): v.decode() for k, v in metadata.items()
                    }
                    metadata_dict["similarity_score"] = score
                    results.append(
                        Document(page_content=content.decode(), metadata=metadata_dict)
                    )
            return results

    async def keyword_search_public(self, query: str, k: int = 4) -> List[Document]:
        """公开的关键字搜索接口（供上层Hybrid调用）。"""
        return await self._keyword_search(query, k)

    async def _keyword_search(self, query: str, k: int) -> List[Document]:
        """关键字搜索（使用索引与管道加速）"""
        try:
            self._ensure_index_loaded()
            query_lower = query.lower()
            doc_ids = list(self._doc_ids_cache)
            if not doc_ids:
                return []

            # 一次性获取所有内容
            pipe = self.redis_client.pipeline()
            for doc_id in doc_ids:
                pipe.get(f"{self.doc_prefix}:{doc_id}")
            contents = pipe.execute()

            matches = []
            for i, content in enumerate(contents):
                if not content:
                    continue
                content_str = content.decode().lower()
                if query_lower in content_str:
                    # 简单匹配得分
                    wc = len(content_str.split()) or 1
                    score = content_str.count(query_lower) / wc
                    matches.append((doc_ids[i], score))

            matches.sort(key=lambda x: x[1], reverse=True)
            top = matches[:k]
            # 构建文档（带keyword_score）
            try:
                pipe = self.redis_client.pipeline()
                for doc_id, _ in top:
                    pipe.get(f"{self.doc_prefix}:{doc_id}")
                    pipe.hgetall(f"{self.metadata_prefix}:{doc_id}")
                raw = pipe.execute()
                results: List[Document] = []
                for i, (doc_id, score) in enumerate(top):
                    content = raw[2 * i]
                    metadata = raw[2 * i + 1] or {}
                    if content:
                        metadata_dict = {
                            k.decode(): v.decode() for k, v in metadata.items()
                        }
                        metadata_dict["keyword_score"] = score
                        results.append(
                            Document(
                                page_content=content.decode(), metadata=metadata_dict
                            )
                        )
                return results
            except Exception as e:
                logger.warning(f"关键字搜索构建文档失败: {e}")
                results: List[Document] = []
                for doc_id, score in top:
                    content = self.redis_client.get(f"{self.doc_prefix}:{doc_id}")
                    metadata = self.redis_client.hgetall(
                        f"{self.metadata_prefix}:{doc_id}"
                    )
                    if content:
                        metadata_dict = {
                            k.decode(): v.decode() for k, v in metadata.items()
                        }
                        metadata_dict["keyword_score"] = score
                        results.append(
                            Document(
                                page_content=content.decode(), metadata=metadata_dict
                            )
                        )
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
                pipe.srem(self.vector_index_key, doc_id)
                # 从 rid 索引集合中清除（如存在）
                try:
                    rid_val = self.redis_client.hget(
                        f"{self.metadata_prefix}:{doc_id}", "record_id"
                    )
                    if rid_val is not None:
                        rid_str = (
                            rid_val.decode()
                            if isinstance(rid_val, bytes)
                            else str(rid_val)
                        )
                        pipe.srem(f"{self.rid_index_prefix}:{rid_str}", doc_id)
                except Exception:
                    pass
            pipe.execute()
            logger.info(f"删除了 {len(doc_ids)} 个文档")
            # 同步更新缓存
            with self._cache_lock:
                for did in doc_ids:
                    self._vector_cache.pop(did, None)
                    if did in self._doc_ids_cache:
                        try:
                            self._doc_ids_cache.remove(did)
                        except ValueError:
                            pass
        except Exception as e:
            logger.error(f"删除文档失败: {e}")

    def delete_by_content(self, content: str) -> bool:
        """根据原始内容删除对应文档（通过内容哈希命中）。"""
        try:
            doc_id = hashlib.md5(content.encode()).hexdigest()
            self.delete_documents([doc_id])
            return True
        except Exception as e:
            logger.error(f"按内容删除文档失败: {e}")
            return False

    def get_document_count(self) -> int:
        """获取文档数量"""
        try:
            if self.redis_client.exists(self.vector_index_key):
                return int(self.redis_client.scard(self.vector_index_key))
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
            # 删除索引集合
            try:
                self.redis_client.delete(self.vector_index_key)
            except Exception:
                pass
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
                "collection": self.collection_name,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "redis_connected": False,
                "error": str(e),
                "collection": self.collection_name,
            }

    def _ensure_index_loaded(self):
        """确保内存中已加载向量索引与向量缓存。"""
        try:
            # 已加载则跳过
            if self._doc_ids_cache and self._vector_cache:
                return

            with self._cache_lock:
                # 再次检查，避免并发重复加载
                if self._doc_ids_cache and self._vector_cache:
                    return

                # 优先使用索引集合
                if self.redis_client.exists(self.vector_index_key):
                    raw_ids = self.redis_client.smembers(self.vector_index_key)
                    doc_ids = [
                        rid.decode() if isinstance(rid, bytes) else str(rid)
                        for rid in raw_ids
                    ]
                else:
                    # 兼容旧数据：从keys构建一次索引
                    vector_keys = self.redis_client.keys(f"{self.vector_prefix}:*")
                    doc_ids = []
                    for vector_key in vector_keys:
                        try:
                            key_str = (
                                vector_key.decode()
                                if isinstance(vector_key, bytes)
                                else str(vector_key)
                            )
                            doc_id = key_str.split(":")[-1]
                            doc_ids.append(doc_id)
                        except Exception:
                            continue
                    if doc_ids:
                        try:
                            self.redis_client.sadd(self.vector_index_key, *doc_ids)
                        except Exception:
                            pass

                self._doc_ids_cache = doc_ids

                # 批量加载向量
                pipe = self.redis_client.pipeline()
                for doc_id in doc_ids:
                    pipe.get(f"{self.vector_prefix}:{doc_id}")
                vector_bytes_list = pipe.execute() if doc_ids else []

                self._vector_cache = {}
                for i, vb in enumerate(vector_bytes_list):
                    if not vb:
                        continue
                    try:
                        self._vector_cache[doc_ids[i]] = pickle.loads(vb)
                    except Exception:
                        continue
                self._last_index_load_ts = time.time()
        except Exception as e:
            logger.warning(f"加载向量索引失败: {e}")


class VectorStoreManager:
    """向量存储管理器"""

    def __init__(
        self, vector_db_path: str, collection_name: str, embedding_model: Embeddings
    ):
        self.vector_db_path = vector_db_path
        self.collection_name = collection_name
        self.embedding_model = embedding_model
        self.redis_client = None
        self.vector_store = None

        self._initialize_redis()

    def _initialize_redis(self):
        """初始化Redis连接"""
        try:
            # 读取全局配置，支持密码
            try:
                from app.config.settings import config as app_config

                host = getattr(app_config.redis, "host", "localhost")
                port = int(getattr(app_config.redis, "port", 6379))
                db = int(getattr(app_config.redis, "db", 0))
                password = getattr(app_config.redis, "password", None)
                socket_timeout = float(getattr(app_config.redis, "socket_timeout", 5.0))
            except Exception:
                host, port, db, password, socket_timeout = (
                    "localhost",
                    6379,
                    0,
                    None,
                    5.0,
                )

            self.redis_client = redis.Redis(
                host=host,
                port=port,
                db=db,
                password=password,
                decode_responses=False,
                socket_timeout=socket_timeout,
                socket_connect_timeout=socket_timeout,
            )

            # 测试连接
            self.redis_client.ping()

            self.vector_store = RedisVectorStore(
                self.redis_client,
                self.collection_name,
                self.embedding_model,
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

    async def similarity_search(
        self, query: str, k: int = 4, **kwargs
    ) -> List[Document]:
        """相似性搜索"""
        if not self.vector_store:
            raise RuntimeError("向量存储未初始化")
        return await self.vector_store.similarity_search(query, k, **kwargs)

    async def keyword_search(self, query: str, k: int = 4) -> List[Document]:
        """关键字搜索（用于Hybrid）。"""
        if not self.vector_store:
            raise RuntimeError("向量存储未初始化")
        return await self.vector_store.keyword_search_public(query, k)

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
        logger.debug(
            f"retriever_invoke query_len={len(query or '')} k={self.search_kwargs.get('k', 4)} score_threshold={self.search_kwargs.get('score_threshold', 0.0)}"
        )
        return await self.vector_store.similarity_search(
            query,
            k=self.search_kwargs.get("k", 4),
            score_threshold=self.search_kwargs.get("score_threshold", 0.0),
        )

    async def get_relevant_documents(self, query: str) -> List[Document]:
        """获取相关文档（LangChain兼容接口）"""
        return await self.invoke(query)
