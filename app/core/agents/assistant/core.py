#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops - 智能助手代理
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 智能助手代理 - 基于RAG技术提供运维知识问答
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.config.settings import config
from app.core.cache.redis_cache_manager import RedisCacheManager

from .answer.reliable_answer_generator import ReliableAnswerGenerator
from .models.base import FallbackChatModel, FallbackEmbeddings, SessionData
from .models.config import assistant_config
from .retrieval.context_retriever import ContextAwareRetriever
from .retrieval.vector_store_manager import VectorStoreManager
from .session.session_manager import SessionManager
from .storage.document_loader import DocumentLoader

logger = logging.getLogger("aiops.assistant")


class AssistantAgent:
    """智能助手代理"""

    def __init__(self):
        """初始化助手代理"""
        self.llm_provider = assistant_config.llm_provider
        self.vector_db_path = assistant_config.vector_db_path
        self.knowledge_base_path = assistant_config.knowledge_base_path
        self.collection_name = assistant_config.collection_name

        assistant_config.ensure_directories()

        # 核心组件
        self.embedding = None
        self.llm = None
        self.vector_store_manager = None
        self.session_manager = SessionManager()
        self.cache_manager = RedisCacheManager(
            redis_config=assistant_config.cache_config["redis_config"],
            cache_prefix=assistant_config.cache_config["cache_prefix"],
            default_ttl=assistant_config.cache_config["default_ttl"]
        )
        self.document_loader = DocumentLoader(str(self.knowledge_base_path))
        self.context_retriever = None
        self.answer_generator = None
        self._shutdown = False

        self._initialize_components()
        logger.info(f"智能助手初始化完成，提供商: {self.llm_provider}")

    def _initialize_components(self):
        """初始化组件"""
        try:
            self._init_embedding()
            self._init_llm()
            self._init_vector_store()
            self._init_advanced_components()
        except Exception as e:
            logger.error(f"组件初始化失败: {e}")
            raise

    def _init_embedding(self):
        """初始化嵌入模型"""
        try:
            if self.llm_provider.lower() == "openai":
                self.embedding = OpenAIEmbeddings(
                    api_key=config.llm.api_key,
                    base_url=config.llm.base_url,
                    model=config.llm.embedding_model or "text-embedding-3-small"
                )
            else:
                self.embedding = OllamaEmbeddings(
                    base_url=config.llm.ollama_base_url,
                    model=config.llm.embedding_model or "nomic-embed-text"
                )
            logger.info(f"嵌入模型初始化成功: {self.llm_provider}")
        except Exception as e:
            logger.error(f"嵌入模型初始化失败: {e}")
            self.embedding = FallbackEmbeddings()

    def _init_llm(self):
        """初始化语言模型"""
        try:
            if self.llm_provider.lower() == "openai":
                self.llm = ChatOpenAI(
                    api_key=config.llm.api_key,
                    base_url=config.llm.base_url,
                    model=config.llm.model,
                    temperature=0.1,
                    max_tokens=2000
                )
            else:
                self.llm = ChatOllama(
                    base_url=config.llm.ollama_base_url,
                    model=config.llm.ollama_model,
                    temperature=0.1,
                    num_ctx=4096
                )
            logger.info(f"语言模型初始化成功: {self.llm_provider}")
        except Exception as e:
            logger.error(f"语言模型初始化失败: {e}")
            self.llm = FallbackChatModel()

    def _init_vector_store(self):
        """初始化向量存储"""
        try:
            self.vector_store_manager = VectorStoreManager(
                self.vector_db_path,
                self.embedding,
                self.collection_name
            )
            logger.info("向量存储初始化成功")
        except Exception as e:
            logger.error(f"向量存储初始化失败: {e}")
            raise

    def _init_advanced_components(self):
        """初始化高级组件"""
        try:
            # 构建高级检索组件
            from .retrieval.document_ranker import DocumentRanker
            from .retrieval.query_rewriter import QueryRewriter
            self.context_retriever = ContextAwareRetriever(
                base_retriever=self.vector_store_manager,
                query_rewriter=QueryRewriter(),
                doc_ranker=DocumentRanker(),
            )
            self.answer_generator = ReliableAnswerGenerator(self.llm)
            logger.info("高级组件初始化成功")
        except Exception as e:
            logger.error(f"高级组件初始化失败: {e}")
            raise

    async def refresh_knowledge_base(self) -> Dict[str, Any]:
        """刷新知识库"""
        try:
            start_time = time.time()
            documents = self.document_loader.load_documents()
            
            if not documents:
                return {"status": "warning", "message": "未找到文档"}

            await self.vector_store_manager.add_documents(documents)
            self._retrain_advanced_components(documents)
            
            duration = time.time() - start_time
            return {
                "status": "success",
                "message": f"知识库刷新完成，处理 {len(documents)} 个文档",
                "duration": f"{duration:.2f}s",
                "document_count": len(documents)
            }
        except Exception as e:
            logger.error(f"刷新知识库失败: {e}")
            return {"status": "error", "message": f"刷新失败: {str(e)}"}

    def _retrain_advanced_components(self, documents: List[Document]):
        """重新训练高级组件"""
        try:
            if self.context_retriever:
                self.context_retriever.update_knowledge(documents)
        except Exception as e:
            logger.warning(f"重新训练高级组件失败: {e}")

    def add_document(self, content: str, metadata: Dict[str, Any] = None) -> bool:
        """添加单个文档"""
        try:
            doc = Document(page_content=content, metadata=metadata or {})
            asyncio.run(self.vector_store_manager.add_documents([doc]))
            return True
        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            return False

    async def get_answer(
        self, question: str, session_id: str = None, max_context_docs: int = 4
    ) -> Dict[str, Any]:
        """获取问题答案"""
        try:
            start_time = time.time()
            logger.debug(f"处理问题: {question[:50]}...")

            # 检查缓存
            # 获取会话历史以构建完整缓存键
            history = self.session_manager.get_history(session_id) if session_id else []
            cached_response = self.cache_manager.get(question, session_id, history)
            if cached_response:
                logger.info("使用缓存回答")
                return cached_response

            # 会话管理
            if session_id:
                self.session_manager.add_message_to_history(session_id, "user", question)

            # 检索相关文档
            relevant_docs = await self._retrieve_relevant_docs(question, max_context_docs)
            
            # 生成答案
            answer_result = await self._generate_answer(question, relevant_docs)
            
            # 构建响应
            result = {
                "answer": answer_result.get("answer", "抱歉，我无法为您提供准确答案。"),
                "sources": self._format_sources(relevant_docs),
                "confidence": answer_result.get("confidence", 0.5),
                "processing_time": f"{time.time() - start_time:.2f}s"
            }

            # 保存到会话和缓存
            if session_id:
                self.session_manager.add_message_to_history(session_id, "assistant", result["answer"])
                updated_history = self.session_manager.get_history(session_id)
                self.cache_manager.set(question, result, session_id, updated_history, ttl=3600)

            return result
        except Exception as e:
            logger.error(f"获取答案失败: {e}")
            return {
                "answer": "抱歉，处理您的问题时出现了错误。",
                "sources": [],
                "confidence": 0.0,
                "error": str(e)
            }

    async def _retrieve_relevant_docs(self, question: str, max_docs: int = 4) -> List[Document]:
        """检索相关文档"""
        try:
            if self.context_retriever:
                return await self.context_retriever.retrieve_relevant_docs(question, max_docs)
            else:
                return await self.vector_store_manager.similarity_search(question, k=max_docs)
        except Exception as e:
            logger.error(f"文档检索失败: {e}")
            return []

    async def _generate_answer(self, question: str, docs: List[Document]) -> Dict[str, Any]:
        """生成答案"""
        try:
            if self.answer_generator:
                return await self.answer_generator.generate_structured_answer(question, docs)
            else:
                return await self._simple_generate_answer(question, docs)
        except Exception as e:
            logger.error(f"答案生成失败: {e}")
            return {"answer": "抱歉，生成答案时出现错误。", "confidence": 0.0}

    async def _simple_generate_answer(self, question: str, docs: List[Document]) -> Dict[str, Any]:
        """简单答案生成"""
        try:
            context = "\n\n".join([doc.page_content for doc in docs[:3]])
            
            messages = [
                SystemMessage(content="你是一个专业的运维助手，基于提供的文档回答问题。"),
                HumanMessage(content=f"基于以下文档内容回答问题：\n\n{context}\n\n问题：{question}")
            ]
            
            response = await self.llm.ainvoke(messages)
            return {
                "answer": response.content,
                "confidence": 0.8 if docs else 0.3
            }
        except Exception as e:
            logger.error(f"简单答案生成失败: {e}")
            return {"answer": "抱歉，无法生成答案。", "confidence": 0.0}

    def _format_sources(self, docs: List[Document]) -> List[Dict[str, Any]]:
        """格式化文档来源"""
        sources = []
        for doc in docs[:3]:
            sources.append({
                "content": doc.page_content[:200] + "..." if len(doc.page_content) > 200 else doc.page_content,
                "metadata": doc.metadata
            })
        return sources

    def clear_cache(self) -> Dict[str, Any]:
        """清空缓存"""
        try:
            self.cache_manager.clear()
            return {"status": "success", "message": "缓存已清空"}
        except Exception as e:
            logger.error(f"清空缓存失败: {e}")
            return {"status": "error", "message": f"清空缓存失败: {str(e)}"}

    async def force_reinitialize(self) -> Dict[str, Any]:
        """强制重新初始化"""
        try:
            self._shutdown = True
            await asyncio.sleep(0.1)
            
            self._initialize_components()
            self._shutdown = False
            
            return {"status": "success", "message": "重新初始化完成"}
        except Exception as e:
            logger.error(f"重新初始化失败: {e}")
            return {"status": "error", "message": f"重新初始化失败: {str(e)}"}

    def create_session(self) -> str:
        """创建会话"""
        return self.session_manager.create_session()

    def get_session(self, session_id: str) -> Optional[SessionData]:
        """获取会话"""
        return self.session_manager.get_session(session_id)

    def clear_session_history(self, session_id: str) -> bool:
        """清空会话历史"""
        return self.session_manager.clear_history(session_id)

    async def shutdown(self):
        """关闭助手"""
        self._shutdown = True
        try:
            if hasattr(self, 'cache_manager'):
                self.cache_manager.shutdown()
            logger.info("助手代理已关闭")
        except Exception as e:
            logger.error(f"关闭助手时出错: {e}")


class AssistantCore:
    def __init__(self):
        self.agent = AssistantAgent()

    async def process_query(self, query: str, session_id: Optional[str] = None, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # 简化：调用现有代理获取答案
        result = await self.agent.get_answer(query, session_id=session_id)
        return {
            "response": result.get("answer") if isinstance(result, dict) else result,
            "confidence": result.get("confidence", 0.8) if isinstance(result, dict) else 0.8,
            "sources": result.get("sources", []) if isinstance(result, dict) else [],
        }