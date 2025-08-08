#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

import pickle
import numpy as np
import threading
import logging
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass
import hashlib
import time
import redis
from redis.connection import ConnectionPool
from concurrent.futures import ThreadPoolExecutor
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

# 可选依赖
FAISS_AVAILABLE = False

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False


# 配置日志
logger = logging.getLogger(__name__)


@dataclass
class VectorDocument:
    """向量文档数据结构"""

    id: str
    content: str
    metadata: Dict[str, Any]
    vector: Optional[np.ndarray] = None
    embedding_hash: Optional[str] = None


class RedisVectorStore:
    """Redis向量存储类"""

    def __init__(
        self,
        redis_config: Dict[str, Any],
        collection_name: str,
        embedding_model: Embeddings,
        vector_dim: int = 1536,
        local_storage_path: Optional[str] = None,
    ):
        """
        初始化Redis向量存储

        Args:
            redis_config: Redis连接配置
            collection_name: 集合名称
            embedding_model: 嵌入模型
            vector_dim: 向量维度
            local_storage_path: 本地存储路径（用于向量持久化）
        """
        self.redis_config = redis_config.copy()  # 复制一份避免修改原始配置
        self.collection_name = collection_name
        self.embedding_model = embedding_model
        self.vector_dim = vector_dim
        self.local_storage_path = local_storage_path or "data/vector_storage"

        # 处理Redis连接参数命名不一致
        # redis-py库使用socket_connect_timeout而不是connection_timeout
        if (
            "connection_timeout" in self.redis_config
            and "socket_connect_timeout" not in self.redis_config
        ):
            self.redis_config["socket_connect_timeout"] = self.redis_config.pop(
                "connection_timeout"
            )

        # 确保decode_responses设置为False以处理二进制数据
        self.redis_config["decode_responses"] = False

        # Redis连接池
        self.connection_pool = ConnectionPool(
            host=self.redis_config.get("host", "localhost"),
            port=self.redis_config.get("port", 6379),
            db=self.redis_config.get("db", 0),
            password=self.redis_config.get("password", ""),
            decode_responses=False,  # 关闭自动解码，因为要存储二进制数据
            max_connections=self.redis_config.get("max_connections", 10),
            socket_timeout=self.redis_config.get("socket_timeout", 5),
            socket_connect_timeout=self.redis_config.get("socket_connect_timeout", 5),
        )

        self.redis_client = redis.Redis(connection_pool=self.connection_pool)
        self._lock = threading.Lock()

        # 本地存储路径
        Path(self.local_storage_path).mkdir(parents=True, exist_ok=True)

        # Redis键前缀
        self.doc_key_prefix = f"doc:{collection_name}:"
        self.meta_key_prefix = f"meta:{collection_name}:"
        self.vector_key_prefix = f"vector:{collection_name}:"
        self.index_key = f"index:{collection_name}"

        # 初始化检查
        self._initialize()

    def _initialize(self):
        """初始化向量存储"""
        try:
            # 测试Redis连接
            self.redis_client.ping()
            logger.info(f"Redis连接成功，使用集合: {self.collection_name}")

            # 初始化索引
            if not self.redis_client.exists(self.index_key):
                self.redis_client.sadd(self.index_key, "")  # 创建空集合
                logger.info(f"创建新的向量索引: {self.index_key}")

        except Exception as e:
            logger.error(f"Redis初始化失败: {e}")
            raise RuntimeError(f"无法连接到Redis: {e}")

    def _generate_doc_id(self, content: str) -> str:
        """生成文档ID"""
        return hashlib.md5(content.encode()).hexdigest()

    def _serialize_vector(self, vector: np.ndarray) -> bytes:
        """序列化向量"""
        return vector.astype(np.float32).tobytes()

    def _deserialize_vector(self, vector_bytes: bytes) -> np.ndarray:
        """反序列化向量"""
        return np.frombuffer(vector_bytes, dtype=np.float32)

    def _save_vector_to_local(self, doc_id: str, vector: np.ndarray):
        """保存向量到本地文件"""
        try:
            vector_file = Path(self.local_storage_path) / f"{doc_id}.npy"
            np.save(vector_file, vector)
        except Exception as e:
            logger.warning(f"保存向量到本地失败 {doc_id}: {e}")

    def _load_vector_from_local(self, doc_id: str) -> Optional[np.ndarray]:
        """从本地文件加载向量"""
        try:
            vector_file = Path(self.local_storage_path) / f"{doc_id}.npy"
            if vector_file.exists():
                return np.load(vector_file)
        except Exception as e:
            logger.warning(f"从本地加载向量失败 {doc_id}: {e}")
        return None

    def add_documents(
        self, documents: List[Document], batch_size: int = 50
    ) -> List[str]:
        """批量添加文档"""
        if not documents:
            return []

        logger.info(f"开始添加 {len(documents)} 个文档到向量存储")

        # 生成嵌入向量
        doc_texts = [doc.page_content for doc in documents]
        try:
            embeddings = self.embedding_model.embed_documents(doc_texts)
        except Exception as e:
            logger.error(f"生成嵌入向量失败: {e}")
            raise

        doc_ids = []

        # 分批处理
        for i in range(0, len(documents), batch_size):
            batch_docs = documents[i : i + batch_size]
            batch_embeddings = embeddings[i : i + batch_size]

            with self.redis_client.pipeline() as pipe:
                for doc, embedding in zip(batch_docs, batch_embeddings):
                    doc_id = self._generate_doc_id(doc.page_content)

                    # 存储文档内容
                    pipe.hset(
                        f"{self.doc_key_prefix}{doc_id}",
                        mapping={
                            "content": doc.page_content,
                            "timestamp": int(time.time()),
                        },
                    )

                    # 存储元数据
                    if doc.metadata:
                        pipe.hset(
                            f"{self.meta_key_prefix}{doc_id}", mapping=doc.metadata
                        )

                    # 存储向量到Redis
                    vector_bytes = self._serialize_vector(np.array(embedding))
                    pipe.set(f"{self.vector_key_prefix}{doc_id}", vector_bytes)

                    # 添加到索引
                    pipe.sadd(self.index_key, doc_id)

                    doc_ids.append(doc_id)

                    # 同时保存向量到本地
                    self._save_vector_to_local(doc_id, np.array(embedding))

                # 执行批量操作
                pipe.execute()

        logger.info(f"成功添加 {len(doc_ids)} 个文档")
        return doc_ids

    def similarity_search(
        self,
        query: str,
        k: int = 4,
        similarity_threshold: float = 0.2,  # 大幅降低阈值提升召回率
        filter_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[Document, float]]:
        """相似度搜索"""
        try:
            # 生成查询向量
            query_embedding = self.embedding_model.embed_query(query)
            query_vector = np.array(query_embedding)

            # 获取所有文档ID
            doc_ids = self.redis_client.smembers(self.index_key)
            doc_ids = [
                doc_id.decode() if isinstance(doc_id, bytes) else doc_id
                for doc_id in doc_ids
            ]
            doc_ids = [doc_id for doc_id in doc_ids if doc_id]  # 移除空字符串

            if not doc_ids:
                logger.warning("向量存储中没有文档")
                return []

            # 计算相似度
            similarities = []

            with ThreadPoolExecutor(max_workers=4) as executor:

                def process_doc(doc_id):
                    try:
                        # 尝试从Redis获取向量
                        vector_bytes = self.redis_client.get(
                            f"{self.vector_key_prefix}{doc_id}"
                        )

                        if vector_bytes:
                            doc_vector = self._deserialize_vector(vector_bytes)
                        else:
                            # 回退到本地存储
                            doc_vector = self._load_vector_from_local(doc_id)
                            if doc_vector is None:
                                return None

                        # 计算余弦相似度
                        similarity = self._cosine_similarity(query_vector, doc_vector)

                        if similarity >= similarity_threshold:
                            return (doc_id, similarity)
                    except Exception as e:
                        logger.warning(f"处理文档 {doc_id} 时出错: {e}")
                    return None

                results = list(executor.map(process_doc, doc_ids))
                similarities = [r for r in results if r is not None]

            # 按相似度排序并取前k个
            similarities.sort(key=lambda x: x[1], reverse=True)
            top_similarities = similarities[:k]

            # 获取文档内容和元数据
            documents_with_scores = []

            for doc_id, similarity in top_similarities:
                try:
                    # 获取文档内容
                    doc_data = self.redis_client.hgetall(
                        f"{self.doc_key_prefix}{doc_id}"
                    )
                    if not doc_data:
                        continue

                    content = doc_data.get(b"content", b"").decode("utf-8")

                    # 获取元数据
                    metadata = {}
                    meta_data = self.redis_client.hgetall(
                        f"{self.meta_key_prefix}{doc_id}"
                    )
                    if meta_data:
                        for key, value in meta_data.items():
                            key_str = (
                                key.decode("utf-8") if isinstance(key, bytes) else key
                            )
                            value_str = (
                                value.decode("utf-8")
                                if isinstance(value, bytes)
                                else value
                            )
                            metadata[key_str] = value_str

                    # 应用元数据过滤
                    if filter_metadata:
                        if not all(
                            metadata.get(k) == v for k, v in filter_metadata.items()
                        ):
                            continue

                    document = Document(page_content=content, metadata=metadata)
                    documents_with_scores.append((document, similarity))

                except Exception as e:
                    logger.warning(f"获取文档 {doc_id} 详情时出错: {e}")
                    continue

            logger.info(f"相似度搜索返回 {len(documents_with_scores)} 个结果")
            return documents_with_scores

        except redis.ConnectionError as e:
            logger.error(f"Redis连接错误: {e}")
            # 如果Redis不可用，尝试从本地文件降级处理
            return self._fallback_local_search(query, k, similarity_threshold)
        except Exception as e:
            logger.error(f"相似度搜索失败: {e}")
            return []

    def _fallback_local_search(
        self, query: str, k: int, similarity_threshold: float
    ) -> List[Tuple[Document, float]]:
        """降级到本地搜索的备用方案"""
        try:
            logger.info("Redis不可用，使用本地文件进行搜索")

            # 生成查询向量
            query_embedding = self.embedding_model.embed_query(query)
            query_vector = np.array(query_embedding)

            # 扫描本地向量文件
            local_storage = Path(self.local_storage_path)
            if not local_storage.exists():
                logger.warning("本地向量存储路径不存在")
                return []

            similarities = []

            for vector_file in local_storage.glob("*.npy"):
                try:
                    doc_id = vector_file.stem
                    doc_vector = np.load(vector_file)

                    # 计算相似度
                    similarity = self._cosine_similarity(query_vector, doc_vector)

                    if similarity >= similarity_threshold:
                        # 尝试构造基本文档（内容可能不完整）
                        document = Document(
                            page_content=f"文档ID: {doc_id} (本地降级模式，内容不完整)",
                            metadata={"doc_id": doc_id, "fallback_mode": True},
                        )
                        similarities.append((document, similarity))

                except Exception as e:
                    logger.warning(f"处理本地向量文件 {vector_file} 失败: {e}")
                    continue

            # 排序并返回前k个
            similarities.sort(key=lambda x: x[1], reverse=True)
            return similarities[:k]

        except Exception as e:
            logger.error(f"本地降级搜索也失败: {e}")
            return []

    def _cosine_similarity(self, vec1: np.ndarray, vec2: np.ndarray) -> float:
        """计算余弦相似度"""
        try:
            # 确保向量长度一致
            if len(vec1) != len(vec2):
                logger.warning(f"向量维度不匹配: {len(vec1)} vs {len(vec2)}")
                return 0.0

            # 计算余弦相似度
            dot_product = np.dot(vec1, vec2)
            norm1 = np.linalg.norm(vec1)
            norm2 = np.linalg.norm(vec2)

            if norm1 == 0 or norm2 == 0:
                return 0.0

            return dot_product / (norm1 * norm2)
        except Exception as e:
            logger.warning(f"计算余弦相似度时出错: {e}")
            return 0.0

    def delete_documents(self, doc_ids: List[str]):
        """删除文档"""
        if not doc_ids:
            return

        with self.redis_client.pipeline() as pipe:
            for doc_id in doc_ids:
                # 删除文档内容
                pipe.delete(f"{self.doc_key_prefix}{doc_id}")
                # 删除元数据
                pipe.delete(f"{self.meta_key_prefix}{doc_id}")
                # 删除向量
                pipe.delete(f"{self.vector_key_prefix}{doc_id}")
                # 从索引中移除
                pipe.srem(self.index_key, doc_id)

                # 删除本地向量文件
                try:
                    vector_file = Path(self.local_storage_path) / f"{doc_id}.npy"
                    if vector_file.exists():
                        vector_file.unlink()
                except Exception as e:
                    logger.warning(f"删除本地向量文件失败 {doc_id}: {e}")

            pipe.execute()

        logger.info(f"删除了 {len(doc_ids)} 个文档")

    def get_document_count(self) -> int:
        """获取文档数量"""
        try:
            return self.redis_client.scard(self.index_key) - 1  # 减去空字符串
        except Exception as e:
            logger.error(f"获取文档数量失败: {e}")
            return 0

    def clear_collection(self):
        """清空集合"""
        try:
            # 获取所有文档ID
            doc_ids = self.redis_client.smembers(self.index_key)
            doc_ids = [
                doc_id.decode() if isinstance(doc_id, bytes) else doc_id
                for doc_id in doc_ids
            ]
            doc_ids = [doc_id for doc_id in doc_ids if doc_id]  # 移除空字符串

            if doc_ids:
                self.delete_documents(doc_ids)

            # 清空索引
            self.redis_client.delete(self.index_key)
            self.redis_client.sadd(self.index_key, "")  # 重新创建空集合

            logger.info(f"清空了集合 {self.collection_name}")

        except Exception as e:
            logger.error(f"清空集合失败: {e}")

    def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        try:
            # Redis连接检查
            self.redis_client.ping()

            # 获取统计信息
            doc_count = self.get_document_count()

            return {
                "status": "healthy",
                "redis_connected": True,
                "document_count": doc_count,
                "collection_name": self.collection_name,
                "local_storage_path": str(self.local_storage_path),
            }

        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "unhealthy", "error": str(e), "redis_connected": False}

    def close(self):
        """关闭连接"""
        try:
            self.connection_pool.disconnect()
            logger.info("Redis连接池已关闭")
        except Exception as e:
            logger.warning(f"关闭Redis连接时出错: {e}")


