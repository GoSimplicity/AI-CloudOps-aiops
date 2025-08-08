#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
智能文档排序器 - 提升检索精确度
"""

import logging
import time
from typing import List, Tuple

from langchain_core.documents import Document
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger("aiops.assistant.document_ranker")


class DocumentRanker:
    """智能文档排序器，提升检索精确度"""

    def __init__(self):
        self.tfidf_vectorizer = TfidfVectorizer(
            max_features=500,  # 减少特征数量
            stop_words=None,
            ngram_range=(1, 2),
        )
        self.document_vectors = None
        self.fitted = False

    def fit(self, documents: List[Document]):
        """训练TF-IDF模型"""
        try:
            if not documents:
                return

            corpus = [doc.page_content for doc in documents]
            self.document_vectors = self.tfidf_vectorizer.fit_transform(corpus)
            self.fitted = True
            logger.info(f"文档排序器训练完成，文档数: {len(documents)}")
        except Exception as e:
            logger.error(f"文档排序器训练失败: {e}")

    def rank_documents(
        self, query: str, documents: List[Document], top_k: int = 10
    ) -> List[Tuple[Document, float]]:
        """对文档进行相关性排序 - 提升召回率优化版"""
        if not self.fitted or not documents:
            return [(doc, 0.7) for doc in documents[:top_k]]  # 提高默认分数

        try:
            # 查询向量化
            query_vector = self.tfidf_vectorizer.transform([query])

            # 计算文档相似度
            doc_vectors = self.tfidf_vectorizer.transform(
                [doc.page_content for doc in documents]
            )
            similarities = cosine_similarity(query_vector, doc_vectors).flatten()

            # 优化评分算法
            scored_docs = []
            for i, doc in enumerate(documents):
                tfidf_score = similarities[i]

                # 调整长度评分 - 不过度惩罚短文档
                content_length = len(doc.page_content)
                if content_length < 100:
                    length_score = 0.6  # 短文档基础分数
                elif content_length < 500:
                    length_score = min(content_length / 300, 1.0)
                else:
                    length_score = 1.0  # 长文档满分

                freshness_score = self._calculate_freshness_score(doc)

                # 关键词匹配加分
                query_words = set(query.lower().split())
                doc_words = set(doc.page_content.lower().split())
                keyword_overlap = len(query_words & doc_words) / max(
                    len(query_words), 1
                )

                # 文档类型加分
                doc_type_score = 1.0
                if doc.metadata:
                    filetype = doc.metadata.get("filetype", "").lower()
                    if filetype in ["md", "markdown", "txt"]:
                        doc_type_score = 1.2  # 文本文档加分
                    elif filetype in ["pdf", "doc"]:
                        doc_type_score = 1.1

                # 综合评分 - 调整权重提升召回率
                final_score = (
                    tfidf_score * 0.4  # 降低TF-IDF权重
                    + length_score * 0.15  # 降低长度权重
                    + freshness_score * 0.1  # 降低时间权重
                    + keyword_overlap * 0.25  # 增加关键词权重
                    + doc_type_score * 0.1  # 增加文档类型权重
                )

                scored_docs.append((doc, final_score))

            # 排序并返回更多结果
            scored_docs.sort(key=lambda x: x[1], reverse=True)
            return scored_docs[:top_k]

        except Exception as e:
            logger.error(f"文档排序失败: {e}")
            return [(doc, 0.6) for doc in documents[:top_k]]

    def _calculate_freshness_score(self, doc: Document) -> float:
        """计算文档新鲜度评分"""
        try:
            if doc.metadata and "modified_time" in doc.metadata:
                mod_time = doc.metadata["modified_time"]
                days_old = (time.time() - mod_time) / (24 * 3600)
                return max(0, 1 - days_old / 365)
            return 0.5
        except Exception:
            return 0.5
