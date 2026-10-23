#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 层次化检索系统，解决多文档准确率下降问题
"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.documents import Document
import numpy as np

logger = logging.getLogger("aiops.hierarchical_retriever")


class QueryComplexity(Enum):
    """查询复杂度"""

    SIMPLE = "simple"  # 简单查询
    MODERATE = "moderate"  # 中等复杂度
    COMPLEX = "complex"  # 复杂查询


class RetrievalStage(Enum):
    """检索阶段"""

    COARSE = "coarse"  # 粗检索
    FINE = "fine"  # 精检索
    RERANK = "rerank"  # 重排序
    FILTER = "filter"  # 过滤


@dataclass
class DocumentCluster:
    """文档聚类"""

    cluster_id: str
    centroid: np.ndarray
    documents: List[Document] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    quality_score: float = 0.0
    size: int = 0

    def add_document(self, doc: Document, embedding: np.ndarray):
        """添加文档到聚类"""
        self.documents.append(doc)
        self.size += 1

        # 更新质心
        if self.size == 1:
            self.centroid = embedding.copy()
        else:
            # 增量更新质心
            alpha = 1.0 / self.size
            self.centroid = (1 - alpha) * self.centroid + alpha * embedding


@dataclass
class RetrievalContext:
    """检索上下文"""

    query: str
    query_embedding: np.ndarray
    query_complexity: QueryComplexity
    preferred_types: List[str] = field(default_factory=list)
    domain_hints: List[str] = field(default_factory=list)
    user_context: Optional[Dict[str, Any]] = None
    retrieval_budget: int = 1000  # 检索预算（最大检索文档数）


@dataclass
class StageResult:
    """阶段结果"""

    stage: RetrievalStage
    documents: List[Tuple[Document, float]]
    processing_time: float
    candidates_count: int
    metadata: Dict[str, Any] = field(default_factory=dict)


