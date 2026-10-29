#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Redis向量存储
"""

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import hashlib
import heapq
import logging
import pickle
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
import numpy as np
import redis

from app.common.constants import ServiceConstants
from app.common.identity import cache_identity
from app.core.vector.generation_gc import GenerationConflict, GenerationGC, GenerationLeaseLost

# MD文档处理器导入
try:
    from app.core.processors.md_document_processor import (
        MDDocumentProcessor,
        MDEnhancedQueryProcessor,
    )

    MD_PROCESSOR_AVAILABLE = True
except ImportError:
    MD_PROCESSOR_AVAILABLE = False

# 层次化检索器导入
try:
    from app.core.retrieval.hierarchical_retriever import HierarchicalRetriever

    HIERARCHICAL_RETRIEVAL_AVAILABLE = True
except ImportError:
    HIERARCHICAL_RETRIEVAL_AVAILABLE = False

logger = logging.getLogger("aiops.vector_store")
VECTOR_SCAN_TIMEOUT_SECONDS = 15
VECTOR_SCAN_MAX_KEYS = 100_000
MAX_DOCUMENTS_PER_WRITE = 1000
MAX_SNAPSHOT_KEYS = 100_000
SNAPSHOT_DEADLINE_SECONDS = 30
MAX_RETAINED_GENERATIONS = 64
MAX_RETAINED_KEYS = 100_000


class RefreshPublishOutcomeUnknown(RuntimeError):
    """发布事务的结果无法确认，必须核对后再重试"""


class RefreshPublishedCacheError(RuntimeError):
    """知识已发布，但后续缓存清理失败"""


class CacheInvalidationError(RuntimeError):
    """查询缓存无法完整清理"""


class VectorSearchError(RuntimeError):
    """无法在受限时间与容量内完成完整语义检索"""



@dataclass
class SearchConfig:
    """检索配置"""

    semantic_weight: float = 0.6
    lexical_weight: float = 0.4
    similarity_threshold: float = 0.3
    use_mmr: bool = True  # 最大边际相关性
    mmr_lambda: float = 0.5
    use_cache: bool = True
    cache_ttl: int = 3600

    # MD文档特定配置
    md_structure_weight: float = 0.15  # 结构权重
    md_semantic_weight: float = 0.10  # MD语义权重
    prefer_structured_content: bool = True  # 优先结构化内容
    boost_code_blocks: bool = True  # 提升代码块权重
    boost_titles: bool = True  # 提升标题权重

    # 层次化检索配置
    use_hierarchical_retrieval: bool = True  # 使用层次化检索
    hierarchical_threshold: int = 100  # 文档数量阈值，超过此数量启用层次化检索
    auto_switch_retrieval: bool = True  # 自动切换检索策略


def _generation_scoped(method):
    """原子采样活动版本并持租约，嵌套调用沿用同一个快照"""
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        scope = getattr(self, "_generation_scope", None)
        if scope is None or scope.get() is not None:
            return await method(self, *args, **kwargs)
        if getattr(self, "_is_refresh_stage", False):
            lease = self._stage_lease
            try:
                await lease.check()
                token = scope.set((lease.generation, lease))
                try:
                    result = await method(self, *args, **kwargs)
                    await lease.check()
                    return result
                finally:
                    scope.reset(token)
            except GenerationLeaseLost as error:
                raise VectorSearchError("知识库版本租约失效，请重试") from error
        operation_returned = False
        try:
            try:
                lease = await self._generation_gc.acquire_active()
            except Exception as error:
                raise VectorSearchError("无法获取知识库版本租约") from error
            async with lease:
                token = scope.set((lease.generation, lease))
                try:
                    result = await method(self, *args, **kwargs)
                    operation_returned = True
                    await lease.check()
                    return result
                finally:
                    scope.reset(token)
        except Exception as error:
            if operation_returned and method.__name__ in {"add_documents", "add_md_documents", "delete_documents", "clear"}:
                raise RefreshPublishOutcomeUnknown("操作可能已生效，请核对知识库后重试") from error
            if isinstance(error, GenerationLeaseLost):
                raise VectorSearchError("知识库版本租约失效，请重试") from error
            raise
    return wrapped


class EnhancedRedisVectorStore:
    """Redis向量存储"""

    def __init__(
        self,
        redis_config: Dict[str, Any],
        collection_name: str,
        embedding_model: Embeddings,
        vector_dim: int = 1536,
        index_type: str = "HNSW",  # FLAT, IVF, HNSW
        max_connections: int = 20,
        connection_timeout: Optional[int] = None,
    ):
        self.redis_config = redis_config
        self.collection_name = collection_name
        self._generation_scope = ContextVar(f"knowledge_generation_{id(self)}", default=None)
        self.embedding_model = embedding_model
        self.vector_dim = vector_dim
        self.index_type = index_type
        self._closed = False

        # 从配置获取超时值
        from app.config.settings import config

        default_timeout = (
            config.redis.connection_timeout if hasattr(config, "redis") else 5
        )

        # 优化Redis连接池配置
        pool_config = {
            **redis_config,
            "max_connections": max_connections,
            "socket_timeout": connection_timeout or default_timeout,
            "socket_connect_timeout": connection_timeout or default_timeout,
            "retry_on_timeout": True,
            "health_check_interval": 30,
        }

        try:
            self.pool = redis.ConnectionPool(**pool_config)
            self.client = redis.Redis(
                connection_pool=self.pool,
                socket_keepalive=True,
                socket_keepalive_options={},
            )
            self._generation_gc = GenerationGC(self.client, collection_name)
        except Exception as e:
            logger.error("Redis连接池初始化失败，异常类型: %s", type(e).__name__)
            raise

        # 索引管理
        try:
            self.index_manager = IndexManager(self.client, collection_name)
        except Exception as e:
            logger.error("索引管理器初始化失败，异常类型: %s", type(e).__name__)
            raise

        # 查询缓存（减少内存占用）
        self.query_cache = QueryCache(
            self.client, ttl=1800, max_cache_size=1000,
            prefix=f"{collection_name}:query_cache:{ServiceConstants.KNOWLEDGE_CACHE_VERSION}:fullscan-v1:",
        )

        # 批处理队列
        self.batch_queue = BatchQueue(max_size=30)

        # 性能监控
        self.stats = {
            "search_count": 0,
            "cache_hits": 0,
            "error_count": 0,
            "last_error": None,
        }

        # MD文档处理器（懒加载）
        self.md_processor = None
        self.md_query_processor = None
        self._md_processor_initialized = False

        if MD_PROCESSOR_AVAILABLE:
            try:
                self._initialize_md_processors()
            except Exception as e:
                logger.warning("MD处理器初始化失败，异常类型: %s", type(e).__name__)

        # 层次化检索器（懒加载）
        self.hierarchical_retriever = None
        self._hierarchical_retriever_initialized = False

        if HIERARCHICAL_RETRIEVAL_AVAILABLE:
            try:
                self._initialize_hierarchical_retriever()
            except Exception as e:
                logger.warning("层次化检索器初始化失败，异常类型: %s", type(e).__name__)

        self._initialize()

    def _physical_collection(self) -> str:
        scope = getattr(self, "_generation_scope", None)
        value = scope.get() if scope else None
        return value[0] if value else self.collection_name

    def _current_lease(self):
        scope = getattr(self, "_generation_scope", None)
        value = scope.get() if scope else None
        return value[1] if value else None

    def _active_key(self) -> str:
        return f"{self.collection_name}:active_generation"

    def _decode_generation(self, value) -> str:
        if value is None:
            return self.collection_name  # 兼容旧版固定前缀
        name = value.decode("ascii") if isinstance(value, bytes) else str(value)
        if name == self.collection_name or re.fullmatch(re.escape(self.collection_name) + r"-refresh-[0-9a-f]{32}", name):
            return name
        raise VectorSearchError("知识库版本标识无效")

    @staticmethod
    def _decode_revision(value) -> str:
        if value is None:
            return "0"
        try:
            version = value.decode("ascii") if isinstance(value, bytes) else str(value)
            if not re.fullmatch(r"0|[1-9][0-9]{0,18}", version) or int(version) > 9223372036854775807:
                raise ValueError("invalid revision")
            return version
        except (UnicodeError, ValueError) as error:
            raise VectorSearchError("知识库版本无效") from error

    async def _read_active_generation(self) -> str:
        try:
            value = await asyncio.to_thread(self.client.hget, self._active_key(), "active")
            return self._decode_generation(value)
        except VectorSearchError:
            raise
        except Exception as error:
            raise VectorSearchError("无法读取知识库版本") from error

    def _initialize(self):
        """初始化向量存储"""
        try:
            # 测试Redis连接
            self.client.ping()
            logger.info(f"Redis连接成功: {self.collection_name}")

            # 创建索引
            self.index_manager.create_index(self.vector_dim, self.index_type)
            logger.info(
                f"向量索引创建成功，类型: {self.index_type}，维度: {self.vector_dim}"
            )

        except redis.ConnectionError as e:
            logger.error("Redis连接失败，异常类型: %s", type(e).__name__)
            raise
        except Exception as e:
            logger.error("初始化失败，异常类型: %s", type(e).__name__)
            self.stats["error_count"] += 1
            self.stats["last_error"] = type(e).__name__
            raise

    def _initialize_md_processors(self):
        """懒加载MD处理器"""
        if not self._md_processor_initialized and MD_PROCESSOR_AVAILABLE:
            try:
                self.md_processor = MDDocumentProcessor(
                    {
                        "max_chunk_size": 800,
                        "chunk_overlap": 100,
                        "preserve_structure": True,
                    }
                )
                self.md_query_processor = MDEnhancedQueryProcessor()
                self._md_processor_initialized = True
                logger.info("MD处理器初始化成功")
            except Exception as e:
                logger.error("MD处理器初始化失败，异常类型: %s", type(e).__name__)
                raise

    def _initialize_hierarchical_retriever(self):
        """懒加载层次化检索器"""
        if (
            not self._hierarchical_retriever_initialized
            and HIERARCHICAL_RETRIEVAL_AVAILABLE
        ):
            try:
                self.hierarchical_retriever = HierarchicalRetriever(
                    vector_store=self,
                    config={
                        "max_clusters": 50,
                        "min_cluster_size": 3,
                        "enable_quality_scoring": True,
                        "dynamic_thresholds": True,
                    },
                )
                self._hierarchical_retriever_initialized = True
                logger.info("层次化检索器初始化成功")
            except Exception as e:
                logger.error("层次化检索器初始化失败，异常类型: %s", type(e).__name__)
                raise

    async def close(self):
        """清理缓存尽力执行，但连接池总要释放"""
        if self._closed:
            return
        try:
            if hasattr(self, "query_cache"):
                await self.query_cache.clear()
        except Exception as error:
            logger.error("关闭前缓存清理失败，异常类型: %s", type(error).__name__)
        finally:
            if hasattr(self, "pool"):
                self.pool.disconnect()
            self._closed = True

    async def _invalidate_after_publish(self):
        try:
            await self.query_cache.invalidate_pattern("*")
        except Exception as error:
            raise RefreshPublishedCacheError("知识已发布，但查询缓存清理失败") from error

    async def _settle_stage_cleanup(self, stage):
        """等待清理退出；重复取消也不能让后台清理与后续操作交错"""
        task = asyncio.create_task(self.discard_refresh_stage(stage))
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancelled = error
            except Exception:
                break
        if task.cancelled():
            return cancelled, RuntimeError("暂存清理已取消")
        return cancelled, task.exception()

    async def _run_stage_operation(self, stage, operation):
        """失败时保留原异常；确认发布后清理失败则报告已发布"""
        try:
            result = await operation(stage)
            if result is not False:
                await self.publish_refresh_stage(stage)
        except BaseException:
            _cancelled, cleanup_error = await self._settle_stage_cleanup(stage)
            if cleanup_error:
                logger.error("暂存清理失败，异常类型: %s", type(cleanup_error).__name__)
            raise
        cancelled, cleanup_error = await self._settle_stage_cleanup(stage)
        if cancelled:
            if result is not False:
                raise RefreshPublishOutcomeUnknown("操作可能已生效，清理期间取消，请先核对") from cancelled
            raise cancelled
        if cleanup_error:
            if result is not False:
                raise RefreshPublishedCacheError("知识已发布，但暂存清理失败") from cleanup_error
            raise cleanup_error
        return result

    def __del__(self):
        """析构函数"""
        if not self._closed and hasattr(self, "pool"):
            try:
                self.pool.disconnect()
            except Exception:
                pass

    @asynccontextmanager
    async def get_redis_client(self):
        """获取Redis客户端的上下文管理器"""
        if self._closed:
            raise RuntimeError("向量存储已关闭")

        client = None
        try:
            client = redis.Redis(connection_pool=self.pool)
            yield client
        except Exception as e:
            logger.error("Redis操作失败，异常类型: %s", type(e).__name__)
            self.stats["error_count"] += 1
            self.stats["last_error"] = type(e).__name__
            raise
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    @_generation_scoped
    async def add_documents(self, documents: List[Document]) -> List[str]:
        """批量添加文档 - 优化版本"""
        if not documents:
            return []
        if not getattr(self, "_is_refresh_stage", False):
            stage = await self._clone_active_stage()
            doc_ids = await self._run_stage_operation(stage, lambda candidate: candidate.add_documents(documents))
            await self._invalidate_after_publish()
            return doc_ids

        # 检测和处理MD文档
        processed_documents = await self._preprocess_documents(documents)

        if len(processed_documents) > MAX_DOCUMENTS_PER_WRITE:
            raise ValueError("单次写入的文档块超过上限")

        # 批处理嵌入生成
        embeddings = await self._batch_embed(processed_documents)

        # 并行存储
        doc_ids = await self._parallel_store(processed_documents, embeddings)
        if len(doc_ids) != len(processed_documents):
            raise ValueError("Partial document storage cannot be committed to the search index")

        # 更新索引
        await self.index_manager.update_index(doc_ids, embeddings)

        # 更新层次化检索器聚类
        if self.hierarchical_retriever:
            try:
                await self.hierarchical_retriever.initialize_clusters(
                    processed_documents, embeddings
                )
                logger.debug("层次化检索聚类已更新")
            except Exception as e:
                logger.warning("更新层次化检索聚类失败，异常类型: %s", type(e).__name__)

        # 清理相关缓存
        await self.query_cache.invalidate_pattern("*")

        logger.info(f"成功添加 {len(doc_ids)} 个文档")
        return doc_ids

    @_generation_scoped
    async def add_md_documents(
        self,
        md_contents: List[str],
        metadata_list: Optional[List[Dict[str, Any]]] = None,
    ) -> List[str]:
        """专门处理MD文档的添加方法"""
        if not md_contents:
            return []

        if not self.md_processor:
            logger.warning("MD处理器未启用，使用普通文档处理")
            documents = [
                Document(page_content=content, metadata=metadata or {})
                for content, metadata in zip(
                    md_contents, metadata_list or [{}] * len(md_contents)
                )
            ]
            return await self.add_documents(documents)

        all_documents = []

        # 先准备全部文档块，再一次提交，避免版本切换造成部分上传
        # 处理每个MD文档
        for i, md_content in enumerate(md_contents):
            metadata = (
                metadata_list[i] if metadata_list and i < len(metadata_list) else {}
            )
            metadata.update({"document_type": "markdown", "is_structured": True})

            logger.info(f"处理MD文档 {i + 1}/{len(md_contents)}")

            # 解析MD文档为结构化块
            md_chunks = self.md_processor.parse_document(md_content, metadata)

            # 转换为Document对象
            documents = []
            for chunk in md_chunks:
                # 增强元数据
                enhanced_metadata = {
                    **chunk.metadata,
                    "chunk_id": chunk.chunk_id,
                    "title_hierarchy": chunk.title_hierarchy,
                    "semantic_weight": chunk.semantic_weight,
                    "structural_weight": chunk.structural_weight,
                    "element_types": chunk.metadata.get("element_types", []),
                    "has_code": chunk.metadata.get("has_code", False),
                    "has_table": chunk.metadata.get("has_table", False),
                    "languages": chunk.metadata.get("languages", []),
                }

                doc = Document(page_content=chunk.content, metadata=enhanced_metadata)
                documents.append(doc)

            all_documents.extend(documents)

        all_doc_ids = await self.add_documents(all_documents)
        logger.info(f"MD文档处理完成，生成 {len(all_doc_ids)} 个块")
        return all_doc_ids

    async def _preprocess_documents(self, documents: List[Document]) -> List[Document]:
        """预处理文档，检测MD文档并优化元数据"""
        processed_docs = []

        for doc in documents:
            processed_doc = doc

            # 检测是否是MD文档
            if self._is_markdown_content(doc.page_content):
                if not doc.metadata.get("document_type"):
                    processed_doc.metadata["document_type"] = "markdown"
                    processed_doc.metadata["is_structured"] = True

                # 如果有MD处理器，进行轻量级结构分析
                if self.md_processor:
                    # 快速提取基本结构信息
                    structure_info = self._extract_quick_structure_info(
                        doc.page_content
                    )
                    processed_doc.metadata.update(structure_info)

            processed_docs.append(processed_doc)

        return processed_docs

    def _is_markdown_content(self, content: str) -> bool:
        """检测内容是否是Markdown格式"""
        md_indicators = [
            "# ",
            "## ",
            "### ",  # 标题
            "```",  # 代码块
            "- ",
            "* ",
            "1. ",  # 列表
            "| ",  # 表格
            "> ",  # 引用
            "[",
            "](",  # 链接
        ]

        content_lower = content.lower()
        indicator_count = sum(
            1 for indicator in md_indicators if indicator in content_lower
        )

        # 如果包含2个以上MD标记，认为是MD文档
        return indicator_count >= 2

    def _extract_quick_structure_info(self, content: str) -> Dict[str, Any]:
        """快速提取结构信息"""
        info = {
            "has_code": "```" in content,
            "has_table": "|" in content and "---|" in content,
            "has_list": any(marker in content for marker in ["- ", "* ", "1. "]),
            "title_count": content.count("# ")
            + content.count("## ")
            + content.count("### "),
        }

        # 提取代码块语言
        import re

        code_blocks = re.findall(r"```(\w+)", content)
        if code_blocks:
            info["languages"] = list(set(code_blocks))

        return info

    async def _batch_embed(self, documents: List[Document]) -> List[np.ndarray]:
        """批量生成嵌入"""
        texts = [doc.page_content for doc in documents]

        # 动态调整批大小
        total_texts = len(texts)
        if total_texts <= 10:
            batch_size = total_texts  # 小批量时不分批
        elif total_texts <= 100:
            batch_size = 20
        else:
            batch_size = 30  # 大批量时使用更大的批

        tasks = []

        # 创建并发任务（限制并发数）
        max_concurrent = min(3, (total_texts + batch_size - 1) // batch_size)
        semaphore = asyncio.Semaphore(max_concurrent)

        async def embed_batch(batch_texts, batch_index):
            async with semaphore:
                try:
                    # 添加小延迟避免API限流
                    if batch_index > 0:
                        await asyncio.sleep(0.05 * batch_index)

                    batch_embeddings = await asyncio.to_thread(
                        self.embedding_model.embed_documents, batch_texts
                    )
                    return batch_index, batch_embeddings
                except Exception as e:
                    logger.error("嵌入批次失败: batch=%d, error_type=%s", batch_index, type(e).__name__)
                    return batch_index, None

        # 创建所有批次任务
        for i in range(0, total_texts, batch_size):
            batch = texts[i : i + batch_size]
            batch_index = i // batch_size
            task = asyncio.create_task(embed_batch(batch, batch_index))
            tasks.append(task)

        # 等待所有任务完成
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 按顺序重组结果
        ordered_embeddings = [None] * len(tasks)
        for result in results:
            if isinstance(result, Exception):
                logger.error("嵌入任务异常，异常类型: %s", type(result).__name__)
                continue
            batch_idx, batch_emb = result
            if batch_emb is not None:
                ordered_embeddings[batch_idx] = batch_emb

        # 展平结果
        final_embeddings = []
        for batch_emb in ordered_embeddings:
            if batch_emb is not None:
                final_embeddings.extend(batch_emb)

        logger.debug(
            f"生成了 {len(final_embeddings)} 个嵌入向量，使用 {len(tasks)} 个批次"
        )
        return [np.array(e, dtype=np.float32) for e in final_embeddings]

    async def _parallel_store(
        self, documents: List[Document], embeddings: List[np.ndarray]
    ) -> List[str]:
        """并行存储文档"""
        if len(documents) != len(embeddings):
            raise ValueError(
                f"文档数量({len(documents)})与嵌入数量({len(embeddings)})不匹配"
            )

        if len(documents) > MAX_DOCUMENTS_PER_WRITE:
            raise ValueError("单次写入的文档块超过上限")
        # 一个上传只提交一个事务，避免刷新发布夹在上传分批之间
        return await self._store_batch(documents, embeddings, 0)

    async def _store_batch(
        self, documents: List[Document], embeddings: List[np.ndarray], batch_id: int
    ) -> List[str]:
        """存储一个批次的文档"""
        doc_ids = []

        try:
            # 保证写入开始时的generation仍为活动版本
            pipe = self.client.pipeline()
            lease = self._current_lease()
            if getattr(self, "_is_refresh_stage", False) and lease is None:
                raise GenerationLeaseLost("暂存版本写入缺少有效租约")
            watch_keys = (self._active_key(),) + (lease.watch_keys if lease else ())
            await asyncio.to_thread(pipe.watch, *watch_keys)
            if lease:
                await lease.check()
            active = await asyncio.to_thread(pipe.hget, self._active_key(), "active")
            if self._decode_generation(active) != self._physical_collection():
                pipe.reset()
                raise RuntimeError("知识库版本已切换，请重新上传")
            pipe.multi()

            for doc, embedding in zip(documents, embeddings):
                doc_id = self._generate_doc_id(doc.page_content)
                doc_ids.append(doc_id)

                # 存储文档
                doc_key = f"{self._physical_collection()}:doc:{doc_id}"
                pipe.hset(
                    doc_key,
                    mapping={
                        "content": doc.page_content,
                        "metadata": pickle.dumps(doc.metadata),
                    },
                )

                # 存储向量
                vec_key = f"{self._physical_collection()}:vec:{doc_id}"
                pipe.set(vec_key, embedding.tobytes())

                # 更新倒排索引（限制词数，降低写放大）
                terms = list(self._tokenize_text(doc.page_content))[:80]  # 减少到80个词
                for term in terms:
                    term_key = f"{self._physical_collection()}:term:{term}"
                    pipe.sadd(term_key, doc_id)

            # revision与数据在同一Redis事务提交，供刷新发布检测并发写入
            pipe.incr(f"{self._physical_collection()}:revision")
            operation = asyncio.create_task(asyncio.to_thread(pipe.execute))
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                # 后台Redis写入完成前不得清理stage，否则它可能再次写入已清理集合
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not operation.cancelled():
                    operation.exception()  # 消费后台异常，仍向调用方传播取消
                raise
            logger.debug(f"批次 {batch_id} 存储完成: {len(doc_ids)} 个文档")

        except Exception as e:
            logger.error("存储批次失败: batch=%d, error_type=%s", batch_id, type(e).__name__)
            raise

        return doc_ids

    async def get_revision(self) -> str:
        """返回活动generation和revision，并拒绝混合旧新快照"""
        try:
            generation = await self._read_active_generation()
            scope = getattr(self, "_generation_scope", None)
            scoped = scope.get() if scope else None
            if scoped is not None and scoped[0] != generation:
                raise VectorSearchError("知识库在查询期间发生变化，请重试")
            value = await asyncio.to_thread(self.client.get, f"{generation}:revision")
            if await self._read_active_generation() != generation:
                raise VectorSearchError("知识库在查询期间发生变化，请重试")
            version = self._decode_revision(value)
            return f"{generation}:{version}"
        except VectorSearchError:
            raise
        except Exception as error:
            raise VectorSearchError("无法读取知识库版本") from error

    @_generation_scoped
    async def similarity_search(
        self, query: str, k: int = 5, config: Optional[SearchConfig] = None
    ) -> List[Tuple[Document, float]]:
        """优化的相似度搜索，支持智能检索策略切换"""
        if self._closed:
            raise RuntimeError("向量存储已关闭")

        config = config or SearchConfig()
        self.stats["search_count"] += 1
        start_time = time.time()

        try:
            logger.info("开始文档搜索: k=%d, threshold=%s", k, config.similarity_threshold)
            revision = await self.get_revision()
            logger.debug(f"搜索配置: {config.__dict__}")

            # 检查缓存
            if config.use_cache:
                cache_key = self._get_search_cache_key(query, k, config, revision)
                cached = await asyncio.to_thread(self.query_cache.get, cache_key)
                if cached:
                    if await self.get_revision() != revision:
                        raise VectorSearchError("知识库在查询期间发生变化，请重试")
                    self.stats["cache_hits"] += 1
                    logger.info(f"搜索缓存命中，返回 {len(cached)} 个结果")
                    return cached

            # 智能选择检索策略
            use_hierarchical = self._should_use_hierarchical_retrieval(config)

            if use_hierarchical and self.hierarchical_retriever:
                logger.info("使用层次化检索策略")
                results = await self._hierarchical_search_with_fallback(
                    query, k, config
                )
            else:
                logger.info("使用标准检索策略")
                try:
                    results = await asyncio.wait_for(self._standard_search(query, k, config), timeout=VECTOR_SCAN_TIMEOUT_SECONDS)
                except asyncio.TimeoutError as error:
                    raise VectorSearchError("知识检索超时，请重试") from error

            if await self.get_revision() != revision:
                raise VectorSearchError("知识库在查询期间发生变化，请重试")

            # 缓存结果
            if config.use_cache and results:
                await asyncio.to_thread(self.query_cache.set, cache_key, results, config.cache_ttl)

            elapsed_time = time.time() - start_time
            logger.info(
                f"搜索完成: 返回 {len(results)} 个结果，耗时 {elapsed_time:.3f}秒"
            )

            return results

        except Exception as e:
            self.stats["error_count"] += 1
            self.stats["last_error"] = type(e).__name__
            logger.error("搜索失败，异常类型: %s", type(e).__name__)
            raise

    def _should_use_hierarchical_retrieval(self, config: SearchConfig) -> bool:
        """判断是否应该使用层次化检索"""
        # 层次索引是进程内状态，不保证覆盖Redis中的完整持久语料
        # 等共享版本索引就绪后再恢复自动切换
        return False

    async def _hierarchical_search_with_fallback(
        self, query: str, k: int, config: SearchConfig
    ) -> List[Tuple[Document, float]]:
        """层次化搜索，带回退机制"""
        try:
            # 增强查询分析
            context = {}
            if self.md_query_processor:
                enhanced_query_info = self.md_query_processor.enhance_query_for_md(
                    query, context
                )
                context.update(enhanced_query_info)

            # 执行层次化搜索
            results = await self.hierarchical_retriever.hierarchical_search(
                query, k, context
            )

            if results:
                logger.debug(f"层次化检索成功，返回 {len(results)} 个结果")
                return results
            else:
                logger.warning("层次化检索无结果，切换到标准检索")
                return await self._standard_search(query, k, config)

        except redis.RedisError as e:
            logger.error("层次化检索Redis错误，切换到标准检索，异常类型: %s", type(e).__name__)
            self.stats["error_count"] += 1
            return await self._standard_search(query, k, config)
        except Exception as e:
            logger.warning("层次化检索失败，切换到标准检索，异常类型: %s", type(e).__name__)
            self.stats["error_count"] += 1
            return await self._standard_search(query, k, config)

    async def _standard_search(
        self, query: str, k: int, config: SearchConfig
    ) -> List[Tuple[Document, float]]:
        """标准搜索"""
        # 生成查询向量
        query_embedding = await asyncio.to_thread(
            self.embedding_model.embed_query, query
        )
        query_vector = np.array(query_embedding, dtype=np.float32)

        # 执行混合搜索
        results = await self._hybrid_search(
            query,
            query_vector,
            k * 3,
            config,  # 获取更多候选
        )

        # MMR去重和重排序
        if config.use_mmr and len(results) > k:
            results = self._mmr_rerank(results, query_vector, k, config.mmr_lambda)
        else:
            results = results[:k]

        return results

    async def _hybrid_search(
        self, query: str, query_vector: np.ndarray, k: int, config: SearchConfig
    ) -> List[Tuple[Document, float]]:
        """混合搜索"""
        logger.debug(
            f"开始混合搜索: k={k}, semantic_weight={config.semantic_weight}, lexical_weight={config.lexical_weight}"
        )

        # 并行执行语义和词汇搜索
        semantic_task = asyncio.create_task(self._semantic_search(query_vector, k))
        lexical_task = asyncio.create_task(self._lexical_search(query, k))

        semantic_results, lexical_results = await asyncio.gather(
            semantic_task, lexical_task
        )

        logger.debug(
            f"搜索阶段结果: 语义搜索={len(semantic_results)}, 词汇搜索={len(lexical_results)}"
        )

        # 融合结果
        fused_results = self._fuse_results(
            semantic_results,
            lexical_results,
            config.semantic_weight,
            config.lexical_weight,
            config.similarity_threshold,
        )

        logger.debug(f"混合搜索完成: 融合后结果数={len(fused_results)}")
        return fused_results

    async def _semantic_search(
        self, query_vector: np.ndarray, k: int
    ) -> List[Tuple[Document, float]]:
        """语义搜索 - 优先使用 fallback 搜索"""
        # Redis保存完整向量集；本地索引可能在重启或其他实例写入后不完整
        return await self._fallback_semantic_search(query_vector, k)

    async def _lexical_search(self, query: str, k: int) -> List[Tuple[Document, float]]:
        """词汇搜索 - 基于倒排索引"""
        # 分词（与索引一致）
        query_terms = set(self._tokenize_text(query))

        # 搜索倒排索引
        doc_scores = defaultdict(float)

        for term in query_terms:
            term_key = f"{self._physical_collection()}:term:{term}"
            doc_ids = await asyncio.to_thread(self.client.smembers, term_key)

            for doc_id in doc_ids:
                if isinstance(doc_id, bytes):
                    doc_id = doc_id.decode()
                doc_scores[doc_id] += 1.0 / len(query_terms)

        # 排序并批量获取文档
        top_items = sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)[:k]
        if not top_items:
            return []

        doc_ids = [doc_id for doc_id, _ in top_items]
        docs_map = await self._get_documents_bulk(doc_ids)

        results = []
        for doc_id, score in top_items:
            doc = docs_map.get(doc_id)
            if doc:
                results.append((doc, score))
        return results

    def _fuse_results(
        self,
        semantic_results: List[Tuple[Document, float]],
        lexical_results: List[Tuple[Document, float]],
        semantic_weight: float,
        lexical_weight: float,
        threshold: float,
    ) -> List[Tuple[Document, float]]:
        """融合搜索结果"""
        # 创建文档到分数的映射
        doc_scores = defaultdict(float)
        doc_map = {}

        # 处理语义结果
        for doc, score in semantic_results:
            doc_id = self._generate_doc_id(doc.page_content)
            doc_scores[doc_id] += score * semantic_weight
            doc_map[doc_id] = doc

        # 处理词汇结果
        for doc, score in lexical_results:
            doc_id = self._generate_doc_id(doc.page_content)
            doc_scores[doc_id] += score * lexical_weight
            doc_map[doc_id] = doc

        # 排序和过滤
        results = []
        # 对于低分结果，使用更宽松的阈值避免过度过滤
        adaptive_threshold = max(threshold * 0.5, 0.1)  # 自适应阈值，最低0.1
        for doc_id, score in doc_scores.items():
            if score >= adaptive_threshold:
                results.append((doc_map[doc_id], score))

        results.sort(key=lambda x: x[1], reverse=True)
        logger.debug(
            f"融合搜索结果: 原始阈值={threshold:.3f}, 自适应阈值={adaptive_threshold:.3f}, 结果数={len(results)}"
        )
        return results

    def _mmr_rerank(
        self,
        results: List[Tuple[Document, float]],
        query_vector: np.ndarray,
        k: int,
        lambda_param: float,
    ) -> List[Tuple[Document, float]]:
        """最大边际相关性重排序"""
        if not results:
            return []

        # 批量提取文档向量（减少往返）
        doc_ids = [self._generate_doc_id(doc.page_content) for doc, _ in results]
        vec_keys = [f"{self._physical_collection()}:vec:{doc_id}" for doc_id in doc_ids]
        pipe = self.client.pipeline()
        for key in vec_keys:
            pipe.get(key)
        vec_bytes_list = pipe.execute()

        doc_vectors = []
        for vec_bytes in vec_bytes_list:
            if vec_bytes:
                doc_vectors.append(np.frombuffer(vec_bytes, dtype=np.float32))
            else:
                doc_vectors.append(np.zeros(self.vector_dim, dtype=np.float32))

        # MMR选择
        selected = []
        selected_indices = set()

        while len(selected) < k and len(selected_indices) < len(results):
            best_score = -1
            best_idx = -1

            for i, (doc, orig_score) in enumerate(results):
                if i in selected_indices:
                    continue

                # 计算与查询的相似度
                query_sim = self._cosine_similarity(query_vector, doc_vectors[i])

                # 计算与已选文档的最大相似度
                max_selected_sim = 0
                for j in selected_indices:
                    sim = self._cosine_similarity(doc_vectors[i], doc_vectors[j])
                    max_selected_sim = max(max_selected_sim, sim)

                # MMR分数
                mmr_score = (
                    lambda_param * query_sim - (1 - lambda_param) * max_selected_sim
                )

                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = i

            if best_idx >= 0:
                selected.append(results[best_idx])
                selected_indices.add(best_idx)

        return selected

    async def _get_document(self, doc_id: str) -> Optional[Document]:
        """获取文档"""
        try:
            doc_key = f"{self._physical_collection()}:doc:{doc_id}"
            doc_data = await asyncio.to_thread(self.client.hgetall, doc_key)

            if not doc_data:
                return None

            content = doc_data.get(b"content", b"").decode()
            metadata = pickle.loads(doc_data.get(b"metadata", pickle.dumps({})))

            return Document(page_content=content, metadata=metadata)

        except redis.RedisError as e:
            logger.error("获取文档Redis错误，异常类型: %s", type(e).__name__)
            self.stats["error_count"] += 1
            return None
        except Exception as e:
            logger.error("获取文档失败，异常类型: %s", type(e).__name__)
            self.stats["error_count"] += 1
            return None

    async def _get_documents_bulk(self, doc_ids: List[str]) -> Dict[str, Document]:
        """批量获取文档，减少往返"""
        if not doc_ids:
            return {}

        try:
            pipe = self.client.pipeline()
            for doc_id in doc_ids:
                doc_key = f"{self._physical_collection()}:doc:{doc_id}"
                pipe.hgetall(doc_key)
            results = await asyncio.to_thread(pipe.execute)

            docs: Dict[str, Document] = {}
            for doc_id, data in zip(doc_ids, results):
                if not data:
                    continue
                content = data.get(b"content", b"").decode()
                metadata = pickle.loads(data.get(b"metadata", pickle.dumps({})))
                docs[doc_id] = Document(page_content=content, metadata=metadata)
            return docs
        except redis.RedisError as e:
            logger.error("批量获取文档Redis错误，异常类型: %s", type(e).__name__)
            self.stats["error_count"] += 1
            return {}
        except Exception as e:
            logger.error("批量获取文档失败，异常类型: %s", type(e).__name__)
            self.stats["error_count"] += 1
            return {}

    def _generate_doc_id(self, content: str) -> str:
        """生成文档ID"""
        return hashlib.md5(content.encode()).hexdigest()

    def _cosine_similarity(self, vec1: np.ndarray, vec2: np.ndarray) -> float:
        """计算余弦相似度"""
        dot = np.dot(vec1, vec2)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(dot / (norm1 * norm2))

    async def _fallback_semantic_search(
        self, query_vector: np.ndarray, k: int
    ) -> List[Tuple[Document, float]]:
        """在限时和容量约束内扫描持久向量，失败时不返回部分结果"""
        if k <= 0 or k > 1000 or query_vector.ndim != 1 or not np.all(np.isfinite(query_vector)):
            raise VectorSearchError("语义查询参数无效")
        try:
            return await asyncio.wait_for(self._scan_semantic_vectors(query_vector, k), timeout=VECTOR_SCAN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as error:
            raise VectorSearchError("语义查询超时，未返回部分结果") from error
        except VectorSearchError:
            raise
        except Exception as error:
            self.stats["error_count"] += 1
            raise VectorSearchError("语义查询失败，未返回部分结果") from error

    async def _scan_semantic_vectors(
        self, query_vector: np.ndarray, k: int
    ) -> List[Tuple[Document, float]]:
        pattern = f"{self._physical_collection()}:vec:*"
        revision = await self.get_revision()
        cursor = 0
        top_k: List[Tuple[float, str]] = []
        seen = set()

        def score_batch(keys, values):
            scored = []
            for key, raw in zip(keys, values):
                if raw is None:
                    continue  # 扫描期间被删除
                if len(raw) != query_vector.size * np.dtype(np.float32).itemsize:
                    raise VectorSearchError("持久向量维度不匹配")
                vector = np.frombuffer(raw, dtype=np.float32)
                if not np.all(np.isfinite(vector)):
                    raise VectorSearchError("持久向量包含非有限值")
                similarity = self._cosine_similarity_fallback(query_vector, vector)
                if similarity > 0.05:
                    key_text = key.decode() if isinstance(key, bytes) else str(key)
                    scored.append((similarity, key_text.rsplit(":", 1)[-1]))
            return scored

        while True:
            cursor, keys = await asyncio.to_thread(
                self.client.scan, cursor, match=pattern, count=500
            )
            # Redis SCAN可能重复返回同一个键
            unique_keys = []
            for key in keys:
                if key not in seen:
                    seen.add(key)
                    unique_keys.append(key)
            keys = unique_keys
            if len(seen) > VECTOR_SCAN_MAX_KEYS:
                raise VectorSearchError("知识库超过完整扫描容量上限")
            if keys:
                pipe = self.client.pipeline()
                for key in keys:
                    pipe.get(key)
                values = await asyncio.to_thread(pipe.execute)
                if len(values) != len(keys):
                    raise VectorSearchError("持久向量读取不完整")
                scored = await asyncio.to_thread(score_batch, keys, values)
                for item in scored:
                    if len(top_k) < k:
                        heapq.heappush(top_k, item)
                    elif item > top_k[0]:
                        heapq.heapreplace(top_k, item)
            if cursor == 0:
                break

        selected = sorted(top_k, reverse=True)
        documents = await self._get_documents_bulk([doc_id for _, doc_id in selected])
        if await self.get_revision() != revision:
            raise VectorSearchError("知识库在查询期间发生变化，请重试")
        if len(documents) != len(selected):
            raise VectorSearchError("文档读取不完整，未返回部分结果")
        return [(documents[doc_id], float(score)) for score, doc_id in selected]

    def _cosine_similarity_fallback(self, vec1: np.ndarray, vec2: np.ndarray) -> float:
        """计算余弦相似度"""
        try:
            # 归一化向量
            norm1 = np.linalg.norm(vec1)
            norm2 = np.linalg.norm(vec2)

            if norm1 == 0 or norm2 == 0:
                return 0.0

            # 计算余弦相似度
            similarity = np.dot(vec1, vec2) / (norm1 * norm2)
            return float(similarity)
        except Exception:
            return 0.0

    def _get_search_cache_key(self, query: str, k: int, config: SearchConfig, revision: str = "0") -> str:
        """按知识库版本隔离搜索缓存"""
        params = f"{revision}:{cache_identity()}:{query}:{k}:{config.semantic_weight}:{config.lexical_weight}"
        return hashlib.md5(params.encode()).hexdigest()

    def _tokenize_text(self, text: str) -> List[str]:
        """简单分词：小写、去掉非字母数字下划线、按空白切分"""
        if not text:
            return []
        lowered = text.lower()
        cleaned = re.sub(r"[^\w\s]", " ", lowered)
        return [t for t in cleaned.split() if t]

    async def _cleanup_partial_data(self, doc_ids: List[str]):
        """统一经版本检查和原子事务清理，避免绕过revision"""
        if doc_ids and not await self.delete_documents(doc_ids):
            raise RuntimeError("文档清理失败")

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = self.stats.copy()

        try:
            # 添加Redis状态
            redis_info = self.client.info()
            stats.update(
                {
                    "redis_connected_clients": redis_info.get("connected_clients", 0),
                    "redis_used_memory": redis_info.get("used_memory_human", "0B"),
                    "redis_used_memory_peak": redis_info.get(
                        "used_memory_peak_human", "0B"
                    ),
                    "redis_uptime": redis_info.get("uptime_in_seconds", 0),
                    "redis_total_commands_processed": redis_info.get(
                        "total_commands_processed", 0
                    ),
                    "redis_keyspace_hits": redis_info.get("keyspace_hits", 0),
                    "redis_keyspace_misses": redis_info.get("keyspace_misses", 0),
                }
            )

            # 计算缓存命中率
            cache_stats = self.query_cache.get_stats()
            total_requests = cache_stats["hits"] + cache_stats["misses"]
            if total_requests > 0:
                stats["cache_hit_rate"] = cache_stats["hits"] / total_requests
            else:
                stats["cache_hit_rate"] = 0.0
            stats.update(cache_stats)

            # 添加索引状态
            if self.index_manager and hasattr(self.index_manager, "faiss_index"):
                faiss_index = self.index_manager.faiss_index
                if faiss_index:
                    stats["index_total_vectors"] = faiss_index.ntotal
                    stats["index_scope"] = "local-ephemeral"
                    stats["index_type"] = self.index_type

            # 添加错误率统计
            if self.stats["search_count"] > 0:
                stats["error_rate"] = (
                    self.stats["error_count"] / self.stats["search_count"]
                )
            else:
                stats["error_rate"] = 0.0

            # 添加集合信息
            stats["collection_name"] = self.collection_name
            stats["vector_dimension"] = self.vector_dim

            # 添加时间戳
            stats["timestamp"] = time.time()

        except Exception as e:
            logger.warning("获取统计信息失败，异常类型: %s", type(e).__name__)

        return stats

    def log_performance_metrics(
        self, operation: str, duration: float, details: Dict[str, Any] = None
    ):
        """记录性能指标"""
        details = details or {}

        # 性能日志
        if duration > 1.0:  # 超过1秒的操作记录为警告
            logger.warning("性能警告，耗时: %.3f秒", duration)
        elif duration > 0.5:  # 超过500ms记录为信息
            logger.info("性能监控，耗时: %.3f秒", duration)
        else:
            logger.debug("性能监控，耗时: %.3f秒", duration)

    def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        health = {
            "status": "unknown",
            "redis_connection": False,
            "index_available": False,
            "cache_available": False,
            "error_count": self.stats["error_count"],
            "last_error": self.stats.get("last_error"),
            "timestamp": time.time(),
        }

        try:
            # 检查Redis连接
            self.client.ping()
            health["redis_connection"] = True

            # 检查索引
            if self.index_manager:
                health["index_available"] = True

            # 检查缓存
            if self.query_cache:
                health["cache_available"] = True

            # 根据错误率判断整体状态
            total_searches = self.stats["search_count"]
            if total_searches == 0:
                health["status"] = "healthy"
            else:
                error_rate = self.stats["error_count"] / total_searches
                if error_rate < 0.01:  # 错误率小于1%
                    health["status"] = "healthy"
                elif error_rate < 0.05:  # 错误率小于5%
                    health["status"] = "degraded"
                else:
                    health["status"] = "unhealthy"

        except Exception as e:
            health["status"] = "unhealthy"
            health["last_check_error"] = type(e).__name__
            logger.error("健康检查失败，异常类型: %s", type(e).__name__)

        return health

    @_generation_scoped
    async def batch_similarity_search(
        self, queries: List[str], k: int = 5, config: Optional[SearchConfig] = None
    ) -> List[List[Tuple[Document, float]]]:
        """批量相似度搜索（性能优化版）"""
        if not queries:
            return []

        config = config or SearchConfig()
        start_time = time.time()

        try:
            revision = await self.get_revision()
            # 预生成所有查询的embedding
            batch_embeddings = await self._batch_embed_queries(queries)

            # 并发执行搜索
            search_tasks = []
            for i, (query, embedding) in enumerate(zip(queries, batch_embeddings)):
                if embedding is not None:
                    task = asyncio.create_task(
                        self._single_search_with_embedding(query, embedding, k, config)
                    )
                    search_tasks.append(task)
                else:
                    search_tasks.append(
                        asyncio.create_task(asyncio.sleep(0, result=[]))
                    )

            # 等待所有搜索完成
            results = await asyncio.gather(*search_tasks, return_exceptions=True)

            # 处理结果
            final_results = []
            for result in results:
                if isinstance(result, VectorSearchError):
                    raise result
                if isinstance(result, Exception):
                    logger.error("批量搜索中的单个查询失败，异常类型: %s", type(result).__name__)
                    final_results.append([])
                else:
                    final_results.append(result)

            batch_time = time.time() - start_time
            logger.info(
                f"批量搜索完成: {len(queries)} 个查询，耗时: {batch_time:.3f}秒"
            )

            if await self.get_revision() != revision:
                raise VectorSearchError("知识库在查询期间发生变化，请重试")
            return final_results

        except VectorSearchError:
            raise
        except Exception as e:
            logger.error("批量搜索失败，异常类型: %s", type(e).__name__)
            # 返回空结果而不是抛出异常
            return [[] for _ in queries]

    async def _batch_embed_queries(
        self, queries: List[str]
    ) -> List[Optional[np.ndarray]]:
        """批量生成查询embedding"""
        try:
            # 使用更大的批处理以提高效率
            batch_size = min(20, len(queries))
            embeddings = []

            for i in range(0, len(queries), batch_size):
                batch = queries[i : i + batch_size]
                try:
                    batch_embeddings = await asyncio.to_thread(
                        self.embedding_model.embed_documents, batch
                    )
                    embeddings.extend(
                        [np.array(e, dtype=np.float32) for e in batch_embeddings]
                    )
                except Exception as e:
                    logger.error("批次embedding生成失败，异常类型: %s", type(e).__name__)
                    # 为失败的批次填充None
                    embeddings.extend([None] * len(batch))

            return embeddings

        except Exception as e:
            logger.error("批量embedding生成失败，异常类型: %s", type(e).__name__)
            return [None] * len(queries)

    async def _single_search_with_embedding(
        self, query: str, query_embedding: np.ndarray, k: int, config: SearchConfig
    ) -> List[Tuple[Document, float]]:
        """批量查询使用与普通查询相同的generation与revision缓存合同"""
        try:
            revision = await self.get_revision()
            cache_key = self._get_search_cache_key(query, k, config, revision)
            if config.use_cache:
                cached = await asyncio.to_thread(self.query_cache.get, cache_key)
                if cached:
                    if await self.get_revision() != revision:
                        raise VectorSearchError("知识库在查询期间发生变化，请重试")
                    return cached
            try:
                results = await asyncio.wait_for(
                    self._standard_search_with_embedding(query, query_embedding, k, config),
                    timeout=VECTOR_SCAN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as error:
                raise VectorSearchError("知识检索超时，请重试") from error
            if await self.get_revision() != revision:
                raise VectorSearchError("知识库在查询期间发生变化，请重试")
            if config.use_cache and results:
                await asyncio.to_thread(self.query_cache.set, cache_key, results, config.cache_ttl)
            return results
        except VectorSearchError:
            raise
        except Exception as error:
            logger.error("单个搜索失败，异常类型: %s", type(error).__name__)
            return []

    async def _standard_search_with_embedding(
        self, query: str, query_embedding: np.ndarray, k: int, config: SearchConfig
    ) -> List[Tuple[Document, float]]:
        """使用预生成embedding的标准搜索"""
        # 执行混合搜索
        results = await self._hybrid_search_with_embedding(
            query, query_embedding, k * 3, config
        )

        # MMR去重和重排序
        if config.use_mmr and len(results) > k:
            results = self._mmr_rerank(results, query_embedding, k, config.mmr_lambda)
        else:
            results = results[:k]

        return results

    async def _hybrid_search_with_embedding(
        self, query: str, query_embedding: np.ndarray, k: int, config: SearchConfig
    ) -> List[Tuple[Document, float]]:
        """使用预生成embedding的混合搜索"""
        # 并行执行语义和词汇搜索
        semantic_task = asyncio.create_task(
            self._semantic_search_with_embedding(query_embedding, k)
        )
        lexical_task = asyncio.create_task(self._lexical_search(query, k))

        semantic_results, lexical_results = await asyncio.gather(
            semantic_task, lexical_task
        )

        # 融合结果
        return self._fuse_results(
            semantic_results,
            lexical_results,
            config.semantic_weight,
            config.lexical_weight,
            config.similarity_threshold,
        )

    async def _semantic_search_with_embedding(
        self, query_embedding: np.ndarray, k: int
    ) -> List[Tuple[Document, float]]:
        """使用预生成embedding的语义搜索"""
        # 与普通语义搜索使用相同完整数据源，避免仅返回本进程新增的文档
        return await self._fallback_semantic_search(query_embedding, k)

    def _collection_keys(self, collection: str, *, deadline: Optional[float] = None, limit: int = VECTOR_SCAN_MAX_KEYS * 5) -> List[bytes]:
        keys = []
        seen = set()
        for kind in ("doc", "vec", "term", "idx"):
            cursor = 0
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("知识库快照构建超时")
                cursor, batch = self.client.scan(cursor, match=f"{collection}:{kind}:*", count=500)
                for key in batch:
                    if key in seen:
                        continue
                    seen.add(key)
                    keys.append(key)
                    if len(keys) > limit:
                        raise RuntimeError("知识库键数量超过安全操作上限")
                if cursor == 0:
                    break
        return keys

    def _atomic_delete(self, doc_ids: Optional[List[str]]) -> int:
        generation = self._physical_collection()
        revision = f"{generation}:revision"
        with self.client.pipeline() as pipe:
            lease = self._current_lease()
            if getattr(self, "_is_refresh_stage", False) and lease is None:
                raise GenerationLeaseLost("暂存版本写入缺少有效租约")
            watch_keys = (self._active_key(), revision) + (lease.watch_keys if lease else ())
            pipe.watch(*watch_keys)
            if lease:
                now = self.client.time()
                now_ms = now[0] * 1000 + now[1] // 1000
                for protected in lease.protected:
                    score = pipe.zscore(lease.owner.lease_key(protected), lease.token)
                    if score is None or score <= now_ms or pipe.exists(lease.owner.tombstone_key(protected)):
                        raise GenerationLeaseLost("知识库版本租约失效")
            if self._decode_generation(pipe.hget(self._active_key(), "active")) != generation:
                raise RuntimeError("知识库版本已切换，请重试")
            if doc_ids is None:
                keys = self._collection_keys(generation)
            else:
                keys = list(self.client.scan_iter(match=f"{self._physical_collection()}:term:*", count=500))
                if len(keys) > VECTOR_SCAN_MAX_KEYS * 5:
                    raise RuntimeError("词条数量超过安全操作上限")
            pipe.multi()
            if doc_ids is None:
                if keys:
                    pipe.delete(*keys)
            else:
                for doc_id in doc_ids:
                    pipe.delete(f"{self._physical_collection()}:doc:{doc_id}", f"{self._physical_collection()}:vec:{doc_id}")
                for key in keys:
                    pipe.srem(key, *doc_ids)
            pipe.incr(revision)
            pipe.execute()
            counts = {"documents": 0, "vectors": 0, "terms": 0, "indices": 0}
            for key in keys:
                text_key = key.decode() if isinstance(key, bytes) else str(key)
                suffix = text_key[len(generation) + 1:]
                for prefix, category in (("doc:", "documents"), ("vec:", "vectors"), ("term:", "terms"), ("idx:", "indices")):
                    if suffix.startswith(prefix):
                        counts[category] += 1
                        break
            return counts

    @_generation_scoped
    async def delete_documents(self, doc_ids: List[str]) -> bool:
        """原子删除文档、向量与倒排关系，并使并发刷新检测到变化"""
        if not doc_ids:
            return True
        if not getattr(self, "_is_refresh_stage", False):
            stage = await self._clone_active_stage()
            deleted = await self._run_stage_operation(stage, lambda candidate: candidate.delete_documents(doc_ids))
            if not deleted:
                return False
            await self._invalidate_after_publish()
            return True
        try:
            await asyncio.to_thread(self._atomic_delete, doc_ids)
            await self.query_cache.invalidate_pattern("*")
            return True
        except Exception as error:
            logger.error("删除文档失败，异常类型: %s", type(error).__name__)
            return False

    def _check_retained_capacity(self):
        """拒绝无界累积；不自动删除任何可能仍被读取的旧版本"""
        deadline = time.monotonic() + SNAPSHOT_DEADLINE_SECONDS
        prefixes = set()
        scanned = 0
        cursor = 0
        pattern = re.compile(re.escape(self.collection_name) + r"-refresh-[0-9a-f]{32}(?=:)")
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("知识库版本容量检查超时")
            cursor, keys = self.client.scan(cursor, match=f"{self.collection_name}-refresh-*", count=500)
            for key in keys:
                scanned += 1
                if scanned > MAX_RETAINED_KEYS:
                    raise RuntimeError("保留的知识库版本超过键数量上限，需要安全清理")
                text_key = key.decode("utf-8") if isinstance(key, bytes) else str(key)
                match = pattern.match(text_key)
                if match:
                    prefixes.add(match.group())
                    if len(prefixes) >= MAX_RETAINED_GENERATIONS:
                        raise RuntimeError("保留的知识库版本超过数量上限，需要安全清理")
            if cursor == 0:
                break

    async def create_refresh_stage(self):
        """创建独立临时集合，并记录待替换集合的原始revision"""
        for candidate in await self._generation_gc.list_candidates():
            await self._generation_gc.collect(candidate)
        await asyncio.to_thread(self._check_retained_capacity)
        name = f"{self.collection_name}-refresh-{uuid.uuid4().hex}"
        stage_lease = await self._generation_gc.acquire_stage(name)
        generation = stage_lease.source_generation
        revision = stage_lease.source_revision
        try:
            self._decode_revision(revision)
        except Exception:
            await self._generation_gc.abort_stage(stage_lease, known_not_committed=True)
            await stage_lease.close()
            await self._generation_gc.collect(name)
            raise
        operation = asyncio.create_task(asyncio.to_thread(
            type(self), self.redis_config, name, self.embedding_model,
            self.vector_dim, self.index_type,
        ))
        try:
            stage = await asyncio.shield(operation)
        except asyncio.CancelledError:
            # 构造线程可能仍创建连接池；等待结果并关闭后才能退出
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                orphan = operation.result()
            except Exception:
                raise asyncio.CancelledError from None
            cleanup = asyncio.create_task(orphan.close())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not cleanup.cancelled():
                cleanup.exception()
            await self._generation_gc.abort_stage(stage_lease, known_not_committed=True)
            await stage_lease.close()
            await self._generation_gc.collect(name)
            raise
        except Exception:
            await self._generation_gc.abort_stage(stage_lease, known_not_committed=True)
            await stage_lease.close()
            await self._generation_gc.collect(name)
            raise
        stage._stage_lease = stage_lease
        stage._is_refresh_stage = True
        stage._refresh_origin = self.collection_name
        stage._refresh_revision = revision
        stage._refresh_generation = generation
        stage._commit_started = False
        return stage

    def _copy_active_snapshot(self, stage):
        """在不可见stage复制稳定快照；任何异常仅影响stage"""
        deadline = time.monotonic() + SNAPSHOT_DEADLINE_SECONDS
        active_key = self._active_key()
        source = stage._refresh_generation
        revision = f"{source}:revision"
        with self.client.pipeline() as guard:
            # 心跳会更新租约ZSET；长扫描只监视稳定删除标记，发布时再校验租约
            guard.watch(active_key, revision, *stage._stage_lease.tombstone_keys)
            if (self._decode_generation(guard.hget(active_key, "active")) != source or
                    guard.get(revision) != stage._refresh_revision):
                raise redis.WatchError("知识库已发生并发写入")
            keys = self._collection_keys(source, deadline=deadline, limit=MAX_SNAPSHOT_KEYS)
            prefix = f"{source}:".encode()
            destination = f"{stage.collection_name}:".encode()
            for offset in range(0, len(keys), 100):
                if time.monotonic() >= deadline:
                    raise TimeoutError("知识库快照构建超时")
                batch = keys[offset:offset + 100]
                # 每批COPY也监视回收标记与租约，防止回收后后台线程重新生成stage键
                with self.client.pipeline() as pipe:
                    pipe.watch(*stage._stage_lease.watch_keys)
                    now = self.client.time()
                    now_ms = now[0] * 1000 + now[1] // 1000
                    for generation in stage._stage_lease.protected:
                        score = pipe.zscore(self._generation_gc.lease_key(generation), stage._stage_lease.token)
                        if score is None or score <= now_ms or pipe.exists(self._generation_gc.tombstone_key(generation)):
                            raise GenerationLeaseLost("知识库版本租约失效")
                    pipe.multi()
                    for key in batch:
                        key = key if isinstance(key, bytes) else key.encode()
                        if not key.startswith(prefix):
                            raise RuntimeError("知识库快照键身份不匹配")
                        suffix = key[len(prefix):]
                        if suffix.startswith(b"idx:"):
                            continue
                        pipe.execute_command("COPY", key, destination + suffix)
                    if not all(pipe.execute()):
                        raise RuntimeError("知识库快照读取不完整")
            if time.monotonic() >= deadline:
                raise TimeoutError("知识库快照构建超时")
            # EXEC验证WATCH，确保复制期间没有活动版本切换或写入
            guard.multi()
            guard.ping()
            guard.execute()

    async def _clone_active_stage(self):
        stage = await self.create_refresh_stage()
        try:
            if stage._refresh_generation != self._physical_collection():
                raise RuntimeError("知识库版本已切换，请重试")
            operation = asyncio.create_task(asyncio.to_thread(self._copy_active_snapshot, stage))
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not operation.cancelled():
                    operation.exception()
                raise
            return stage
        except BaseException:
            await self.discard_refresh_stage(stage)
            raise

    async def publish_refresh_stage(self, stage):
        """标记未知后由单个Lua命令校验租约、版本并切换指针"""
        if (getattr(stage, "_refresh_origin", None) != self.collection_name or
                not re.fullmatch(re.escape(self.collection_name) + r"-refresh-[0-9a-f]{32}", stage.collection_name)):
            raise ValueError("临时知识库不属于当前集合")
        lease = stage._stage_lease
        await lease.check()
        await self._generation_gc.protect_unknown(lease)
        stage._commit_started = True
        operation = asyncio.create_task(self._generation_gc.publish_cas(lease))
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError as error:
            # 结果未知前等后台操作结束，不得与stage清理并发
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                operation.result()
            except (GenerationConflict, GenerationLeaseLost):
                stage._commit_started = False
                raise RuntimeError("知识库版本已变化或租约失效，请重新刷新") from error
            except Exception as inner:
                raise RefreshPublishOutcomeUnknown("刷新提交结果未知，请先核对知识库") from inner
            raise RefreshPublishOutcomeUnknown("刷新提交期间取消，请先核对知识库") from error
        except (GenerationConflict, GenerationLeaseLost) as error:
            stage._commit_started = False
            raise RuntimeError("知识库版本已变化或租约失效，请重新刷新") from error
        except Exception as error:
            raise RefreshPublishOutcomeUnknown("刷新提交结果未知，请先核对知识库") from error
        try:
            await self._generation_gc.confirm_published(lease)
        except Exception as error:
            raise RefreshPublishOutcomeUnknown("刷新已提交但确认失败，请先核对知识库") from error

    async def discard_refresh_stage(self, stage):
        """仅回收明确未提交的stage；未知或已发布版本保留"""
        if (getattr(stage, "_refresh_origin", None) != self.collection_name or
                not re.fullmatch(re.escape(self.collection_name) + r"-refresh-[0-9a-f]{32}", stage.collection_name)):
            raise ValueError("临时知识库不属于当前集合")
        if getattr(stage, "_discarded", False):
            return
        lease = stage._stage_lease
        try:
            if not getattr(stage, "_commit_started", False):
                await self._generation_gc.abort_stage(lease, known_not_committed=True)
        finally:
            try:
                await lease.close()
            finally:
                await stage.close()
                stage._discarded = True
        if not getattr(stage, "_commit_started", False):
            await self._generation_gc.collect(stage.collection_name)

    @_generation_scoped
    async def get_document_count(self) -> int:
        """获取文档总数"""
        try:
            revision = await self.get_revision()
            async with self.get_redis_client() as client:
                pattern = f"{self._physical_collection()}:doc:*"
                cursor = 0
                keys_seen = set()
                while True:
                    cursor, keys = await asyncio.to_thread(client.scan, cursor, match=pattern, count=1000)
                    keys_seen.update(keys)
                    if len(keys_seen) > VECTOR_SCAN_MAX_KEYS:
                        raise VectorSearchError("知识库超过统计容量上限")
                    if cursor == 0:
                        break
                if await self.get_revision() != revision:
                    raise VectorSearchError("知识库在统计期间发生变化，请重试")
                return len(keys_seen)
        except VectorSearchError:
            raise
        except Exception as error:
            raise VectorSearchError("无法读取知识库文档数量") from error

    async def optimize_storage(self):
        """禁用未纳入revision事务的旧索引整理，避免覆盖并发刷新结果"""
        raise RuntimeError("索引整理暂不可用：需要支持revision事务")

    async def _cleanup_empty_term_sets(self):
        raise RuntimeError("索引整理暂不可用：需要支持revision事务")

    async def _compress_sparse_term_sets(self):
        raise RuntimeError("索引整理暂不可用：需要支持revision事务")

    @_generation_scoped
    async def clear(self):
        """通过发布空版本清空活动知识库，旧版本保留供恢复"""
        if not getattr(self, "_is_refresh_stage", False):
            stage = await self.create_refresh_stage()
            async def prepare(candidate):
                if candidate._refresh_generation != self._physical_collection():
                    raise RuntimeError("知识库版本已切换，请重试")
                return True
            await self._run_stage_operation(stage, prepare)
            await self._invalidate_after_publish()
            return {"success": True, "message": "活动知识库已清空，旧版本保留"}
        try:
            logger.info(f"开始清空向量存储: {self.collection_name}")
            start_time = time.time()

            # 一次事务删除所有数据并更新revision，避免刷新与清空交错
            deleted_counts = await asyncio.to_thread(self._atomic_delete, None)

            # 5. 清理FAISS索引
            if (
                self.index_manager
                and hasattr(self.index_manager, "faiss_index")
                and self.index_manager.faiss_index
            ):
                try:
                    # 重新创建空的FAISS索引
                    self.index_manager.create_index(self.vector_dim, self.index_type)
                    logger.info("FAISS索引已重新创建")
                except Exception as e:
                    logger.warning("重新创建FAISS索引失败，异常类型: %s", type(e).__name__)

            # 6. 清理查询缓存
            if self.query_cache:
                try:
                    await self.query_cache.clear()
                    logger.info("查询缓存已清空")
                except Exception as e:
                    logger.warning("清理查询缓存失败，异常类型: %s", type(e).__name__)

            # 7. 清理层次化检索器
            if self.hierarchical_retriever:
                try:
                    self.hierarchical_retriever.reset()
                    logger.info("层次化检索器状态已重置")
                except Exception as e:
                    logger.warning("重置层次化检索器失败，异常类型: %s", type(e).__name__)

            # 8. 重置统计信息
            self.stats = {
                "search_count": 0,
                "cache_hits": 0,
                "error_count": 0,
                "last_error": None,
            }

            elapsed_time = time.time() - start_time
            total_deleted = sum(deleted_counts.values())

            logger.info(
                f"向量存储清空完成: 删除了 {total_deleted} 个条目 "
                f"(文档: {deleted_counts['documents']}, 向量: {deleted_counts['vectors']}, "
                f"词条: {deleted_counts['terms']}, 索引: {deleted_counts['indices']}), "
                f"耗时: {elapsed_time:.3f}秒"
            )

            return {
                "success": True,
                "deleted_counts": deleted_counts,
                "total_deleted": total_deleted,
                "processing_time": elapsed_time,
                "message": "向量存储清空成功",
            }

        except Exception as e:
            logger.error("清空向量存储失败，异常类型: %s", type(e).__name__)
            return {
                "success": False,
                "error": type(e).__name__,
                "message": "向量存储清空失败",
            }


class IndexManager:
    """索引管理器"""

    def __init__(self, redis_client, collection_name: str):
        self.client = redis_client
        self.collection_name = collection_name
        self.index_key = f"{collection_name}:index"
        # FAISS位置属于本实例，不可写入所有实例共享的Redis位置映射
        self._document_ids: List[str] = []
        self._index_stats = {
            "total_vectors": 0,
            "last_update": 0,
            "search_count": 0,
            "build_time": 0,
        }

        # 尝试加载FAISS
        try:
            import faiss

            self.faiss = faiss
            self.faiss_available = True
            self.faiss_index = None
            logger.info("FAISS库加载成功")
        except ImportError:
            self.faiss_available = False
            self.faiss = None
            logger.warning("FAISS不可用，使用基础索引")

    def create_index(self, dim: int, index_type: str = "FLAT"):
        """创建索引"""
        if not self.faiss_available:
            logger.warning("FAISS不可用，跳过索引创建")
            return

        start_time = time.time()
        self._document_ids = []
        self._index_stats["total_vectors"] = 0

        try:
            if index_type == "FLAT":
                self.faiss_index = self.faiss.IndexFlatIP(dim)
            elif index_type == "IVF":
                # 改进的IVF配置
                nlist = min(100, max(10, dim // 20))  # 动态调整聚类数
                quantizer = self.faiss.IndexFlatIP(dim)
                self.faiss_index = self.faiss.IndexIVFFlat(quantizer, dim, nlist)
                # 设置搜索参数
                self.faiss_index.nprobe = min(10, nlist // 2)
            elif index_type == "HNSW":
                # 改进的HNSW配置
                M = min(64, max(16, dim // 30))  # 动态调整连接数
                self.faiss_index = self.faiss.IndexHNSWFlat(dim, M, self.faiss.METRIC_INNER_PRODUCT)
                # 设置搜索参数
                self.faiss_index.hnsw.efSearch = 64
                self.faiss_index.hnsw.efConstruction = 200
            else:
                self.faiss_index = self.faiss.IndexFlatIP(dim)

            build_time = time.time() - start_time
            self._index_stats["build_time"] = build_time
            self._index_stats["last_update"] = time.time()

            logger.info(
                f"创建{index_type}索引成功，维度: {dim}，耗时: {build_time:.3f}秒"
            )

        except Exception as e:
            logger.error("创建索引失败，异常类型: %s", type(e).__name__)
            self.faiss_index = None
            raise

    async def update_index(self, doc_ids: List[str], embeddings: List[np.ndarray]):
        """更新索引"""
        if not self.faiss_available or not self.faiss_index:
            logger.debug("索引不可用，跳过更新")
            return

        if not doc_ids or not embeddings:
            return

        start_time = time.time()

        try:
            # 添加到FAISS索引
            vectors = np.array(embeddings, dtype=np.float32)

            # 检查向量维度
            if vectors.shape[1] != self.faiss_index.d:
                raise ValueError(
                    f"向量维度不匹配: 期望 {self.faiss_index.d}, 实际 {vectors.shape[1]}"
                )

            # 归一化向量（用于内积相似度）
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            vectors = vectors / (norms + 1e-10)

            # 训练索引（如果需要）
            if (
                hasattr(self.faiss_index, "is_trained")
                and not self.faiss_index.is_trained
            ):
                logger.info("训练FAISS索引...")
                train_start = time.time()
                self.faiss_index.train(vectors)
                train_time = time.time() - train_start
                logger.info(f"索引训练完成，耗时: {train_time:.3f}秒")

            # 位置映射和本地FAISS同生命周期，不能由其他进程覆盖
            self.faiss_index.add(vectors)
            self._document_ids.extend(doc_ids)

            # 更新统计信息
            self._index_stats["total_vectors"] = self.faiss_index.ntotal
            self._index_stats["last_update"] = time.time()

            update_time = time.time() - start_time
            logger.debug(
                f"索引更新完成: 添加 {len(doc_ids)} 个向量，总计 {self.faiss_index.ntotal} 个，耗时: {update_time:.3f}秒"
            )

        except Exception as e:
            logger.error("更新索引失败，异常类型: %s", type(e).__name__)
            raise

    async def search_vectors(
        self, query_vector: np.ndarray, k: int
    ) -> List[Tuple[str, float]]:
        """搜索向量"""
        if (
            not self.faiss_available
            or not self.faiss_index
            or self.faiss_index.ntotal == 0
        ):
            logger.debug("索引不可用或为空，返回空结果")
            return []

        start_time = time.time()
        self._index_stats["search_count"] += 1

        try:
            # 检查查询向量维度
            if query_vector.shape[0] != self.faiss_index.d:
                raise ValueError(
                    f"查询向量维度不匹配: 期望 {self.faiss_index.d}, 实际 {query_vector.shape[0]}"
                )

            # 归一化查询向量
            query_vector = query_vector / (np.linalg.norm(query_vector) + 1e-10)
            query_vector = query_vector.reshape(1, -1).astype(np.float32)

            # 动态调整搜索参数
            search_k = min(k * 2, self.faiss_index.ntotal)  # 获取更多候选以提高质量

            # FAISS搜索
            scores, indices = self.faiss_index.search(query_vector, search_k)

            # FAISS返回的位置只在当前索引实例中有效
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0 or idx >= len(self._document_ids):
                    continue
                results.append((self._document_ids[int(idx)], float(score)))
                if len(results) >= k:
                    break

            search_time = time.time() - start_time
            logger.debug(
                f"向量搜索完成: 查询 {self.faiss_index.ntotal} 个向量，返回 {len(results)} 个结果，耗时: {search_time:.3f}秒"
            )
            return results

        except Exception as e:
            logger.error("向量搜索失败，异常类型: %s", type(e).__name__)
            return []

    def get_stats(self) -> Dict[str, Any]:
        """获取索引统计信息"""
        stats = self._index_stats.copy()
        if self.faiss_index:
            stats["index_type"] = type(self.faiss_index).__name__
            stats["is_trained"] = getattr(self.faiss_index, "is_trained", True)
        return stats


class QueryCache:
    """查询缓存"""

    def __init__(self, redis_client, ttl: int = 3600, max_cache_size: int = 1000, *, prefix: str = "query_cache:"):
        self.client = redis_client
        self.ttl = ttl
        self.max_cache_size = max_cache_size
        self.prefix = prefix
        self._cache_stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "evictions": 0,
        }

    def get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        try:
            data = self.client.get(self.prefix + key)
            if data:
                self._cache_stats["hits"] += 1
                return pickle.loads(data)
            else:
                self._cache_stats["misses"] += 1
        except Exception as e:
            logger.warning("缓存读取失败，异常类型: %s", type(e).__name__)
            self._cache_stats["misses"] += 1
        return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """设置缓存"""
        try:
            # 检查缓存大小，如果超限则清理最旧的条目
            self._ensure_cache_size()

            # 序列化数据并检查大小
            data = pickle.dumps(value)
            data_size = len(data)

            # 如果单个数据过大，记录警告但仍然缓存
            if data_size > 1024 * 1024:  # 1MB
                logger.warning("缓存数据过大: %d bytes", data_size)

            self.client.setex(self.prefix + key, ttl or self.ttl, data)
            self._cache_stats["sets"] += 1

        except Exception as e:
            logger.warning("缓存写入失败，异常类型: %s", type(e).__name__)

    def _ensure_cache_size(self):
        """确保缓存大小不超过限制"""
        try:
            # 获取当前缓存键的数量
            pattern = self.prefix + "*"
            cursor = 0
            keys = []

            while True:
                cursor, batch_keys = self.client.scan(cursor, match=pattern, count=100)
                keys.extend(batch_keys)
                if cursor == 0:
                    break

            if len(keys) >= self.max_cache_size:
                # 删除最旧的25%的缓存
                evict_count = len(keys) // 4
                if evict_count > 0:
                    # 简单策略：删除前N个键（FIFO近似）
                    to_delete = keys[:evict_count]
                    if to_delete:
                        self.client.delete(*to_delete)
                        self._cache_stats["evictions"] += len(to_delete)
                        logger.debug(f"清理了 {len(to_delete)} 个缓存条目")

        except Exception as e:
            logger.warning("缓存大小检查失败，异常类型: %s", type(e).__name__)

    async def invalidate_pattern(self, pattern: str):
        """Redis清理失败必须显式报告，不能伪装成缓存已清空"""
        try:
            cursor = 0
            deleted_count = 0
            while True:
                cursor, keys = await asyncio.to_thread(self.client.scan, cursor, match=self.prefix + pattern, count=100)
                if keys:
                    await asyncio.to_thread(self.client.delete, *keys)
                    deleted_count += len(keys)
                if cursor == 0:
                    break
            return deleted_count
        except Exception as error:
            logger.warning("缓存清理失败，异常类型: %s", type(error).__name__)
            raise CacheInvalidationError("查询缓存清理失败") from error

    async def clear(self):
        """仅在Redis实际清理完成后重置统计"""
        await self.invalidate_pattern("*")
        self._cache_stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "evictions": 0,
        }

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        return self._cache_stats.copy()


class BatchQueue:
    """批处理队列"""

    def __init__(self, max_size: int = 50):
        self.max_size = max_size
        self.queue = []
        self.lock = asyncio.Lock()

    async def add(self, item: Any) -> Optional[List[Any]]:
        """添加项目，如果达到批量大小则返回批次"""
        async with self.lock:
            self.queue.append(item)

            if len(self.queue) >= self.max_size:
                batch = self.queue[: self.max_size]
                self.queue = self.queue[self.max_size :]
                return batch

        return None

    async def flush(self) -> List[Any]:
        """清空队列"""
        async with self.lock:
            batch = self.queue[:]
            self.queue = []
            return batch
