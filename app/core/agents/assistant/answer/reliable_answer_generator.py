#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
可靠的答案生成器 - 注重稳定性和回答质量
"""

import asyncio
import logging
import time
from typing import Any, Dict, List

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger("aiops.assistant.answer_generator")


class ReliableAnswerGenerator:
    """可靠的答案生成器 - 注重稳定性和回答质量"""

    def __init__(self, llm):
        self.llm = llm
        self._cache = {}  # 简单的内存缓存
        self._last_cleanup = time.time()

    async def generate_structured_answer(
        self, question: str, docs: List[Document], context: str = None
    ) -> Dict[str, Any]:
        """生成结构化答案 - AI思考优化版"""

        start_time = time.time()
        logger.debug("开始生成结构化答案")

        try:
            # 确保至少有一个文档
            if not docs:
                return self._get_simple_response("没有找到相关文档", [])

            # 1. 分类问题类型
            question_type = self._classify_question_enhanced(question)
            logger.debug(f"问题分类: {question_type}")

            # 2. 构建增强的上下文
            enhanced_context = self._build_enhanced_context(
                docs, question, question_type
            )

            # 3. 使用AI思考模式生成答案
            answer = await self._generate_document_based_answer_enhanced(
                question, enhanced_context, question_type, context, docs
            )

            # 4. 提取关键点
            key_points = self._extract_enhanced_key_points(answer, docs)

            # 5. 计算置信度
            confidence = self._calculate_enhanced_confidence(question, answer, docs)

            # 6. 格式化最终结果
            result = {
                "answer": answer,
                "question_type": question_type,
                "key_points": key_points,
                "confidence": confidence,
                "source_count": len(docs),
                "processing_time_ms": int((time.time() - start_time) * 1000),
            }

            return result

        except Exception as e:
            logger.error(f"生成结构化答案失败: {e}")
            return await self._generate_emergency_document_answer(question, docs)

    def _build_enhanced_context(
        self, docs: List[Document], question: str, question_type: str
    ) -> str:
        """构建增强的上下文，确保内容丰富"""
        if not docs:
            return ""

        context_parts = []
        max_docs = min(len(docs), 6)  # 增加文档数量

        for i, doc in enumerate(docs[:max_docs]):
            source = (
                doc.metadata.get("filename", f"文档{i + 1}")
                if doc.metadata
                else f"文档{i + 1}"
            )

            # 更智能的内容提取
            content = self._extract_relevant_content_enhanced(
                doc.page_content, question, question_type
            )

            if content and len(content.strip()) > 20:  # 确保内容有意义
                context_parts.append(f"[{source}]\n{content}")

        return "\n\n".join(context_parts)

    def _extract_relevant_content_enhanced(
        self, content: str, question: str, question_type: str
    ) -> str:
        """增强的相关内容提取"""
        if not content:
            return ""

        # 获取问题关键词
        question_words = set(question.lower().split())

        # 根据问题类型添加相关关键词
        type_keywords = {
            "core_architecture": ["核心", "功能", "模块", "组件", "架构"],
            "architecture": ["系统", "架构", "设计", "组件"],
            "deployment": ["部署", "安装", "配置", "启动"],
            "monitoring": ["监控", "检测", "指标", "告警"],
            "troubleshooting": ["故障", "问题", "错误", "排查"],
            "performance": ["性能", "优化", "效率", "速度"],
            "features": ["特性", "特点", "功能", "能力"],
            "technical": ["技术", "实现", "原理", "算法"],
        }

        relevant_keywords = type_keywords.get(question_type, [])
        all_keywords = question_words.union(set(relevant_keywords))

        # 按段落分割并评分
        paragraphs = content.split("\n\n")
        scored_paragraphs = []

        for paragraph in paragraphs:
            if len(paragraph.strip()) < 20:
                continue

            paragraph_lower = paragraph.lower()

            # 计算相关性分数
            keyword_count = sum(
                1 for keyword in all_keywords if keyword in paragraph_lower
            )
            score = keyword_count / max(len(all_keywords), 1)

            # 标题和重要标记加分
            if any(
                marker in paragraph
                for marker in ["#", "##", "###", "**", "重要", "核心"]
            ):
                score += 0.3

            scored_paragraphs.append((paragraph, score))

        # 排序并选择最相关的段落
        scored_paragraphs.sort(key=lambda x: x[1], reverse=True)

        # 选择最多4个最相关的段落
        selected_paragraphs = [p[0] for p in scored_paragraphs[:4] if p[1] > 0]

        if not selected_paragraphs:
            # 如果没有相关段落，返回前几段
            return "\n".join(paragraphs[:2])

        return "\n\n".join(selected_paragraphs)

    async def _generate_document_based_answer_enhanced(
        self,
        question: str,
        context: str,
        question_type: str,
        history_context: str = None,
        docs: List[Document] = None,
    ) -> str:
        """增强的基于文档的答案生成 - AI思考优化版"""
        try:
            if not context or len(context.strip()) < 20:
                return await self._force_document_answer(
                    question, docs or [], question_type
                )

            # 针对不同问题类型的专门思考提示
            type_prompts = {
                "core_architecture": "你是AI思考者。请思考文档内容关于系统核心功能模块的信息，理解后用自己的语言简明总结。",
                "architecture": "你是AI思考者。请思考文档中的系统架构信息，分析后提供简洁清晰的总结。",
                "deployment": "你是AI思考者。请思考文档中的部署和配置信息，理解后提供简洁的部署要点。",
                "monitoring": "你是AI思考者。请思考文档中的监控功能信息，分析关键点后提供简明摘要。",
                "troubleshooting": "你是AI思考者。请思考文档中的故障排查信息，分析关键解决方案后简洁总结。",
                "performance": "你是AI思考者。请思考文档中的性能优化信息，提取要点后给出精简建议。",
                "features": "你是AI思考者。请思考文档中的功能特性信息，分析后提供简洁的功能概述。",
                "technical": "你是AI思考者。请思考文档中的技术实现信息，理解后提供简明的技术要点。",
                "usage": "你是AI思考者。请思考文档中的使用方法信息，理解后提供简洁的操作指南。",
                "general": "你是AI思考者。请分析文档内容，提取与问题相关的信息，思考后给出简明答案。",
            }

            system_prompt = type_prompts.get(question_type, type_prompts["general"])

            # 构建详细的用户提示，引导AI思考
            user_prompt_parts = []
            if history_context:
                user_prompt_parts.append(f"对话背景: {history_context}")

            user_prompt_parts.extend(
                [
                    f"用户问题: {question}",
                    "",
                    "==== 相关文档内容 ====",
                    context,
                    "",
                    "==== 思考与回答指南 ====",
                    "1. 首先思考文档中与问题相关的关键信息",
                    "2. 分析这些信息如何回答用户的具体问题",
                    "3. 提炼出最重要的内容要点",
                    "4. 用自己的语言组织一个简洁的回答",
                    "5. 不要直接复制文档内容，要进行总结提炼",
                    "6. 回答应当简明扼要，不超过300字",
                    "7. 确保回答直接针对用户问题",
                    "",
                    "请先思考，然后回答：",
                ]
            )

            user_prompt = "\n".join(user_prompt_parts)

            # 调用LLM生成答案
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ]

            try:
                response = await asyncio.wait_for(
                    self.llm.ainvoke(messages), timeout=15
                )  # 减少超时时间
                answer = response.content.strip()

                # 检查答案长度，过长则再次总结
                if len(answer) > 600:
                    try:
                        summarize_prompt = f"请将以下回答总结为200字以内的简洁版本，保留核心信息:\n\n{answer}"
                        summary_response = await asyncio.wait_for(
                            self.llm.ainvoke([HumanMessage(content=summarize_prompt)]),
                            timeout=8,
                        )
                        answer = summary_response.content.strip()
                    except Exception:
                        # 手动截断过长答案
                        answer = answer[:600] + "..."

                # 验证答案质量
                if len(answer) < 30:
                    raise ValueError("回答过短")

                if self._is_template_answer(answer):
                    raise ValueError("生成了模板回答")

                return answer

            except asyncio.TimeoutError:
                logger.warning("生成答案超时，使用文档摘要回答")
                return self._generate_document_summary_answer(
                    question, docs or [], question_type
                )

        except Exception as e:
            logger.error(f"增强答案生成失败: {e}")
            return await self._force_document_answer(
                question, docs or [], question_type
            )

    def _is_template_answer(self, answer: str) -> bool:
        """检测是否为模板回答"""
        template_indicators = [
            "基于文档内容，这是一个关于",
            "请查看相关文档获取详细信息",
            "抱歉，我找不到",
            "暂时没有找到相关信息",
            "请尝试重新表述",
            "由于主要模型暂时不可用",
        ]

        answer_lower = answer.lower()
        return any(indicator in answer_lower for indicator in template_indicators)

    async def _force_document_answer(
        self, question: str, docs: List[Document], question_type: str
    ) -> str:
        """强制基于文档生成答案，不依赖LLM"""
        if not docs:
            return "抱歉，没有找到相关文档来回答您的问题。"

        # 直接从文档中提取和组织答案
        relevant_content = []

        for doc in docs[:4]:
            content = doc.page_content
            source = doc.metadata.get("filename", "文档") if doc.metadata else "文档"

            # 提取相关段落
            relevant_parts = self._extract_relevant_content_enhanced(
                content, question, question_type
            )
            if relevant_parts:
                relevant_content.append(f"根据{source}：\n{relevant_parts}")

        if relevant_content:
            return (
                f"基于相关文档，针对您关于'{question}'的问题，找到以下信息：\n\n"
                + "\n\n".join(relevant_content)
            )
        else:
            # 最后的备选方案
            return self._generate_document_summary_answer(question, docs, question_type)

    def _generate_document_summary_answer(
        self, question: str, docs: List[Document], question_type: str
    ) -> str:
        """生成基于文档摘要的答案"""
        if not docs:
            return "抱歉，没有找到相关文档。"

        summaries = []
        for doc in docs[:3]:
            content = doc.page_content
            # 提取前几个重要句子
            sentences = [s.strip() for s in content.split("。") if len(s.strip()) > 10]
            if sentences:
                summaries.append(sentences[0] + "。")

        if summaries:
            return f"关于{question}，根据文档内容：\n\n" + "\n".join(
                [f"• {s}" for s in summaries]
            )
        else:
            return "找到了相关文档，但内容提取遇到问题，建议查看源文档获取详细信息。"

    async def _generate_emergency_document_answer(
        self, question: str, docs: List[Document]
    ) -> Dict[str, Any]:
        """紧急情况下基于文档生成答案"""
        if not docs:
            answer = "抱歉，没有找到相关文档来回答您的问题。"
        else:
            answer = await self._force_document_answer(question, docs, "general")

        return {
            "answer": answer,
            "question_type": "general",
            "key_points": [],
            "confidence": 0.3,
            "source_count": len(docs),
        }

    def _classify_question_enhanced(self, question: str) -> str:
        """增强的问题分类，更细致的分类"""
        question_lower = question.lower()

        # 架构和功能相关
        if any(
            word in question_lower
            for word in ["功能", "模块", "组件", "架构", "结构", "系统", "平台"]
        ):
            if any(word in question_lower for word in ["核心", "主要", "重要", "关键"]):
                return "core_architecture"
            return "architecture"

        # 部署和安装
        elif any(
            word in question_lower
            for word in ["部署", "安装", "配置", "搭建", "启动", "运行"]
        ):
            return "deployment"

        # 监控和观察
        elif any(
            word in question_lower
            for word in ["监控", "观察", "检测", "巡检", "指标", "告警"]
        ):
            return "monitoring"

        # 故障和问题
        elif any(
            word in question_lower
            for word in ["故障", "错误", "问题", "异常", "排查", "诊断"]
        ):
            return "troubleshooting"

        # 性能和优化
        elif any(
            word in question_lower for word in ["性能", "优化", "效率", "调优", "速度"]
        ):
            return "performance"

        # 使用和操作
        elif any(
            word in question_lower for word in ["使用", "操作", "怎么", "如何", "方法"]
        ):
            return "usage"

        # 特性和能力
        elif any(
            word in question_lower for word in ["特性", "特点", "能力", "优势", "作用"]
        ):
            return "features"

        # 技术和实现
        elif any(
            word in question_lower for word in ["技术", "实现", "原理", "算法", "框架"]
        ):
            return "technical"

        else:
            return "general"

    def _extract_enhanced_key_points(
        self, answer: str, docs: List[Document]
    ) -> List[str]:
        """提取增强的关键点"""
        if len(answer) < 100:
            return []

        key_points = []

        # 寻找项目符号点或编号列表
        lines = answer.split("\n")
        for line in lines:
            line = line.strip()
            # 检查是否为列表项
            if (
                line.startswith("•")
                or line.startswith("-")
                or line.startswith("*")
                or any(line.startswith(f"{i}.") for i in range(1, 10))
            ):
                if 15 < len(line) < 150:
                    key_points.append(line)

        # 如果没有找到列表，提取重要句子
        if not key_points:
            sentences = [s.strip() for s in answer.split("。") if s.strip()]
            for sentence in sentences:
                if 30 < len(sentence) < 120 and any(
                    keyword in sentence
                    for keyword in [
                        "重要",
                        "关键",
                        "主要",
                        "核心",
                        "包括",
                        "功能",
                        "特性",
                    ]
                ):
                    key_points.append(sentence + "。")
                    if len(key_points) >= 3:
                        break

        return key_points[:3]

    def _calculate_enhanced_confidence(
        self, question: str, answer: str, docs: List[Document]
    ) -> float:
        """计算增强的置信度"""
        try:
            base_score = 0.5

            # 文档数量评分
            doc_score = min(len(docs) / 4, 0.25)

            # 回答长度评分
            length_score = min(len(answer) / 300, 0.2)

            # 内容质量评分
            quality_score = 0.0
            if not self._is_template_answer(answer):
                quality_score += 0.2

            # 关键词匹配评分
            question_words = set(question.lower().split())
            answer_words = set(answer.lower().split())
            overlap = len(question_words & answer_words) / max(len(question_words), 1)
            overlap_score = min(overlap, 0.25)

            total_score = (
                base_score + doc_score + length_score + quality_score + overlap_score
            )
            return min(total_score, 1.0)
        except Exception:
            return 0.5

    def _get_simple_response(
        self, message: str, docs: List[Document]
    ) -> Dict[str, Any]:
        """获取简单响应"""
        return {
            "answer": message,
            "question_type": "general",
            "key_points": [],
            "confidence": 0.3,
            "source_count": len(docs),
        }