class OptimizedRedisVectorStore(RedisVectorStore):
    """优化的Redis向量存储类，集成FAISS索引和智能缓存"""

    def __init__(
        self,
        redis_config: Dict[str, Any],
        collection_name: str,
        embedding_model: Embeddings,
        vector_dim: int = 1536,
        local_storage_path: Optional[str] = None,
        use_faiss: bool = True,
        faiss_index_type: str = "Flat",
    ):
        """
        初始化优化的Redis向量存储

        Args:
            redis_config: Redis连接配置
            collection_name: 集合名称
            embedding_model: 嵌入模型
            vector_dim: 向量维度
            local_storage_path: 本地存储路径
            use_faiss: 是否使用FAISS索引
            faiss_index_type: FAISS索引类型 (Flat, IVF, HNSW)
        """
        super().__init__(
            redis_config,
            collection_name,
            embedding_model,
            vector_dim,
            local_storage_path,
        )

        self.use_faiss = use_faiss and FAISS_AVAILABLE
        if use_faiss and not FAISS_AVAILABLE:
            logger.warning("FAISS未安装，禁用FAISS功能")

        self.faiss_index_type = faiss_index_type
        self.faiss_index = None
        self.doc_id_to_faiss_id = {}
        self.faiss_id_to_doc_id = {}

        # FAISS相关路径
        self.faiss_index_path = (
            Path(self.local_storage_path) / f"{collection_name}_faiss.index"
        )
        self.faiss_mapping_path = (
            Path(self.local_storage_path) / f"{collection_name}_mapping.pkl"
        )

        # TF-IDF索引用于混合检索
        if SKLEARN_AVAILABLE:
            self.tfidf_vectorizer = TfidfVectorizer(
                max_features=5000,
                stop_words=None,
                ngram_range=(1, 3),  # 使用三元组
                min_df=1,
                max_df=0.95,
            )
            self.tfidf_matrix = None
            self.tfidf_doc_ids = []
        else:
            logger.warning("sklearn未安装，禁用TF-IDF功能")
            self.tfidf_vectorizer = None
            self.tfidf_matrix = None
            self.tfidf_doc_ids = []

        # 初始化FAISS索引
        if self.use_faiss:
            self._initialize_faiss_index()

    def _initialize_faiss_index(self):
        """初始化FAISS索引"""
        if not FAISS_AVAILABLE:
            logger.warning("FAISS未安装，跳过初始化")
            self.use_faiss = False
            return

        try:
            # 尝试加载现有索引
            if self.faiss_index_path.exists() and self.faiss_mapping_path.exists():
                try:
                    import importlib

                    faiss = importlib.import_module("faiss")
                    self.faiss_index = faiss.read_index(str(self.faiss_index_path))
                    with open(self.faiss_mapping_path, "rb") as f:
                        mapping_data = pickle.load(f)
                        self.doc_id_to_faiss_id = mapping_data["doc_id_to_faiss_id"]
                        self.faiss_id_to_doc_id = mapping_data["faiss_id_to_doc_id"]
                    logger.info(
                        f"FAISS索引加载成功，包含 {self.faiss_index.ntotal} 个向量"
                    )
                except Exception as e:
                    logger.warning(f"加载FAISS索引失败: {e}，将创建新索引")
                    self._create_faiss_index()
            else:
                # 创建新索引
                self._create_faiss_index()

        except Exception as e:
            logger.error(f"FAISS索引初始化失败: {e}")
            self.use_faiss = False

    def _create_faiss_index(self):
        """创建 FAISS 索引"""
        if not FAISS_AVAILABLE:
            logger.warning("FAISS不可用，禁用FAISS功能")
            self.use_faiss = False
            return

        try:
            import importlib

            faiss = importlib.import_module("faiss")
            if self.faiss_index_type == "Flat":
                self.faiss_index = faiss.IndexFlatIP(self.vector_dim)  # 内积相似度
            elif self.faiss_index_type == "IVF":
                # IVF索引，适合中等规模
                nlist = min(100, max(10, int(np.sqrt(1000))))  # 适应性设置
                quantizer = faiss.IndexFlatIP(self.vector_dim)
                self.faiss_index = faiss.IndexIVFFlat(quantizer, self.vector_dim, nlist)
            elif self.faiss_index_type == "HNSW":
                # HNSW索引，适合大规模和高精度
                self.faiss_index = faiss.IndexHNSWFlat(self.vector_dim, 32)
                self.faiss_index.hnsw.efConstruction = 40
                self.faiss_index.hnsw.efSearch = 16
            else:
                # 默认使用Flat
                self.faiss_index = faiss.IndexFlatIP(self.vector_dim)

            logger.info(
                f"创建了 {self.faiss_index_type} 类型FAISS索引，维度: {self.vector_dim}"
            )

        except Exception as e:
            logger.error(f"创建FAISS索引失败: {e}")
            self.use_faiss = False

    def _save_faiss_index(self):
        """保存FAISS索引"""
        try:
            if self.faiss_index is not None:
                import importlib

                faiss = importlib.import_module("faiss")
                faiss.write_index(self.faiss_index, str(self.faiss_index_path))

                mapping_data = {
                    "doc_id_to_faiss_id": self.doc_id_to_faiss_id,
                    "faiss_id_to_doc_id": self.faiss_id_to_doc_id,
                }

                with open(self.faiss_mapping_path, "wb") as f:
                    pickle.dump(mapping_data, f)

                logger.debug("FAISS索引保存成功")

        except Exception as e:
            logger.error(f"保存FAISS索引失败: {e}")

    def add_documents(
        self, documents: List[Document], batch_size: int = 50
    ) -> List[str]:
        """批量添加文档（优化版）"""
        if not documents:
            return []

        logger.info(f"开始添加 {len(documents)} 个文档到优化向量存储")

        # 生成嵌入向量
        doc_texts = [doc.page_content for doc in documents]
        try:
            embeddings = self.embedding_model.embed_documents(doc_texts)
        except Exception as e:
            logger.error(f"生成嵌入向量失败: {e}")
            raise

        # 调用父类方法存储到Redis
        doc_ids = super().add_documents(documents, batch_size)

        # 添加到FAISS索引
        if self.use_faiss and self.faiss_index is not None:
            self._add_to_faiss_index(doc_ids, embeddings)

        # 更新TF-IDF索引
        self._update_tfidf_index()

        return doc_ids

    def _add_to_faiss_index(self, doc_ids: List[str], embeddings: List[List[float]]):
        """添加向量到FAISS索引"""
        if not FAISS_AVAILABLE:
            logger.debug("FAISS不可用，跳过FAISS索引添加")
            return

        try:
            vectors = np.array(embeddings, dtype=np.float32)

            # 检查向量维度是否匹配
            if vectors.shape[1] != self.vector_dim:
                logger.error(
                    f"向量维度不匹配: 期望 {self.vector_dim}, 实际 {vectors.shape[1]}"
                )
                return

            # 如果是IVF索引且未训练，先训练
            if (
                hasattr(self.faiss_index, "is_trained")
                and not self.faiss_index.is_trained
                and len(embeddings) >= 100
            ):  # 需要足够的数据进行训练
                logger.info("开始训练FAISS IVF索引...")
                self.faiss_index.train(vectors)
                logger.info("FAISS IVF索引训练完成")

            # 添加向量
            if (
                not hasattr(self.faiss_index, "is_trained")
                or self.faiss_index.is_trained
            ):
                start_id = self.faiss_index.ntotal
                self.faiss_index.add(vectors)

                # 更新映射
                for i, doc_id in enumerate(doc_ids):
                    faiss_id = start_id + i
                    self.doc_id_to_faiss_id[doc_id] = faiss_id
                    self.faiss_id_to_doc_id[faiss_id] = doc_id

                # 保存索引
                self._save_faiss_index()

                logger.info(f"添加了 {len(doc_ids)} 个向量到FAISS索引")
            else:
                logger.warning("FAISS索引未训练，跳过添加")

        except Exception as e:
            logger.error(f"添加向量FAISS索引失败: {str(e)}", exc_info=True)

    def _update_tfidf_index(self):
        """更新TF-IDF索引"""
        if not SKLEARN_AVAILABLE or self.tfidf_vectorizer is None:
            logger.debug("sklearn未安装或TF-IDF向量化器未初始化，跳过TF-IDF索引更新")
            return

        try:
            # 获取所有文档内容
            doc_ids = self.redis_client.smembers(self.index_key)
            doc_ids = [
                doc_id.decode() if isinstance(doc_id, bytes) else doc_id
                for doc_id in doc_ids
            ]
            doc_ids = [doc_id for doc_id in doc_ids if doc_id]  # 移除空字符串

            if not doc_ids:
                return

            # 获取文档内容
            doc_contents = []
            valid_doc_ids = []

            for doc_id in doc_ids:
                try:
                    doc_data = self.redis_client.hgetall(
                        f"{self.doc_key_prefix}{doc_id}"
                    )
                    if doc_data:
                        content = doc_data.get(b"content", b"").decode("utf-8")
                        if content:
                            doc_contents.append(content)
                            valid_doc_ids.append(doc_id)
                except Exception as e:
                    logger.warning(f"获取文档内容失败 {doc_id}: {e}")
                    continue

            if doc_contents:
                # 训练TF-IDF模型
                self.tfidf_matrix = self.tfidf_vectorizer.fit_transform(doc_contents)
                self.tfidf_doc_ids = valid_doc_ids
                logger.info(f"TF-IDF索引更新完成，包含 {len(valid_doc_ids)} 个文档")

        except Exception as e:
            logger.error(f"更新TF-IDF索引失败: {e}")

    def _keyword_search(self, query: str, k: int) -> List[Tuple[Document, float]]:
        """关键词搜索 - 用于提升召回率"""
        try:
            # 提取查询中的关键词
            query_words = query.lower().split()
            query_words = [word for word in query_words if len(word) > 2]  # 过滤短词

            if not query_words:
                return []

            # 获取所有文档ID
            doc_ids = self.redis_client.smembers(self.index_key)
            doc_ids = [
                doc_id.decode() if isinstance(doc_id, bytes) else doc_id
                for doc_id in doc_ids
            ]
            doc_ids = [doc_id for doc_id in doc_ids if doc_id]  # 移除空字符串

            if not doc_ids:
                return []

            # 计算关键词匹配分数
            scored_docs = []

            for doc_id in doc_ids:
                try:
                    # 获取文档内容
                    doc_data = self.redis_client.hgetall(
                        f"{self.doc_key_prefix}{doc_id}"
                    )
                    if not doc_data:
                        continue

                    content = doc_data.get(b"content", b"").decode("utf-8").lower()

                    # 计算关键词匹配分数
                    word_matches = 0
                    for word in query_words:
                        if word in content:
                            word_matches += content.count(word)

                    if word_matches > 0:
                        # 计算归一化分数
                        content_words = len(content.split())
                        score = min(
                            word_matches / max(content_words, 1) * 5, 1.0
                        )  # 归一化并限制在[0,1]

                        # 获取完整文档
                        metadata = {}
                        meta_data = self.redis_client.hgetall(
                            f"{self.meta_key_prefix}{doc_id}"
                        )
                        if meta_data:
                            for key, value in meta_data.items():
                                key_str = (
                                    key.decode("utf-8")
                                    if isinstance(key, bytes)
                                    else key
                                )
                                value_str = (
                                    value.decode("utf-8")
                                    if isinstance(value, bytes)
                                    else value
                                )
                                metadata[key_str] = value_str

                        original_content = doc_data.get(b"content", b"").decode("utf-8")
                        document = Document(
                            page_content=original_content, metadata=metadata
                        )
                        scored_docs.append((document, score))

                except Exception as e:
                    logger.warning(f"关键词搜索处理文档 {doc_id} 失败: {e}")
                    continue

            # 按分数排序并返回前k个
            scored_docs.sort(key=lambda x: x[1], reverse=True)
            return scored_docs[:k]

        except Exception as e:
            logger.error(f"关键词搜索失败: {e}")
            return []

    def hybrid_similarity_search(
        self,
        query: str,
        k: int = 4,
        semantic_weight: float = 0.6,  # 降低语义权重
        lexical_weight: float = 0.4,  # 增加词汇权重
        similarity_threshold: float = 0.3,
    ) -> List[Tuple[Document, float]]:  # 大幅降低阈值
        """混合相似度搜索（语义+词汇） - 提升召回率优化版"""
        try:
            # 1. 语义搜索（使用FAISS或余弦相似度）
            semantic_results = []
            if (
                self.use_faiss
                and self.faiss_index is not None
                and self.faiss_index.ntotal > 0
            ):
                semantic_results = self._faiss_search(query, k * 3)  # 增加搜索数量
                logger.debug(f"FAISS语义搜索返回 {len(semantic_results)} 个结果")
            else:
                semantic_results = self.similarity_search(
                    query, k * 3, similarity_threshold * 0.5
                )  # 进一步降低阈值
                logger.debug(f"基础语义搜索返回 {len(semantic_results)} 个结果")

            # 2. 词汇搜索（使用TF-IDF）
            lexical_results = []
            if self.tfidf_matrix is not None and len(self.tfidf_doc_ids) > 0:
                lexical_results = self._tfidf_search(query, k * 3)  # 增加搜索数量
                logger.debug(f"TF-IDF词汇搜索返回 {len(lexical_results)} 个结果")
            else:
                logger.debug("TF-IDF索引不可用")

            # 3. 如果结果仍然不足，尝试关键词搜索
            all_results = semantic_results + lexical_results
            if len(all_results) < k:
                keyword_results = self._keyword_search(query, k * 2)
                all_results.extend(keyword_results)
                logger.debug(f"关键词搜索额外返回 {len(keyword_results)} 个结果")

            # 4. 如果两种搜索都没有结果，降级到最宽松的基础搜索
            if not all_results:
                logger.warning("混合搜索无结果，使用最宽松的基础搜索")
                basic_results = self.similarity_search(query, k * 2, 0.1)  # 极低阈值
                return basic_results

            # 5. 合并和重排序
            if semantic_results or lexical_results:
                final_results = self._merge_search_results(
                    semantic_results,
                    lexical_results,
                    semantic_weight,
                    lexical_weight,
                    k * 3,
                )

                # 6. 宽松的阈值过滤
                filtered_results = [
                    (doc, score)
                    for doc, score in final_results
                    if score >= similarity_threshold
                ]

                # 如果过滤后结果太少，大幅降低阈值
                if len(filtered_results) < k // 2 and final_results:
                    min_threshold = similarity_threshold * 0.3  # 大幅降低阈值
                    filtered_results = [
                        (doc, score)
                        for doc, score in final_results
                        if score >= min_threshold
                    ]
                    logger.debug(
                        f"降低阈值到 {min_threshold} 后获得 {len(filtered_results)} 个结果"
                    )

                # 确保至少返回一些结果
                if len(filtered_results) < k // 2 and final_results:
                    filtered_results = final_results[:k]
                    logger.debug(f"返回前 {k} 个未过滤结果确保召回率")

                logger.info(f"混合搜索最终返回 {len(filtered_results)} 个结果")
                return filtered_results[:k]

            # 最后的降级方案
            logger.warning("混合搜索完全失败，使用极宽松基础搜索")
            return self.similarity_search(query, k, 0.1)

        except Exception as e:
            logger.error(f"混合相似度搜索失败: {e}")
            # 降级到极宽松的基础搜索
            return self.similarity_search(query, k, 0.1)

    def _faiss_search(self, query: str, k: int) -> List[Tuple[Document, float]]:
        """使用FAISS进行快速语义搜索"""
        try:
            # 生成查询向量
            query_embedding = self.embedding_model.embed_query(query)
            query_vector = np.array([query_embedding], dtype=np.float32)

            # FAISS搜索
            scores, indices = self.faiss_index.search(
                query_vector, min(k, self.faiss_index.ntotal)
            )

            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx == -1:  # FAISS返回-1表示无效结果
                    continue

                doc_id = self.faiss_id_to_doc_id.get(idx)
                if not doc_id:
                    continue

                # 获取文档
                try:
                    doc_data = self.redis_client.hgetall(
                        f"{self.doc_key_prefix}{doc_id}"
                    )
                    if not doc_data:
                        continue

                    content = doc_data.get(b"content", b"").decode("utf-8")

                    # 获取元数据
                    metadata = {}
                    meta_data = self.redis_client.hgetall(
                        f"{self.meta_key_prefix}{doc_id}"
                    )
                    if meta_data:
                        for key, value in meta_data.items():
                            key_str = (
                                key.decode("utf-8") if isinstance(key, bytes) else key
                            )
                            value_str = (
                                value.decode("utf-8")
                                if isinstance(value, bytes)
                                else value
                            )
                            metadata[key_str] = value_str

                    document = Document(page_content=content, metadata=metadata)
                    # FAISS返回的是内积分数，转换为相似度 (0-1)
                    similarity = max(0, min(1, score))
                    results.append((document, similarity))

                except Exception as e:
                    logger.warning(f"获取FAISS搜索文档失败 {doc_id}: {e}")
                    continue

            return results

        except Exception as e:
            logger.error(f"FAISS搜索失败: {e}")
            return []

    def _tfidf_search(self, query: str, k: int) -> List[Tuple[Document, float]]:
        """使用TF-IDF进行词汇搜索"""
        if not SKLEARN_AVAILABLE or self.tfidf_matrix is None or not self.tfidf_doc_ids:
            logger.debug("TF-IDF功能不可用，跳过词汇搜索")
            return []

        try:
            # 转换查询为TF-IDF向量
            query_vector = self.tfidf_vectorizer.transform([query])

            # 计算相似度
            similarities = cosine_similarity(query_vector, self.tfidf_matrix).flatten()

            # 获取前k个最相似的文档
            top_indices = np.argsort(similarities)[::-1][:k]

            results = []
            for idx in top_indices:
                similarity = similarities[idx]
                if similarity <= 0:
                    continue

                doc_id = self.tfidf_doc_ids[idx]

                try:
                    # 获取文档
                    doc_data = self.redis_client.hgetall(
                        f"{self.doc_key_prefix}{doc_id}"
                    )
                    if not doc_data:
                        continue

                    content = doc_data.get(b"content", b"").decode("utf-8")

                    # 获取元数据
                    metadata = {}
                    meta_data = self.redis_client.hgetall(
                        f"{self.meta_key_prefix}{doc_id}"
                    )
                    if meta_data:
                        for key, value in meta_data.items():
                            key_str = (
                                key.decode("utf-8") if isinstance(key, bytes) else key
                            )
                            value_str = (
                                value.decode("utf-8")
                                if isinstance(value, bytes)
                                else value
                            )
                            metadata[key_str] = value_str

                    document = Document(page_content=content, metadata=metadata)
                    results.append((document, similarity))

                except Exception as e:
                    logger.warning(f"获取TF-IDF搜索文档失败 {doc_id}: {e}")
                    continue

            return results

        except Exception as e:
            logger.error(f"TF-IDF搜索失败: {e}")
            return []

    def _merge_search_results(
        self,
        semantic_results: List[Tuple[Document, float]],
        lexical_results: List[Tuple[Document, float]],
        semantic_weight: float,
        lexical_weight: float,
        k: int,
    ) -> List[Tuple[Document, float]]:
        """合并语义和词汇搜索结果"""
        try:
            # 创建文档ID到分数的映射
            semantic_scores = {}
            lexical_scores = {}
            all_docs = {}

            # 处理语义搜索结果
            for doc, score in semantic_results:
                doc_id = self._generate_doc_id(doc.page_content)
                semantic_scores[doc_id] = score
                all_docs[doc_id] = doc

            # 处理词汇搜索结果
            for doc, score in lexical_results:
                doc_id = self._generate_doc_id(doc.page_content)
                lexical_scores[doc_id] = score
                all_docs[doc_id] = doc

            # 计算混合分数
            merged_results = []
            for doc_id, doc in all_docs.items():
                semantic_score = semantic_scores.get(doc_id, 0)
                lexical_score = lexical_scores.get(doc_id, 0)

                # 加权平均
                hybrid_score = (
                    semantic_score * semantic_weight + lexical_score * lexical_weight
                )

                merged_results.append((doc, hybrid_score))

            # 按分数排序
            merged_results.sort(key=lambda x: x[1], reverse=True)

            return merged_results[:k]

        except Exception as e:
            logger.error(f"合并搜索结果失败: {e}")
            return semantic_results[:k] if semantic_results else lexical_results[:k]


