#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

from dataclasses import dataclass
from typing import Any, Dict, List

import numpy as np
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


@dataclass
class SessionData:
    """会话数据结构"""

    session_id: str
    created_at: str
    history: List[Dict[str, Any]]
    metadata: Dict[str, Any]
    context_summary: str = ""


class FallbackEmbeddings(Embeddings):
    """备用嵌入模型"""

    def __init__(self, dimensions: int = 384):
        self.dimensions = dimensions
        self._cache = {}

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> List[float]:
        if text in self._cache:
            return self._cache[text]

        text_hash = hash(text) % (2**32)
        np.random.seed(text_hash)
        embedding = list(np.random.rand(self.dimensions))
        self._cache[text] = embedding
        return embedding


class FallbackChatModel(BaseChatModel):
    """备用聊天模型"""

    @property
    def _llm_type(self) -> str:
        return "fallback_chat_model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last_message = messages[-1].content if messages else "无输入"

        if "部署" in last_message or "安装" in last_message:
            response = "关于部署问题，建议您查看官方文档或联系技术支持。"
        elif "监控" in last_message:
            response = "关于监控配置，请确保相关服务正常运行并检查配置文件。"
        elif "故障" in last_message or "错误" in last_message:
            response = "遇到故障时，建议先查看日志文件，然后按照标准排查流程进行诊断。"
        else:
            response = f"您询问关于：'{last_message}'。由于主要模型暂时不可用，建议您稍后重试或查看相关文档。"

        message = AIMessage(content=response)
        # 明确忽略 stop/run_manager 参数以保持兼容签名
        _ = (stop, run_manager)
        generation = ChatGeneration(message=message)
        return ChatResult(generations=[generation])