class DocumentQualityScorer:
    """文档质量评分器"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.quality_cache = {}

        # 质量评分权重
        self.weights = {
            "content_length": 0.15,
            "structure_score": 0.25,
            "metadata_richness": 0.20,
            "language_quality": 0.15,
            "uniqueness": 0.25,
        }

    def calculate_quality_score(
        self, doc: Document, doc_embedding: np.ndarray
    ) -> float:
        """计算文档质量分数"""
        doc_id = doc.metadata.get("chunk_id") or hash(doc.page_content)

        if doc_id in self.quality_cache:
            return self.quality_cache[doc_id]

        # 内容长度评分
        content_score = self._score_content_length(doc.page_content)

        # 结构评分
        structure_score = self._score_structure(doc)

        # 元数据丰富度评分
        metadata_score = self._score_metadata_richness(doc.metadata)

        # 语言质量评分
        language_score = self._score_language_quality(doc.page_content)

        # 独特性评分（基于嵌入向量）
        uniqueness_score = self._score_uniqueness(doc_embedding)

        # 加权综合评分
        total_score = (
            content_score * self.weights["content_length"]
            + structure_score * self.weights["structure_score"]
            + metadata_score * self.weights["metadata_richness"]
            + language_score * self.weights["language_quality"]
            + uniqueness_score * self.weights["uniqueness"]
        )

        self.quality_cache[doc_id] = total_score
        return total_score

    def _score_content_length(self, content: str) -> float:
        """评分内容长度"""
        length = len(content)
        if length < 50:
            return 0.2
        elif length < 200:
            return 0.6
        elif length < 800:
            return 1.0
        elif length < 2000:
            return 0.8
        else:
            return 0.6  # 过长的内容可能质量不高

    def _score_structure(self, doc: Document) -> float:
        """评分文档结构"""
        content = doc.page_content
        metadata = doc.metadata

        score = 0.0

        # MD文档结构评分
        if metadata.get("document_type") == "markdown":
            if metadata.get("has_code"):
                score += 0.3
            if metadata.get("has_table"):
                score += 0.2
            if metadata.get("title_hierarchy"):
                score += 0.3
            if len(metadata.get("element_types", [])) > 2:
                score += 0.2

        # 通用结构评分
        if "```" in content:  # 包含代码块
            score += 0.2
        if content.count("\n") > 3:  # 多行内容
            score += 0.1
        if any(marker in content for marker in ["#", "*", "-", "1."]):  # 结构标记
            score += 0.2

        return min(score, 1.0)

    def _score_metadata_richness(self, metadata: Dict[str, Any]) -> float:
        """评分元数据丰富度"""
        score = 0.0

        key_fields = [
            "title_hierarchy",
            "source",
            "category",
            "tags",
            "languages",
            "element_types",
            "has_code",
            "has_table",
        ]

        for key_field in key_fields:
            if key_field in metadata and metadata[key_field]:
                if isinstance(metadata[key_field], (list, tuple)):
                    score += 0.1 if len(metadata[key_field]) > 0 else 0
                else:
                    score += 0.1

        return min(score, 1.0)

    def _score_language_quality(self, content: str) -> float:
        """评分语言质量"""
        # 简单的语言质量评分
        if not content:
            return 0.0

        # 检查是否包含有意义的词汇
        words = content.split()
        if len(words) < 3:
            return 0.3

        # 检查重复内容比例
        unique_words = set(words)
        diversity_ratio = len(unique_words) / len(words)

        # 检查是否包含技术术语
        tech_terms = [
            "kubernetes",
            "docker",
            "prometheus",
            "nginx",
            "deployment",
            "service",
            "pod",
            "cluster",
            "namespace",
            "yaml",
            "json",
            "api",
            "http",
            "https",
            "tcp",
            "udp",
        ]

        tech_score = sum(
            1 for term in tech_terms if term.lower() in content.lower()
        ) / len(tech_terms)

        return min(diversity_ratio * 0.7 + tech_score * 0.3, 1.0)

    def _score_uniqueness(self, embedding: np.ndarray) -> float:
        """评分独特性（基于向量方差）"""
        # 简单的独特性评分：基于向量的方差
        variance = np.var(embedding)
        # 归一化方差分数
        return min(variance / 0.1, 1.0)


class ClusterManager:
    """聚类管理器"""

    def __init__(self, max_clusters: int = 50, min_cluster_size: int = 3):
        self.max_clusters = max_clusters
        self.min_cluster_size = min_cluster_size
        self.clusters: List[DocumentCluster] = []
        self.document_to_cluster: Dict[str, str] = {}

    def add_document(self, doc: Document, embedding: np.ndarray, quality_score: float):
        """添加文档到聚类"""
        doc_id = doc.metadata.get("chunk_id") or hash(doc.page_content)

        # 寻找最相似的聚类
        best_cluster = None
        best_similarity = -1

        for cluster in self.clusters:
            similarity = self._cosine_similarity(embedding, cluster.centroid)
            if similarity > best_similarity and similarity > 0.75:  # 相似度阈值
                best_similarity = similarity
                best_cluster = cluster

        if best_cluster and best_cluster.size < 20:  # 限制聚类大小
            # 添加到现有聚类
            best_cluster.add_document(doc, embedding)
            best_cluster.quality_score = (
                best_cluster.quality_score * (best_cluster.size - 1) + quality_score
            ) / best_cluster.size
            self.document_to_cluster[doc_id] = best_cluster.cluster_id
        else:
            # 创建新聚类
            if len(self.clusters) < self.max_clusters:
                cluster_id = f"cluster_{len(self.clusters)}"
                new_cluster = DocumentCluster(
                    cluster_id=cluster_id,
                    centroid=embedding.copy(),
                    quality_score=quality_score,
                )
                new_cluster.add_document(doc, embedding)
                self.clusters.append(new_cluster)
                self.document_to_cluster[doc_id] = cluster_id

    def get_top_clusters(
        self, query_embedding: np.ndarray, k: int = 10
    ) -> List[DocumentCluster]:
        """获取与查询最相似的聚类"""
        cluster_scores = []

        for cluster in self.clusters:
            # 如果总聚类数量不多，放宽size要求
            min_size_threshold = (
                max(1, self.min_cluster_size // 2)
                if len(self.clusters) < 5
                else self.min_cluster_size
            )

            if cluster.size >= min_size_threshold:
                similarity = self._cosine_similarity(query_embedding, cluster.centroid)
                combined_score = similarity * 0.7 + cluster.quality_score * 0.3
                cluster_scores.append((cluster, combined_score))

        cluster_scores.sort(key=lambda x: x[1], reverse=True)
        selected_clusters = [cluster for cluster, _ in cluster_scores[:k]]

        logger.debug(
            f"获取聚类结果: 总聚类数={len(self.clusters)}, 符合条件={len(cluster_scores)}, 返回={len(selected_clusters)}"
        )
        return selected_clusters

    def _cosine_similarity(self, vec1: np.ndarray, vec2: np.ndarray) -> float:
        """计算余弦相似度"""
        dot_product = np.dot(vec1, vec2)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)


class QueryRouter:
    """查询路由器"""

    def __init__(self):
        # 查询模式
        self.complexity_patterns = {
            QueryComplexity.SIMPLE: [
                r"\b(什么是|what is|定义|definition)\b",
                r"\b(多少|how many|count)\b",
                r"\b(是否|是不是|can|能否)\b",
            ],
            QueryComplexity.MODERATE: [
                r"\b(如何|how to|怎么|步骤|step)\b",
                r"\b(为什么|why|原因|reason)\b",
                r"\b(配置|config|设置|setting)\b",
            ],
            QueryComplexity.COMPLEX: [
                r"\b(故障|错误|问题|排查|troubleshoot|debug)\b",
                r"\b(优化|性能|performance|optimize)\b",
                r"\b(架构|architecture|设计|design)\b",
                r"\b(部署|deploy|安装|install|实现|implement)\b",
            ],
        }

        self.domain_patterns = {
            "kubernetes": [r"\b(k8s|kubernetes|pod|deployment|service|namespace)\b"],
            "prometheus": [r"\b(prometheus|metric|监控|monitor|alert)\b"],
            "docker": [r"\b(docker|container|镜像|image)\b"],
            "network": [r"\b(网络|network|ip|port|tcp|udp|http)\b"],
            "storage": [r"\b(存储|storage|volume|pv|pvc|disk)\b"],
        }

    def analyze_query(self, query: str) -> RetrievalContext:
        """分析查询并生成检索上下文"""
        import re

        query_lower = query.lower()

        # 分析复杂度
        complexity = QueryComplexity.SIMPLE
        for comp, patterns in self.complexity_patterns.items():
            if any(re.search(pattern, query_lower) for pattern in patterns):
                complexity = comp
                break

        # 分析领域提示
        domain_hints = []
        for domain, patterns in self.domain_patterns.items():
            if any(re.search(pattern, query_lower) for pattern in patterns):
                domain_hints.append(domain)

        # 分析偏好类型
        preferred_types = []
        if re.search(r"\b(代码|code|script|命令)\b", query_lower):
            preferred_types.append("code_block")
        if re.search(r"\b(配置|config|参数|setting)\b", query_lower):
            preferred_types.extend(["code_block", "table"])
        if re.search(r"\b(步骤|教程|guide|tutorial)\b", query_lower):
            preferred_types.extend(["title", "list"])

        # 设置检索预算
        budget_map = {
            QueryComplexity.SIMPLE: 500,
            QueryComplexity.MODERATE: 800,
            QueryComplexity.COMPLEX: 1200,
        }

        return RetrievalContext(
            query=query,
            query_embedding=np.array([]),  # 将在后续填充
            query_complexity=complexity,
            preferred_types=preferred_types,
            domain_hints=domain_hints,
            retrieval_budget=budget_map[complexity],
        )


class HierarchicalRetriever:
    """层次化检索器"""

    def __init__(self, vector_store, config: Optional[Dict[str, Any]] = None):
        self.vector_store = vector_store
        self.config = config or {}

        # 初始化组件
        self.quality_scorer = DocumentQualityScorer(config)
        self.cluster_manager = ClusterManager(
            max_clusters=self.config.get("max_clusters", 50),
            min_cluster_size=self.config.get("min_cluster_size", 3),
        )
        self.query_router = QueryRouter()

        # 动态阈值管理
        self.dynamic_thresholds = {
            "base_similarity": 0.3,
            "cluster_similarity": 0.6,
            "final_similarity": 0.4,
        }

        # 性能统计
        self.stats = {
            "total_documents": 0,
            "total_clusters": 0,
            "cache_hits": 0,
            "retrieval_times": deque(maxlen=100),
        }

        logger.info("层次化检索器初始化完成")

    async def initialize_clusters(
        self, documents: List[Document], embeddings: List[np.ndarray]
    ):
        """初始化文档聚类"""
        logger.info(f"开始聚类 {len(documents)} 个文档")

        for doc, embedding in zip(documents, embeddings):
            quality_score = self.quality_scorer.calculate_quality_score(doc, embedding)
            self.cluster_manager.add_document(doc, embedding, quality_score)

        self.stats["total_documents"] = len(self.cluster_manager.document_to_cluster)
        self.stats["total_clusters"] = len(self.cluster_manager.clusters)

        logger.info(f"聚类完成，生成 {self.stats['total_clusters']} 个聚类")

    def reset(self) -> None:
        """清空聚类中的文档，避免刷新后重新召回已删除内容"""
        self.cluster_manager.clusters.clear()
        self.cluster_manager.document_to_cluster.clear()
        self.quality_scorer.quality_cache.clear()
        self.stats["total_documents"] = 0
        self.stats["total_clusters"] = 0

    async def hierarchical_search(
        self, query: str, k: int = 5, context: Optional[Dict[str, Any]] = None
    ) -> List[Tuple[Document, float]]:
        """层次化搜索"""
        start_time = time.time()

        try:
            # Step 1: 查询分析和路由
            retrieval_context = self.query_router.analyze_query(query)

            # 生成查询向量
            query_embedding = await asyncio.to_thread(
                self.vector_store.embedding_model.embed_query, query
            )
            retrieval_context.query_embedding = np.array(
                query_embedding, dtype=np.float32
            )
            retrieval_context.user_context = context

            # 动态调整阈值
            self._adjust_dynamic_thresholds(retrieval_context)

            # Step 2: 粗检索 - 聚类级别筛选
            coarse_result = await self._coarse_retrieval(retrieval_context, k * 3)

            # Step 3: 精检索 - 文档级别精确匹配
            fine_result = await self._fine_retrieval(
                retrieval_context, coarse_result, k * 2
            )

            # Step 4: 重排序 - 多维度综合排序
            rerank_result = await self._rerank_documents(
                retrieval_context, fine_result, k
            )

            # Step 5: 最终过滤 - 质量和相关性过滤
            final_result = await self._final_filtering(
                retrieval_context, rerank_result, k
            )

            processing_time = time.time() - start_time
            self.stats["retrieval_times"].append(processing_time)

            logger.info(
                f"层次化检索完成: {len(final_result)} 个结果, "
                f"耗时 {processing_time:.3f}s"
            )

            return final_result

        except Exception as e:
            logger.error(f"层次化检索失败: {e}")
            return []

    async def _coarse_retrieval(self, context: RetrievalContext, k: int) -> StageResult:
        """粗检索 - 聚类级别"""
        start_time = time.time()

        # 获取相似聚类
        top_clusters = self.cluster_manager.get_top_clusters(
            context.query_embedding, k // 3
        )

        # 从聚类中采样文档
        candidate_docs = []
        for cluster in top_clusters:
            # 根据查询复杂度调整采样数量
            sample_size = {
                QueryComplexity.SIMPLE: 3,
                QueryComplexity.MODERATE: 5,
                QueryComplexity.COMPLEX: 8,
            }.get(context.query_complexity, 5)

            # 从聚类中选择最相关的文档
            cluster_docs = []
            for doc in cluster.documents:
                doc_embedding = await self._get_document_embedding(doc)
                if doc_embedding is not None:
                    similarity = self._cosine_similarity(
                        context.query_embedding, doc_embedding
                    )
                    cluster_docs.append((doc, similarity))

            # 排序并取前N个
            cluster_docs.sort(key=lambda x: x[1], reverse=True)
            candidate_docs.extend(cluster_docs[:sample_size])

        processing_time = time.time() - start_time

        return StageResult(
            stage=RetrievalStage.COARSE,
            documents=candidate_docs,
            processing_time=processing_time,
            candidates_count=len(candidate_docs),
            metadata={"clusters_used": len(top_clusters)},
        )

    async def _fine_retrieval(
        self, context: RetrievalContext, coarse_result: StageResult, k: int
    ) -> StageResult:
        """精检索 - 文档级别"""
        start_time = time.time()

        # 结合原始相似度搜索结果
        original_results = []
        try:
            from app.core.vector.redis_vector_store import SearchConfig

            search_config = SearchConfig(
                similarity_threshold=self.dynamic_thresholds["base_similarity"],
                use_cache=True,
                use_mmr=False,  # 在这个阶段不使用MMR
            )

            vector_results = await self.vector_store.similarity_search(
                context.query, k * 2, search_config
            )
            original_results = list(vector_results)

        except Exception as e:
            logger.warning(f"向量搜索失败: {e}")

        # 合并结果
        all_candidates = {}

        # 添加聚类结果
        for doc, score in coarse_result.documents:
            doc_id = doc.metadata.get("chunk_id") or hash(doc.page_content)
            all_candidates[doc_id] = (doc, score * 0.6)  # 聚类结果权重

        # 添加向量搜索结果
        for doc, score in original_results:
            doc_id = doc.metadata.get("chunk_id") or hash(doc.page_content)
            if doc_id in all_candidates:
                # 组合分数
                existing_doc, existing_score = all_candidates[doc_id]
                combined_score = existing_score + score * 0.4
                all_candidates[doc_id] = (doc, combined_score)
            else:
                all_candidates[doc_id] = (doc, score * 0.4)

        # 排序并取前k个
        sorted_candidates = sorted(
            all_candidates.values(), key=lambda x: x[1], reverse=True
        )[:k]

        processing_time = time.time() - start_time

        return StageResult(
            stage=RetrievalStage.FINE,
            documents=sorted_candidates,
            processing_time=processing_time,
            candidates_count=len(sorted_candidates),
            metadata={
                "cluster_results": len(coarse_result.documents),
                "vector_results": len(original_results),
                "combined_results": len(all_candidates),
            },
        )

    async def _rerank_documents(
        self, context: RetrievalContext, fine_result: StageResult, k: int
    ) -> StageResult:
        """重排序 - 多维度排序"""
        start_time = time.time()

        reranked_docs = []

        for doc, base_score in fine_result.documents:
            # 基础相似度分数
            similarity_score = base_score

            # 质量分数
            doc_embedding = await self._get_document_embedding(doc)
            quality_score = (
                self.quality_scorer.calculate_quality_score(doc, doc_embedding)
                if doc_embedding is not None
                else 0.5
            )

            # 类型匹配分数
            type_score = self._calculate_type_match_score(doc, context)

            # 领域匹配分数
            domain_score = self._calculate_domain_match_score(doc, context)

            # MD结构化分数
            structure_score = self._calculate_structure_score(doc, context)

            # 综合分数
            final_score = (
                similarity_score * 0.35
                + quality_score * 0.25
                + type_score * 0.15
                + domain_score * 0.15
                + structure_score * 0.10
            )

            reranked_docs.append((doc, final_score))

        # 排序
        reranked_docs.sort(key=lambda x: x[1], reverse=True)

        processing_time = time.time() - start_time

        return StageResult(
            stage=RetrievalStage.RERANK,
            documents=reranked_docs[:k],
            processing_time=processing_time,
            candidates_count=len(reranked_docs),
        )

    async def _final_filtering(
        self, context: RetrievalContext, rerank_result: StageResult, k: int
    ) -> List[Tuple[Document, float]]:
        """最终过滤"""
        start_time = time.time()

        final_docs = []
        seen_content = set()

        for doc, score in rerank_result.documents:
            # 分数阈值过滤
            if score < self.dynamic_thresholds["final_similarity"]:
                continue

            # 去重过滤
            content_hash = hashlib.md5(doc.page_content.encode()).hexdigest()
            if content_hash in seen_content:
                continue
            seen_content.add(content_hash)

            # 长度过滤
            if len(doc.page_content.strip()) < 20:
                continue

            # 多样性检查
            if self._check_diversity(doc, final_docs):
                final_docs.append((doc, score))

                if len(final_docs) >= k:
                    break

        processing_time = time.time() - start_time
        logger.debug(f"最终过滤耗时: {processing_time:.3f}s")

        return final_docs

    def _adjust_dynamic_thresholds(self, context: RetrievalContext):
        """动态调整阈值"""
        # 根据文档数量调整
        doc_count = self.stats["total_documents"]

        if doc_count > 1000:
            # 文档多时提高阈值
            self.dynamic_thresholds["base_similarity"] = 0.4
            self.dynamic_thresholds["final_similarity"] = 0.5
        elif doc_count > 500:
            self.dynamic_thresholds["base_similarity"] = 0.35
            self.dynamic_thresholds["final_similarity"] = 0.45
        else:
            self.dynamic_thresholds["base_similarity"] = 0.3
            self.dynamic_thresholds["final_similarity"] = 0.4

        # 根据查询复杂度调整
        if context.query_complexity == QueryComplexity.SIMPLE:
            self.dynamic_thresholds["final_similarity"] *= 1.1
        elif context.query_complexity == QueryComplexity.COMPLEX:
            self.dynamic_thresholds["final_similarity"] *= 0.9

    def _calculate_type_match_score(
        self, doc: Document, context: RetrievalContext
    ) -> float:
        """计算类型匹配分数（增强版）"""
        base_score = 0.5

        # 基础元素类型匹配
        if context.preferred_types:
            element_types = doc.metadata.get("element_types", [])
            if element_types:
                matches = sum(
                    1 for pref in context.preferred_types if pref in element_types
                )
                base_score = min(matches / len(context.preferred_types), 1.0)

        # 增强元数据匹配
        content_patterns = doc.metadata.get("content_patterns", [])
        semantic_tags = doc.metadata.get("semantic_tags", [])

        # 内容模式匹配
        pattern_bonus = 0.0
        for pattern in content_patterns:
            pattern_type = pattern.get("pattern_type", "")
            confidence = pattern.get("confidence", 0.0)

            if any(pref in pattern_type for pref in context.preferred_types):
                pattern_bonus += confidence * 0.2

        # 语义标签匹配
        tag_bonus = 0.0
        for tag in semantic_tags:
            tag_name = tag.get("tag", "")
            confidence = tag.get("confidence", 0.0)

            if any(pref in tag_name for pref in context.preferred_types):
                tag_bonus += confidence * 0.15

        return min(base_score + pattern_bonus + tag_bonus, 1.0)

    def _calculate_domain_match_score(
        self, doc: Document, context: RetrievalContext
    ) -> float:
        """计算领域匹配分数（增强版）"""
        base_score = 0.5

        if not context.domain_hints:
            return base_score

        content_lower = doc.page_content.lower()
        metadata = doc.metadata

        score = 0.0

        # 基础领域匹配
        for domain in context.domain_hints:
            if domain in content_lower:
                score += 0.3
            if metadata.get("category") == domain:
                score += 0.2
            if domain in metadata.get("tags", []):
                score += 0.1

        # 增强元数据领域匹配
        technical_domains = metadata.get("technical_domains", [])
        if technical_domains:
            domain_overlap = len(set(context.domain_hints) & set(technical_domains))
            if domain_overlap > 0:
                score += domain_overlap / len(context.domain_hints) * 0.4

        # 技术概念匹配
        technical_concepts = metadata.get("technical_concepts", [])
        concept_score = 0.0
        for concept_data in technical_concepts:
            if isinstance(concept_data, dict):
                concept_domain = concept_data.get("domain", "")
                concept_freq = concept_data.get("frequency", 0)
                if concept_domain in context.domain_hints:
                    concept_score += min(concept_freq / 10, 0.1)

        score += concept_score

        return min(score, 1.0)

    def _calculate_structure_score(
        self, doc: Document, context: RetrievalContext
    ) -> float:
        """计算MD结构化分数（增强版）"""
        base_score = 0.5

        if doc.metadata.get("document_type") != "markdown":
            return base_score

        score = 0.0

        # 基础结构化程度评分
        if doc.metadata.get("has_code"):
            score += 0.25
        if doc.metadata.get("has_table"):
            score += 0.15
        if doc.metadata.get("title_hierarchy"):
            score += 0.25
        if len(doc.metadata.get("element_types", [])) > 2:
            score += 0.15

        # 增强结构化评分
        readability_score = doc.metadata.get("readability_score", 0.0)
        completeness_score = doc.metadata.get("completeness_score", 0.0)
        technical_depth = doc.metadata.get("technical_depth", 0.0)

        # 可读性奖励
        score += readability_score * 0.1

        # 完整性奖励
        score += completeness_score * 0.15

        # 技术深度奖励
        score += technical_depth * 0.1

        # 内容复杂度匹配
        complexity = doc.metadata.get("content_complexity", "simple")
        complexity_bonus = {
            "simple": 0.0,
            "moderate": 0.05,
            "complex": 0.1,
            "expert": 0.15,
        }.get(complexity, 0.0)
        score += complexity_bonus

        # 关键信息奖励
        key_info_bonus = 0.0
        if doc.metadata.get("key_commands"):
            key_info_bonus += 0.05
        if doc.metadata.get("configuration_files"):
            key_info_bonus += 0.05
        if doc.metadata.get("api_endpoints"):
            key_info_bonus += 0.03

        score += key_info_bonus

        return min(score, 1.0)

    def _check_diversity(
        self, doc: Document, selected_docs: List[Tuple[Document, float]]
    ) -> bool:
        """检查文档多样性"""
        if not selected_docs:
            return True

        doc_words = set(doc.page_content.lower().split()[:50])

        for selected_doc, _ in selected_docs:
            selected_words = set(selected_doc.page_content.lower().split()[:50])

            if doc_words and selected_words:
                similarity = len(doc_words & selected_words) / len(
                    doc_words | selected_words
                )
                if similarity > 0.8:  # 过高相似度
                    return False

        return True

    def _cosine_similarity(self, vec1: np.ndarray, vec2: np.ndarray) -> float:
        """计算余弦相似度"""
        dot_product = np.dot(vec1, vec2)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(dot_product / (norm1 * norm2))

    async def _get_document_embedding(self, doc: Document) -> Optional[np.ndarray]:
        """获取文档嵌入向量"""
        try:
            doc_id = (
                doc.metadata.get("chunk_id")
                or hashlib.md5(doc.page_content.encode()).hexdigest()
            )
            vec_key = f"{self.vector_store.collection_name}:vec:{doc_id}"

            vec_bytes = await asyncio.to_thread(self.vector_store.client.get, vec_key)
            if vec_bytes:
                return np.frombuffer(vec_bytes, dtype=np.float32)
        except Exception as e:
            logger.warning(f"获取文档嵌入失败: {e}")

        return None

    def get_stats(self) -> Dict[str, Any]:
        """获取检索统计信息"""
        avg_time = (
            sum(self.stats["retrieval_times"]) / len(self.stats["retrieval_times"])
            if self.stats["retrieval_times"]
            else 0
        )

        return {
            "total_documents": self.stats["total_documents"],
            "total_clusters": self.stats["total_clusters"],
            "cache_hits": self.stats["cache_hits"],
            "average_retrieval_time": avg_time,
            "current_thresholds": self.dynamic_thresholds,
        }