class RedisVectorStoreManager:
    """Redis向量存储管理器"""

    def __init__(
        self,
        redis_config: Dict[str, Any],
        collection_name: str,
        embedding_dimensions: int = 1536,
        local_storage_path: Optional[str] = None,
    ):
        self.redis_config = redis_config
        self.collection_name = collection_name
        self.embedding_dimensions = embedding_dimensions
        self.local_storage_path = local_storage_path
        self.vector_store = None
        self._lock = threading.Lock()
        self.client = None
        self.embedding_model = None  # 添加嵌入模型属性
        self._init_client()

    def _init_client(self):
        """初始化Redis客户端"""
        try:
            # Redis连接配置
            # 注意：redis-py库使用socket_connect_timeout而不是connection_timeout，需要处理参数名不匹配
            config_copy = self.redis_config.copy()
            if (
                "connection_timeout" in config_copy
                and "socket_connect_timeout" not in config_copy
            ):
                config_copy["socket_connect_timeout"] = config_copy.pop(
                    "connection_timeout"
                )

            # 创建两个不同的客户端：一个用于文本操作（自动解码），一个用于二进制操作（不解码）
            # 1. 用于向量操作的二进制客户端
            binary_config = config_copy.copy()
            binary_config["decode_responses"] = False
            self.binary_client = redis.Redis(**binary_config)
            self.binary_client.ping()  # 测试连接

            # 2. 用于文本操作的客户端（可能自动解码，取决于原始配置）
            self.client = redis.Redis(**config_copy)
            self.client.ping()  # 测试连接

            logger.info(f"Redis连接成功，使用集合: {self.collection_name}")
            return True
        except redis.ConnectionError as e:
            logger.error(f"Redis直接连接失败: {e}")
            self.client = None
            self.binary_client = None
            return False
        except Exception as e:
            logger.error(f"Redis初始化失败: {e}")
            self.client = None
            self.binary_client = None
            return False

    def set_embedding_model(self, embedding_model: Embeddings) -> None:
        """设置嵌入模型

        Args:
            embedding_model: 嵌入模型实例
        """
        self.embedding_model = embedding_model
        # 如果已经创建了向量存储，更新它的embedding_model
        if self.vector_store:
            self.vector_store.embedding_model = embedding_model
            logger.info("已更新向量存储的嵌入模型")

    def get_vector_store(self) -> RedisVectorStore:
        """获取向量存储实例（单例模式）"""
        if self.vector_store is None:
            with self._lock:
                if self.vector_store is None:
                    # 创建一个默认的嵌入模型，如果未提供
                    from app.core.agents.assistant import FallbackEmbeddings

                    # 获取嵌入模型，优先使用自身的embedding_model属性
                    embedding_model = self.embedding_model

                    # 如果自身没有嵌入模型，尝试从调用栈获取
                    if embedding_model is None:
                        # 如果在app.core.agents.assistant模块中的VectorStoreManager中有embedding_model属性
                        from app.core.agents.assistant import (
                            VectorStoreManager as AssistantVSM,
                        )

                        # 使用一个安全的方式获取嵌入模型
                        try:
                            # 尝试获取全局实例的嵌入模型
                            import inspect

                            stack = inspect.stack()
                            for frame_info in stack:
                                frame = frame_info.frame
                                if "self" in frame.f_locals:
                                    obj = frame.f_locals["self"]
                                    if isinstance(obj, AssistantVSM) and hasattr(
                                        obj, "embedding_model"
                                    ):
                                        embedding_model = obj.embedding_model
                                        logger.info("从调用栈获取到嵌入模型")
                                        break
                        except Exception:
                            pass

                    # 如果没有找到嵌入模型，使用备用模型
                    if embedding_model is None:
                        logger.warning("未找到嵌入模型，使用备用模型")
                        embedding_model = FallbackEmbeddings(
                            dimensions=self.embedding_dimensions
                        )

                    self.vector_store = RedisVectorStore(
                        redis_config=self.redis_config,
                        collection_name=self.collection_name,
                        embedding_model=embedding_model,
                        vector_dim=self.embedding_dimensions,
                        local_storage_path=self.local_storage_path,
                    )
        return self.vector_store

    def add_documents(self, documents: List[Document]) -> List[str]:
        """添加文档"""
        vector_store = self.get_vector_store()
        return vector_store.add_documents(documents)

    def similarity_search(
        self, query: str, k: int = 4, **kwargs
    ) -> List[Tuple[Document, float]]:
        """相似度搜索"""
        vector_store = self.get_vector_store()
        return vector_store.similarity_search(query, k, **kwargs)

    def get_retriever(self, **kwargs):
        """获取检索器（为了保持与原有接口兼容）"""
        vector_store = self.get_vector_store()

        class RedisRetriever:
            def __init__(self, vs):
                self.vector_store = vs

            def get_relevant_documents(
                self, query: str, **search_kwargs
            ) -> List[Document]:
                results = self.vector_store.similarity_search(query, **search_kwargs)
                return [doc for doc, _ in results]

            def invoke(self, query) -> List[Document]:
                # 兼容字符串查询和字典输入
                if isinstance(query, dict):
                    query_str = query.get("query", "")
                elif isinstance(query, str):
                    query_str = query
                else:
                    query_str = str(query)
                return self.get_relevant_documents(query_str)

        return RedisRetriever(vector_store)

    def get_document_count(self) -> int:
        """获取文档数量

        Returns:
            int: 文档数量
        """
        try:
            if self.client is None:
                self._init_client()

            if self.client is None:
                return 0

            # 尝试获取文档数量
            count = self.client.scard(f"index:{self.collection_name}")
            return max(0, count - 1)  # 减去空字符串
        except Exception as e:
            logger.error(f"获取文档数量失败: {str(e)}")
            return 0

    def get_vector_dimension(self) -> Optional[int]:
        """获取向量维度

        Returns:
            Optional[int]: 向量维度，如果没有向量则返回None
        """
        try:
            if (
                not self.client
                or not hasattr(self, "binary_client")
                or not self.binary_client
            ):
                self._init_client()

            if (
                not self.client
                or not hasattr(self, "binary_client")
                or not self.binary_client
            ):
                logger.error("Redis客户端未初始化，无法获取向量维度")
                return None

            # 使用二进制客户端处理向量数据
            # 获取索引中的文档ID
            index_key = f"index:{self.collection_name}"
            doc_ids_set = self.binary_client.smembers(index_key)

            if not doc_ids_set:
                logger.warning(f"索引 {index_key} 中没有文档ID")
                return None

            # 安全地处理文档ID
            clean_doc_ids = []
            for doc_id in doc_ids_set:
                if doc_id == b"" or doc_id == "":  # 跳过空字符串
                    continue

                try:
                    # 尝试作为ASCII处理文档ID
                    if isinstance(doc_id, bytes):
                        doc_id_str = doc_id.decode("ascii", errors="ignore")
                    else:
                        doc_id_str = str(doc_id)

                    if doc_id_str:  # 确保解码后不为空
                        clean_doc_ids.append(doc_id_str)
                except Exception as e:
                    logger.warning(f"跳过无法解码的文档ID: {e}")

            if not clean_doc_ids:
                logger.warning(f"索引 {index_key} 中没有有效的文档ID")
                return None

            # 尝试获取第一个向量
            for doc_id in clean_doc_ids[:3]:  # 只尝试前3个文档ID
                try:
                    vector_key = f"vector:{self.collection_name}:{doc_id}"
                    vector_bytes = self.binary_client.get(vector_key)

                    if vector_bytes:
                        # 解析向量维度 - 直接处理二进制数据
                        vector = np.frombuffer(vector_bytes, dtype=np.float32)
                        vector_dim = len(vector)
                        logger.debug(f"向量维度: {vector_dim}")
                        return vector_dim
                except Exception as e:
                    logger.warning(f"尝试获取向量 {doc_id} 的维度时出错: {e}")
                    continue

            logger.warning("未找到有效向量数据")
            return None

        except Exception as e:
            logger.error(f"获取向量维度失败: {str(e)}")
            return None

    def clear_collection(self) -> bool:
        """清理整个集合

        Returns:
            bool: 清理成功返回True，否则返回False
        """
        try:
            if self.client is None:
                self._init_client()

            if self.client is None:
                return False

            # 删除所有相关的键
            prefix = f"*:{self.collection_name}*"
            cursor = 0
            deleted = 0

            while True:
                cursor, keys = self.client.scan(cursor, prefix, count=100)
                if keys:
                    deleted += self.client.delete(*keys)
                if cursor == 0:
                    break

            logger.info(f"已清理 {deleted} 个键")

            # 重置向量存储
            self.vector_store = None

            return True
        except Exception as e:
            logger.error(f"清理集合失败: {str(e)}")
            return False

    def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        if self.vector_store:
            return self.vector_store.health_check()
        else:
            # 简化的健康检查
            try:
                doc_count = self.get_document_count()
                return {
                    "status": "healthy" if doc_count > 0 else "empty",
                    "document_count": doc_count,
                    "collection_name": self.collection_name,
                }
            except Exception as e:
                return {"status": "unhealthy", "error": str(e)}

    def close(self):
        """关闭连接"""
        if self.vector_store:
            self.vector_store.close()
        elif self.client:
            try:
                self.client.close()
            except Exception:
                pass


