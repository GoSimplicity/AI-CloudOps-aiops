#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
上下文感知检索器 - 提升对话连贯性
"""

import logging
import re
from collections import Counter
from typing import Dict, List, Tuple

from langchain_core.documents import Document

logger = logging.getLogger("aiops.assistant.context_retriever")


class ContextAwareRetriever:
    """上下文感知检索器，提升对话连贯性"""

    def __init__(self, base_retriever, query_rewriter, doc_ranker):
        self.base_retriever = base_retriever
        self.query_rewriter = query_rewriter
        self.doc_ranker = doc_ranker
        self.conversation_context = {}

    async def retrieve_with_context(
        self,
        query: str,
        session_id: str = None,
        history: List[Dict] = None,
        top_k: int = 8,
    ) -> List[Document]:
        """带上下文的智能检索 - 提升召回率优化版"""
        try:
            # 1. 构建增强查询
            enhanced_query = self._build_enhanced_query(query, history)

            # 2. 多查询检索 - 降低阈值提升召回率
            all_docs = []
            queries = self.query_rewriter.expand_query(enhanced_query)

            for q in queries:
                try:
                    # 兼容异步/同步检索器
                    result = self.base_retriever.invoke(q)
                    docs = await result if hasattr(result, "__await__") else result
                    all_docs.extend(docs)
                except Exception as e:
                    logger.warning(f"查询检索失败 '{q}': {e}")
                    continue

            # 3. 如果主查询结果太少，尝试部分匹配
            if len(all_docs) < top_k:
                # 尝试关键词查询
                query_words = query.split()
                if len(query_words) > 1:
                    for word in query_words:
                        if len(word) > 2:  # 跳过太短的词
                            try:
                                result = self.base_retriever.invoke(word)
                                word_docs = await result if hasattr(result, "__await__") else result
                                all_docs.extend(word_docs)
                                if len(all_docs) >= top_k * 2:
                                    break
                            except Exception as e:
                                logger.warning(f"关键词查询失败 '{word}': {e}")

            # 4. 去重
            unique_docs = self._deduplicate_documents(all_docs)

            # 5. 智能排序 - 增加返回数量
            if unique_docs:
                ranked_docs = self.doc_ranker.rank_documents(
                    enhanced_query, unique_docs, top_k * 2
                )
            else:
                ranked_docs = []

            # 6. 宽松的上下文过滤
            filtered_docs = self._context_filter_lenient(
                ranked_docs, session_id, history
            )

            return [doc for doc, _ in filtered_docs[:top_k]]

        except Exception as e:
            logger.error(f"上下文感知检索失败: {e}")
            # 降级到基础检索
            try:
                result = self.base_retriever.invoke(query)
                docs = await result if hasattr(result, "__await__") else result
                return docs[:top_k]
            except Exception:
                return []

    async def retrieve_relevant_docs(self, query: str, max_docs: int = 8) -> List[Document]:
        """兼容方法：按 AssistantAgent 预期提供的接口包装。"""
        return await self.retrieve_with_context(query, session_id=None, history=None, top_k=max_docs)

    def update_knowledge(self, documents: List[Document]):
        """占位以兼容上层调用；当前检索器无需持久状态。"""
        _ = documents

    def _build_enhanced_query(self, query: str, history: List[Dict] = None) -> str:
        """构建增强查询"""
        if not history:
            return query

        # 提取历史关键词
        historical_keywords = []
        for msg in history[-2:]:  # 减少历史消息数量
            if msg.get("role") == "user":
                keywords = re.findall(r"\w+", msg.get("content", ""))
                historical_keywords.extend([w for w in keywords if len(w) > 2])

        # 去重并选择最重要的关键词
        keyword_counts = Counter(historical_keywords)
        important_keywords = [
            k for k, _ in keyword_counts.most_common(2)
        ]  # 减少关键词数量

        # 构建增强查询
        if important_keywords:
            enhanced_query = f"{query} {' '.join(important_keywords)}"
        else:
            enhanced_query = query

        return enhanced_query

    def _deduplicate_documents(self, docs: List[Document]) -> List[Document]:
        """文档去重"""
        seen_content = set()
        unique_docs = []

        for doc in docs:
            # 使用内容前50字符作为去重标准（减少长度）
            content_hash = hash(doc.page_content[:50])
            if content_hash not in seen_content:
                seen_content.add(content_hash)
                unique_docs.append(doc)

        return unique_docs

    def _context_filter_lenient(
        self,
        ranked_docs: List[Tuple[Document, float]],
        session_id: str = None,
        history: List[Dict] = None,
    ) -> List[Tuple[Document, float]]:
        """宽松的基于上下文的文档过滤 - 提升召回率"""
        if not history or not ranked_docs:
            return ranked_docs

        # 分析对话主题
        conversation_topics = self._extract_conversation_topics(history)

        # 根据主题调整分数 - 使用更宽松的策略
        adjusted_docs = []
        for doc, score in ranked_docs:
            topic_relevance = self._calculate_topic_relevance(doc, conversation_topics)

            # 宽松的分数调整 - 避免过度惩罚
            if topic_relevance > 0.3:  # 降低阈值
                adjusted_score = score * 0.9 + topic_relevance * 0.1  # 减少调整幅度
            else:
                adjusted_score = score * 0.95  # 轻微降低分数而不是大幅惩罚

            adjusted_docs.append((doc, adjusted_score))

        # 重新排序但保持更多文档
        adjusted_docs.sort(key=lambda x: x[1], reverse=True)
        return adjusted_docs

    def _extract_conversation_topics(self, history: List[Dict]) -> List[str]:
        """提取对话主题"""
        topics = []
        for msg in history:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if "部署" in content or "安装" in content:
                    topics.append("deployment")
                elif "监控" in content or "观察" in content:
                    topics.append("monitoring")
                elif "故障" in content or "错误" in content:
                    topics.append("troubleshooting")
                elif "性能" in content or "优化" in content:
                    topics.append("performance")

        return list(set(topics))

    def _calculate_topic_relevance(self, doc: Document, topics: List[str]) -> float:
        """计算文档主题相关性"""
        if not topics:
            return 0.5

        content = doc.page_content.lower()
        relevance_scores = []

        topic_keywords = {
            "deployment": ["部署", "安装", "配置", "搭建"],
            "monitoring": ["监控", "观察", "检测", "巡检"],
            "troubleshooting": ["故障", "问题", "错误", "异常"],
            "performance": ["性能", "优化", "效率", "速度"],
        }

        for topic in topics:
            if topic in topic_keywords:
                keyword_count = sum(
                    1 for keyword in topic_keywords[topic] if keyword in content
                )
                relevance_scores.append(
                    min(keyword_count / len(topic_keywords[topic]), 1.0)
                )

        return max(relevance_scores) if relevance_scores else 0.5
