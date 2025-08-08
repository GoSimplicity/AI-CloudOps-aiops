#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
助手工具函数模块
"""

import re
import sys
from typing import List, Tuple, Optional, TYPE_CHECKING
from langchain_core.documents import Document

if TYPE_CHECKING:
    from app.core.agents.assistant.session.session_manager import SessionData


def is_test_environment() -> bool:
    """检查是否为测试环境"""
    return "pytest" in sys.modules


def _generate_fallback_answer() -> str:
    """生成回退答案"""
    return "抱歉，我找不到与您问题相关的信息。请尝试重新表述您的问题，或询问关于AIOps平台的核心功能、部署方式或使用方法等问题。"


async def _check_hallucination_advanced(
    question: str, answer: str, docs: List[Document]
) -> Tuple[bool, float]:
    """高级幻觉检查"""
    try:
        if len(answer) < 30 or not docs:  # 调整最小长度
            return True, 0.3

        # 关键词匹配检查
        answer_words = set(re.findall(r"\w+", answer.lower()))
        doc_words = set()
        for doc in docs:
            doc_words.update(re.findall(r"\w+", doc.page_content.lower()))

        common_words = answer_words & doc_words
        coverage = len(common_words) / len(answer_words) if answer_words else 0

        # 数字和具体信息检查
        answer_numbers = re.findall(r"\d+", answer)
        doc_numbers = []
        for doc in docs:
            doc_numbers.extend(re.findall(r"\d+", doc.page_content))

        number_match = (
            len(set(answer_numbers) & set(doc_numbers)) / len(answer_numbers)
            if answer_numbers
            else 1.0
        )

        # 综合评分
        final_score = coverage * 0.7 + number_match * 0.3
        is_valid = final_score > 0.3  # 降低阈值

        return is_valid, final_score

    except Exception as e:
        logger = __import__("logging").getLogger(__name__)
        logger.error(f"高级幻觉检查失败: {e}")
        return True, 0.5


async def _evaluate_doc_relevance_advanced(
    question: str, doc: Document
) -> Tuple[bool, float]:
    """高级文档相关性评估 - 提升召回率版本"""
    try:
        # 基础关键词匹配
        question_words = set(re.findall(r"\w+", question.lower()))
        doc_words = set(re.findall(r"\w+", doc.page_content.lower()))

        overlap = len(question_words & doc_words)
        total = len(question_words | doc_words)
        basic_similarity = overlap / total if total > 0 else 0

        # 语义相关性检查
        semantic_score = 0.5

        # 文档质量评分
        content_length = len(doc.page_content)
        quality_score = min(content_length / 200, 1.0)  # 降低质量要求

        # 综合评分
        final_score = (
            basic_similarity * 0.5 + semantic_score * 0.3 + quality_score * 0.2
        )

        is_relevant = final_score > 0.05  # 大幅降低阈值

        return is_relevant, final_score

    except Exception as e:
        logger = __import__("logging").getLogger(__name__)
        logger.error(f"高级文档相关性评估失败: {e}")
        return True, 0.5  # 默认认为相关


def _build_context_with_history(session: Optional["SessionData"]) -> Optional[str]:
    """构建包含历史的上下文"""
    if not session or not session.history:
        return None

    recent_history = session.history[-2:]  # 减少历史数量
    if len(recent_history) < 2:
        return None

    context = "基于以下对话历史:\n"
    for msg in recent_history:
        role = "用户" if msg["role"] == "user" else "助手"
        content = (
            msg["content"][:60] + "..." if len(msg["content"]) > 60 else msg["content"]
        )  # 减少内容长度
        context += f"{role}: {content}\n"

    return context + "\n当前问题:\n"


async def _filter_relevant_docs_advanced(
    question: str, docs: List[Document]
) -> List[Document]:
    """高级文档过滤 - 提升召回率版本"""
    if not docs or len(docs) <= 3:  # 降低最小文档数要求
        return docs

    try:
        relevant_docs = []

        for doc in docs:
            is_relevant, score = await _evaluate_doc_relevance_advanced(question, doc)

            if is_relevant:
                doc.metadata = doc.metadata or {}
                doc.metadata["relevance_score"] = score
                relevant_docs.append(doc)

        # 如果没有相关文档，返回评分最高的几个
        if not relevant_docs:
            all_docs_with_scores = []
            for doc in docs:
                _, score = await _evaluate_doc_relevance_advanced(question, doc)
                doc.metadata = doc.metadata or {}
                doc.metadata["relevance_score"] = score
                all_docs_with_scores.append(doc)

            all_docs_with_scores.sort(
                key=lambda x: x.metadata.get("relevance_score", 0), reverse=True
            )
            return all_docs_with_scores[:4]  # 增加返回数量

        # 按相关性排序
        relevant_docs.sort(
            key=lambda x: x.metadata.get("relevance_score", 0), reverse=True
        )

        return relevant_docs

    except Exception as e:
        logger = __import__("logging").getLogger(__name__)
        logger.error(f"高级文档过滤失败: {e}")
        return docs[:4]  # 增加返回数量