class VectorStoreManager:
    def __init__(self, vector_db_path: str, collection_name: str, embedding_model):
        self.vector_db_path = vector_db_path
        self.collection_name = collection_name
        self.embedding_model = embedding_model
        self.redis_manager = None
        self._initialize_redis()

    def _initialize_redis(self):
        """初始化Redis连接"""
        try:
            from redis import Redis
            from app.config.settings import config

            # Redis连接配置
            redis_config = {
                "host": config.redis.host,
                "port": config.redis.port,
                "db": config.redis.db,
                "password": config.redis.password,
                "socket_timeout": config.redis.socket_timeout,
                "socket_connect_timeout": config.redis.connection_timeout,
                "decode_responses": config.redis.decode_responses,
            }

            # 测试连接（仅用于验证）
            Redis(**redis_config).ping()
            # 创建Redis管理器
            redis_config["decode_responses"] = False  # 向量存储需要设为False

            # 获取嵌入维度
            embedding_dimensions = self._get_embedding_dimensions()

            self.redis_manager = RedisVectorStoreManager(
                redis_config=redis_config,
                collection_name=self.collection_name,
                embedding_dimensions=embedding_dimensions,
                local_storage_path=self.vector_db_path,
            )

        except ImportError:
            logger.error("Redis库未安装，无法使用Redis向量存储")
            self.redis_manager = None
        except Exception as e:
            logger.error(f"Redis连接失败: {str(e)}")
            self.redis_manager = None

    def _get_embedding_dimensions(self) -> int:
        """获取嵌入模型的维度"""
        try:
            # 测试嵌入模型
            if hasattr(self.embedding_model, "embed_query"):
                test_embedding = self.embedding_model.embed_query("test")
                dimensions = len(test_embedding)
                return dimensions
            return 1536  # 默认维度
        except Exception as e:
            logger.error(f"无法确定嵌入维度: {str(e)}")
            return 1536  # 默认维度

    def check_dimension_mismatch(self, current_dim: int) -> bool:
        """检查向量维度是否匹配

        Args:
            current_dim: 当前嵌入模型的维度

        Returns:
            bool: 如果维度不匹配返回True，否则返回False
        """
        try:
            if not self.redis_manager:
                return False

            # 检查Redis中是否有现有向量
            vector_count = self.redis_manager.get_document_count()
            if vector_count == 0:
                return False

            # 获取第一个向量的维度
            stored_dim = self.redis_manager.get_vector_dimension()

            if stored_dim and stored_dim != current_dim:
                logger.warning(f"向量维度不匹配: 存储={stored_dim}, 当前={current_dim}")
                return True

            return False
        except Exception as e:
            logger.error(f"检查向量维度失败: {str(e)}")
            return False

    def clear_store(self):
        """清理向量存储"""
        try:
            if not self.redis_manager:
                logger.warning("Redis管理器未初始化，无法清理")
                return False

            logger.info("开始清理向量存储...")
            result = self.redis_manager.clear_collection()
            logger.info(f"向量存储清理完成，结果: {result}")
            return True
        except Exception as e:
            logger.error(f"清理向量存储失败: {str(e)}")
            return False

    def load_existing_db(self) -> bool:
        """加载现有的向量数据库"""
        try:
            if not self.redis_manager:
                return False

            count = self.redis_manager.get_document_count()
            if count > 0:
                logger.info(f"Redis向量存储已存在，包含 {count} 个文档")
                return True
            else:
                logger.info("Redis向量存储为空或不可用")
                return False
        except Exception as e:
            logger.error(f"检查Redis向量存储失败: {str(e)}")
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
            vector_store = self.redis_manager.get_vector_store()
            vector_store.embedding_model = self.embedding_model

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
        """获取检索器"""
        if not self.redis_manager:
            logger.error("Redis管理器未初始化，无法获取检索器")
            return None

        return self.redis_manager.get_retriever()
