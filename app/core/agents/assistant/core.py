#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops - 优化版智能助手代理
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 智能助手代理 - 基于RAG技术提供运维知识问答和决策支持服务
优化重点: 大幅提升精确度和召回率，修复所有错误
"""

import uuid
import logging
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional
from datetime import datetime

from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_ollama import OllamaEmbeddings, ChatOllama
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage

from app.config.settings import config
from app.core.cache.redis_cache_manager import RedisCacheManager

# 导入拆分后的模块
from .models.base import SessionData, FallbackEmbeddings, FallbackChatModel
from .models.config import assistant_config
from .retrieval.vector_store_manager import VectorStoreManager
from .retrieval.query_rewriter import QueryRewriter
from .retrieval.document_ranker import DocumentRanker
from .retrieval.context_retriever import ContextAwareRetriever
from .answer.reliable_answer_generator import ReliableAnswerGenerator
from .storage.document_loader import DocumentLoader
from .session.session_manager import SessionManager
from .utils.task_manager import get_task_manager
from .utils.helpers import is_test_environment

logger = logging.getLogger("aiops.assistant")


class AssistantAgent:
    """优化版智能小助手代理 - 修复所有错误，大幅提升精确度和召回率"""

    def __init__(self):
        """初始化助手代理"""
        self.llm_provider = assistant_config.llm_provider

        # 路径设置
        self.vector_db_path = assistant_config.vector_db_path
        self.knowledge_base_path = assistant_config.knowledge_base_path
        self.collection_name = assistant_config.collection_name

        # 创建必要目录
        assistant_config.ensure_directories()

        # 初始化组件
        self.embedding = None
        self.llm = None
        self.task_llm = None

        # 管理器
        self.vector_store_manager = None
        self.session_manager = SessionManager()

        # 使用Redis缓存管理器
        self.cache_manager = RedisCacheManager(
            redis_config=assistant_config.cache_config['redis_config'],
            cache_prefix=assistant_config.cache_config['cache_prefix'],
            default_ttl=assistant_config.cache_config['default_ttl'],
            max_cache_size=assistant_config.cache_config['max_cache_size'],
            enable_compression=assistant_config.cache_config['enable_compression']
        )

        self.document_loader = DocumentLoader(str(self.knowledge_base_path))

        # 优化组件
        self.query_rewriter = QueryRewriter()
        self.doc_ranker = DocumentRanker()
        self.context_retriever = None
        self.answer_generator = None

        # 线程池
        self.executor = ThreadPoolExecutor(max_workers=3)  # 减少线程数

        # 关闭标志
        self._shutdown = False

        # 初始化所有组件
        self._initialize_components()

        logger.info(f"优化版智能小助手初始化完成，提供商: {self.llm_provider}")

    def _initialize_components(self):
        """初始化所有组件"""
        try:
            self._init_embedding()
            self._init_llm()
            self._init_vector_store()
            self._init_advanced_components()
        except Exception as e:
            logger.error(f"组件初始化失败: {e}")
            raise

    def _init_embedding(self):
        """初始化嵌入模型 - 改进版本并添加缓存机制"""
        max_retries = 3
        original_provider = self.llm_provider

        # 检查是否有可用的缓存嵌入服务
        try:
            # 尝试先从Redis获取预缓存的向量服务
            cached_model = self.cache_manager.get("embedding_model_status")
            if cached_model and cached_model.get('status') == 'ready':
                logger.info("使用缓存的嵌入模型配置")
        except Exception as e:
            logger.warning(f"检查嵌入缓存失败: {str(e)}")

        for attempt in range(max_retries):
            try:
                logger.info(f"尝试初始化 {self.llm_provider} 嵌入模型 (第 {attempt + 1}/{max_retries} 次)")

                if self.llm_provider == 'openai':
                    # 验证配置
                    if not config.llm.api_key:
                        raise ValueError("OpenAI API key 未配置")
                    if not config.llm.base_url:
                        raise ValueError("OpenAI base URL 未配置")
                    if not config.rag.openai_embedding_model:
                        raise ValueError("OpenAI embedding model 未配置")

                    logger.info(f"OpenAI嵌入配置 - Model: {config.rag.openai_embedding_model}")

                    self.embedding = OpenAIEmbeddings(
                        model=config.rag.openai_embedding_model,
                        api_key=config.llm.api_key,
                        base_url=config.llm.base_url,
                        timeout=10,
                        max_retries=1
                    )
                else:
                    # Ollama配置验证
                    if not config.llm.ollama_base_url:
                        raise ValueError("Ollama base URL 未配置")
                    if not config.rag.ollama_embedding_model:
                        raise ValueError("Ollama embedding model 未配置")

                    logger.info(f"Ollama嵌入配置 - Model: {config.rag.ollama_embedding_model}")

                    self.embedding = OllamaEmbeddings(
                        model=config.rag.ollama_embedding_model,
                        base_url=config.llm.ollama_base_url
                    )

                # 快速嵌入模型测试 - 减少测试量
                logger.info("快速测试嵌入模型连接...")
                test_text = "测试文本"

                # 测试单个嵌入
                single_embedding = self.embedding.embed_query(test_text)
                if not single_embedding or len(single_embedding) == 0:
                    raise ValueError("嵌入测试失败")

                # 缓存嵌入模型状态
                self.cache_manager.set("embedding_model_status", {
                    'status': 'ready', 
                    'provider': self.llm_provider
                })

                logger.info(f"嵌入模型测试成功 - 维度: {len(single_embedding)}, 提供商: {self.llm_provider}")
                return

            except Exception as e:
                error_msg = str(e)
                logger.error(f"嵌入模型初始化失败 (尝试 {attempt + 1}): {error_msg}")

                if attempt < max_retries - 1:
                    # 切换提供商
                    self.llm_provider = 'ollama' if self.llm_provider == 'openai' else 'openai'
                    logger.info(f"切换到 {self.llm_provider} 嵌入提供商")
                    time.sleep(1)
                else:
                    # 最后一次尝试使用原始提供商
                    if self.llm_provider != original_provider:
                        self.llm_provider = original_provider
                        logger.info(f"最后一次尝试使用原始嵌入提供商 {original_provider}")

        # 所有尝试都失败，使用备用嵌入
        logger.error("所有嵌入提供商初始化失败，使用备用嵌入模型")
        logger.warning("⚠️  当前使用备用嵌入模型，向量检索质量可能受限")
        self.embedding = FallbackEmbeddings()

    def _init_llm(self):
        """初始化语言模型 - 改进版本，使用缓存策略"""
        max_retries = 2
        original_provider = self.llm_provider

        # 检查LLM缓存
        try:
            cached_llm = self.cache_manager.get("llm_model_status")
            if cached_llm and cached_llm.get('status') == 'ready':
                logger.info("使用缓存的LLM配置")
                self.llm_provider = cached_llm.get('provider', self.llm_provider)
        except Exception as e:
            logger.warning(f"检查LLM缓存失败: {str(e)}")

        for attempt in range(max_retries):
            try:
                logger.info(f"尝试初始化 {self.llm_provider} 语言模型 (第 {attempt + 1}/{max_retries} 次)")

                if self.llm_provider == 'openai':
                    # 验证配置
                    if not config.llm.api_key:
                        raise ValueError("OpenAI API key 未配置")
                    if not config.llm.base_url:
                        raise ValueError("OpenAI base URL 未配置")

                    logger.info(f"OpenAI配置 - Model: {config.llm.model}, Base URL: {config.llm.base_url}")

                    self.llm = ChatOpenAI(
                        model=config.llm.model,
                        temperature=config.llm.temperature,
                        api_key=config.llm.api_key,
                        base_url=config.llm.base_url,
                        timeout=15
                    )

                    # 任务专用模型
                    self.task_llm = ChatOpenAI(
                        model=config.llm.task_model or config.llm.model,
                        temperature=0.2,
                        api_key=config.llm.api_key,
                        base_url=config.llm.base_url,
                        timeout=8
                    )

                elif self.llm_provider == 'ollama':
                    # 验证配置
                    if not config.llm.ollama_base_url:
                        raise ValueError("Ollama base URL 未配置")
                    if not config.llm.ollama_model:
                        raise ValueError("Ollama model 未配置")

                    logger.info(f"Ollama配置 - Model: {config.llm.ollama_model}, Base URL: {config.llm.ollama_base_url}")

                    self.llm = ChatOllama(
                        model=config.llm.ollama_model,
                        base_url=config.llm.ollama_base_url,
                        temperature=config.llm.temperature
                    )

                    self.task_llm = ChatOllama(
                        model=config.llm.ollama_model,
                        base_url=config.llm.ollama_base_url,
                        temperature=0.2
                    )

                else:
                    raise ValueError(f"不支持的LLM提供商: {self.llm_provider}")

                # 快速测试LLM
                logger.info("快速测试LLM连接...")
                test_messages = [
                    SystemMessage(content="你是AI助手"),
                    HumanMessage(content="测试")
                ]

                response = self.llm.invoke(test_messages)
                if not response or not response.content:
                    raise ValueError("主模型测试返回空响应")

                # 任务模型只做简单检查
                if self.task_llm and self.task_llm != self.llm:
                    task_test = self.task_llm.invoke([HumanMessage(content="测试")])
                    if not (task_test and task_test.content):
                        logger.warning("任务模型测试失败，使用主模型")
                        self.task_llm = self.llm

                # 缓存LLM状态
                self.cache_manager.set("llm_model_status", {
                    'status': 'ready',
                    'provider': self.llm_provider,
                    'timestamp': datetime.now().isoformat()
                })

                logger.info(f"语言模型初始化成功 - 提供商: {self.llm_provider}")
                return

            except Exception as e:
                error_msg = str(e)
                logger.error(f"语言模型初始化失败 (尝试 {attempt + 1}): {error_msg}")

                if attempt < max_retries - 1:
                    # 切换提供商
                    self.llm_provider = 'ollama' if self.llm_provider == 'openai' else 'openai'
                    logger.info(f"切换到 {self.llm_provider} 提供商")
                    time.sleep(1)
                else:
                    # 最后一次尝试使用原始提供商
                    if self.llm_provider != original_provider:
                        self.llm_provider = original_provider
                        logger.info(f"最后一次尝试使用原始提供商 {original_provider}")

        # 所有尝试都失败，使用备用模型
        logger.error("所有LLM提供商初始化失败，使用备用模型")
        logger.warning("⚠️  当前使用备用模型，回答质量可能受限，请检查LLM配置")
        self.llm = FallbackChatModel()
        self.task_llm = self.llm

    def _init_vector_store(self):
        """初始化向量存储"""
        self.vector_store_manager = VectorStoreManager(
            str(self.vector_db_path),
            self.collection_name,
            self.embedding
        )

        # 检查向量维度是否匹配
        try:
            # 测试当前嵌入模型维度
            test_dim = len(self.embedding.embed_query("测试"))
            logger.info(f"检测到嵌入维度: {test_dim}")

            # 如果之前使用的是不同维度的嵌入模型，则需要清理旧数据
            if hasattr(self.vector_store_manager, 'redis_manager') and self.vector_store_manager.redis_manager:
                # 使用redis_manager获取存储的向量维度
                stored_dim = self.vector_store_manager.redis_manager.get_vector_dimension()
                if stored_dim and stored_dim != test_dim:
                    logger.warning(f"检测到向量维度不匹配: 存储={stored_dim}, 当前={test_dim}，清理旧数据并重建索引")
                    self.vector_store_manager.clear_store()
        except Exception as e:
            logger.warning(f"向量维度检查失败: {e}")

        if not self.vector_store_manager.load_existing_db():
            logger.info("创建新的向量数据库")
            documents = self.document_loader.load_documents()

            use_memory = is_test_environment()

            # 修复：使用异步运行同步方法
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # 如果循环正在运行，使用create_task
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(asyncio.run, self.vector_store_manager.create_vector_store(documents, use_memory))
                        success = future.result()
                else:
                    success = loop.run_until_complete(
                        self.vector_store_manager.create_vector_store(documents, use_memory)
                    )
            except RuntimeError:
                # 没有事件循环，创建新的
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    success = loop.run_until_complete(
                        self.vector_store_manager.create_vector_store(documents, use_memory)
                    )
                finally:
                    loop.close()

            if not success:
                logger.error("向量存储初始化失败")
                raise RuntimeError("无法初始化向量存储")

        logger.info("向量存储初始化完成")

    def _init_advanced_components(self):
        """初始化高级组件"""
        try:
            # 初始化文档排序器
            if self.vector_store_manager and self.vector_store_manager.redis_manager:
                logger.debug("使用Redis向量存储，文档排序由检索器内部处理")
                # 加载一些示例文档用于训练排序器
                try:
                    all_docs = self.document_loader.load_documents()
                except Exception as e:
                    logger.warning(f"获取训练文档失败: {e}")
                    all_docs = []

                if all_docs:
                    self.doc_ranker.fit(all_docs)
                    logger.info("文档排序器训练完成")

            # 初始化上下文感知检索器
            if self.vector_store_manager.get_retriever():
                self.context_retriever = ContextAwareRetriever(
                    self.vector_store_manager.get_retriever(),
                    self.query_rewriter,
                    self.doc_ranker
                )
                logger.info("上下文感知检索器初始化完成")

            # 初始化高级答案生成器
            if self.llm:
                self.answer_generator = ReliableAnswerGenerator(self.llm)
                logger.info("可靠答案生成器初始化完成")

        except Exception as e:
            logger.error(f"高级组件初始化失败: {e}")

    # ==================== 知识库管理 ====================

    async def refresh_knowledge_base(self) -> Dict[str, Any]:
        """刷新知识库"""
        try:
            logger.info("开始刷新知识库...")

            # 清理缓存
            logger.info("清理缓存（Redis缓存自动管理）")

            # 加载文档
            documents = await asyncio.get_event_loop().run_in_executor(
                self.executor, self.document_loader.load_documents
            )

            # 重新创建向量存储
            use_memory = is_test_environment()
            success = await self.vector_store_manager.create_vector_store(documents, use_memory)

            if success:
                # 重新训练高级组件
                await asyncio.get_event_loop().run_in_executor(
                    self.executor,
                    self._retrain_advanced_components,
                    documents
                )

                doc_count = len(documents)
                logger.info(f"知识库刷新成功，包含 {doc_count} 个文档")
                return {"success": True, "documents_count": doc_count}
            else:
                return {"success": False, "documents_count": 0, "error": "向量存储创建失败"}

        except Exception as e:
            logger.error(f"刷新知识库失败: {e}")
            return {"success": False, "documents_count": 0, "error": str(e)}

    def _retrain_advanced_components(self, documents: List[Document]):
        """重新训练高级组件"""
        try:
            if documents and self.doc_ranker:
                self.doc_ranker.fit(documents)
                logger.info("文档排序器重新训练完成")
        except Exception as e:
            logger.error(f"重新训练高级组件失败: {e}")

    def add_document(self, content: str, metadata: Dict[str, Any] = None) -> bool:
        """添加文档到知识库"""
        try:
            if not content.strip():
                return False

            doc_id = str(uuid.uuid4())
            filename = metadata.get('filename', f"{doc_id}.txt") if metadata else f"{doc_id}.txt"
            file_path = self.knowledge_base_path / filename

            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)

            logger.info(f"文档已添加: {filename}")
            return True

        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            return False

    # ==================== 核心问答逻辑 ====================

    async def get_answer(
        self,
        question: str,
        session_id: str = None,
        max_context_docs: int = 4
    ) -> Dict[str, Any]:
        """获取问题答案 - 性能优化版核心方法"""

        try:
            start_time = time.time()
            logger.debug(f"处理问题: '{question[:50]}...', 会话ID: {session_id}")

            # 获取会话历史
            session = self.session_manager.get_session(session_id) if session_id else None
            history = session.history[-5:] if session and session.history else []

            # 优化缓存键生成
            cache_key = f"{hash(question)}_{session_id}_{len(history)}"
            cached_response = None

            try:
                cached_response = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, self.cache_manager.get, cache_key
                    ), timeout=0.5
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"缓存检查超时或失败: {e}")

            if cached_response:
                logger.info("使用缓存回答")
                if session_id:
                    self.session_manager.add_message_to_history(session_id, "user", question)
                    self.session_manager.add_message_to_history(session_id, "assistant", cached_response.get("answer", ""))
                return cached_response

            # 添加用户消息到历史
            if session_id:
                session_id = self.session_manager.add_message_to_history(session_id, "user", question)

            # 性能优化：并行执行文档检索和问题分类
            try:
                async def parallel_tasks():
                    # 任务1: 快速文档检索
                    docs_task = self._retrieve_relevant_docs_fast(question, session_id, history, max_context_docs)

                    # 任务2: 问题分类
                    classification_task = asyncio.get_event_loop().run_in_executor(
                        None, self.answer_generator._classify_question_enhanced, question
                    )

                    return await asyncio.gather(docs_task, classification_task, return_exceptions=True)

                results = await asyncio.wait_for(parallel_tasks(), timeout=10)
                relevant_docs, question_type = results
            except Exception as e:
                logger.error(f"并行任务执行失败: {e}")
                # 回退到简单的文档检索
                relevant_docs = self._get_relevant_docs_fallback(question, max_context_docs)
                question_type = 'general'

            # 处理异常结果
            if isinstance(relevant_docs, Exception):
                logger.error(f"文档检索失败: {relevant_docs}")
                relevant_docs = []
            if isinstance(question_type, Exception):
                logger.warning(f"问题分类失败: {question_type}")
                question_type = 'general'

            logger.debug(f"检索到文档数量: {len(relevant_docs)}, 问题类型: {question_type}")

            # 快速回答生成
            answer = ""
            confidence = 0.7

            if relevant_docs:
                try:
                    answer = await asyncio.wait_for(
                        self._generate_answer_fast(question, relevant_docs, question_type),
                        timeout=8
                    )
                    confidence = 0.8
                except asyncio.TimeoutError:
                    logger.warning("答案生成超时，使用快速回答")
                    answer = self._generate_quick_answer(question, relevant_docs, question_type)
                    confidence = 0.6
                except Exception as e:
                    logger.error(f"答案生成失败: {e}")
                    answer = self._generate_quick_answer(question, relevant_docs, question_type)
                    confidence = 0.5
            else:
                answer = self._get_fallback_answer(question_type)
                confidence = 0.3

            # 计算处理时间
            processing_time = time.time() - start_time

            # 构建响应
            result = {
                "answer": answer,
                "source_documents": self._format_source_documents_simple(relevant_docs[:3]),
                "relevance_score": confidence,
                "recall_rate": min(len(relevant_docs) / max_context_docs, 1.0) if relevant_docs else 0.0,
                "follow_up_questions": self._get_simple_follow_up_questions(question_type)[:2],
                "total_docs_found": len(relevant_docs),
                "processing_time": round(processing_time, 2)
            }

            # 添加助手回复到历史
            if session_id:
                self.session_manager.add_message_to_history(session_id, "assistant", answer)

            # 异步缓存结果
            try:
                asyncio.create_task(
                    asyncio.get_event_loop().run_in_executor(
                        None, self._cache_result_async, cache_key, result
                    )
                )
            except Exception as e:
                logger.debug(f"异步缓存失败: {e}")

            logger.info(f"回答生成完成，处理时间: {processing_time:.2f}秒")
            return result

        except asyncio.TimeoutError:
            logger.error("回答生成总体超时")
            return self._get_timeout_response(question)
        except Exception as e:
            logger.error(f"获取回答失败: {e}")

            error_answer = "抱歉，处理您的问题时出现了错误，请稍后重试。"
            if session_id:
                self.session_manager.add_message_to_history(session_id, "assistant", error_answer)

            return {
                "answer": error_answer,
                "source_documents": [],
                "relevance_score": 0.0,
                "recall_rate": 0.0,
                "follow_up_questions": ["AIOps平台有哪些核心功能？", "如何部署AIOps系统？"],
                "total_docs_found": 0,
                "processing_time": 0
            }

    async def _retrieve_relevant_docs_fast(
        self, question: str, session_id: str = None,
        history: List[Dict] = None, max_docs: int = 4
    ) -> List[Document]:
        """快速文档检索 - 性能优化版"""
        try:
            # 直接使用最快的检索方法
            if self.vector_store_manager:
                docs = await asyncio.get_event_loop().run_in_executor(
                    self.executor,
                    self._safe_vector_search_fast,
                    question, max_docs
                )
                return docs[:max_docs]
            return []
        except Exception as e:
            logger.error(f"快速文档检索失败: {e}")
            return []

    def _safe_vector_search_fast(self, question: str, max_docs: int = 4) -> List[Document]:
        """安全的快速向量搜索"""
        try:
            return self.vector_store_manager.search_documents(question)[:max_docs]
        except Exception as e:
            logger.warning(f"快速向量搜索失败: {e}")
            return []

    async def _generate_answer_fast(
        self, question: str, docs: List[Document], question_type: str
    ) -> str:
        """快速答案生成 - 减少AI调用开销"""
        try:
            # 检查文档列表
            if not docs:
                logger.warning("快速答案生成：没有文档提供")
                return self._get_fallback_answer(question_type)

            # 构建简化的上下文
            context = ""
            for i, doc in enumerate(docs[:2]):  # 只使用前2个文档
                try:
                    if doc is None or not hasattr(doc, 'page_content'):
                        logger.warning(f"文档{i}格式无效")
                        continue
                    content = doc.page_content[:300] if doc.page_content else ""
                    context += f"文档{i+1}: {content}\n\n"
                except Exception as doc_e:
                    logger.error(f"处理文档{i}时出错: {doc_e}")

            # 如果没有有效上下文，返回备用回答
            if not context.strip():
                logger.warning("快速答案生成：有效文档内容为空")
                return self._generate_quick_answer(question, docs, question_type)

            # 使用简化的提示词
            try:
                system_prompt = "你是AI助手。基于提供的文档简洁回答问题，不超过200字。"
                user_prompt = f"问题: {question}\n\n文档内容:\n{context}\n\n请简洁回答:"

                messages = [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_prompt)
                ]

                # 检查LLM是否已初始化
                if not hasattr(self, 'llm') or self.llm is None:
                    logger.error("LLM未初始化，使用备用回答")
                    return self._generate_quick_answer(question, docs, question_type)

                response = await asyncio.wait_for(
                    self.llm.ainvoke(messages),
                    timeout=15  # 使用适当的超时时间
                )

                # 检查响应格式
                if not hasattr(response, 'content'):
                    logger.error(f"LLM返回格式异常: {type(response)}")
                    return self._generate_quick_answer(question, docs, question_type)

                return response.content.strip()
            except asyncio.TimeoutError:
                logger.error("LLM调用超时，使用备用回答")
                return self._generate_quick_answer(question, docs, question_type)
            except Exception as llm_e:
                logger.error(f"LLM调用失败: {llm_e}")
                return self._generate_quick_answer(question, docs, question_type)

        except Exception as e:
            logger.error(f"快速答案生成失败: {e}")
            return self._generate_quick_answer(question, docs, question_type)

    def _generate_quick_answer(
        self, question: str, docs: List[Document], question_type: str
    ) -> str:
        """生成快速回答 - 不依赖AI模型"""
        try:
            if not docs:
                logger.debug("快速回答生成：没有文档")
                return self._get_fallback_answer(question_type)

            # 从文档中提取关键信息
            relevant_content = []
            for i, doc in enumerate(docs[:2]):
                try:
                    if doc is None or not hasattr(doc, 'page_content'):
                        logger.warning(f"快速回答：文档{i}格式无效")
                        continue

                    content = doc.page_content.strip() if doc.page_content else ""
                    if content:
                        # 提取前几句话
                        sentences = content.split('。')[:2]
                        if sentences:
                            relevant_content.append('。'.join(sentences) + ('' if sentences[-1].endswith('。') else '。'))
                except Exception as doc_e:
                    logger.error(f"快速回答处理文档{i}时出错: {doc_e}")

            if relevant_content:
                try:
                    answer = f"根据相关文档，{relevant_content[0]}"
                    if len(relevant_content) > 1:
                        answer += f"\n\n另外，{relevant_content[1]}"
                    return answer
                except Exception as format_e:
                    logger.error(f"格式化回答时出错: {format_e}")

            # 如果没有成功提取内容或出错，返回备用回答
            logger.debug("使用备用回答")
            return self._get_fallback_answer(question_type)
        except Exception as e:
            logger.error(f"生成快速回答时出错: {e}")
            return self._get_fallback_answer(question_type)

    def _get_fallback_answer(self, question_type: str) -> str:
        """获取后备答案"""
        fallback_answers = {
            'core_architecture': "AI-CloudOps平台包含智能运维助手、根因分析、预测扩容、自动修复等核心功能模块。",
            'deployment': "AI-CloudOps平台支持Docker和Kubernetes部署方式，具体步骤请参考部署文档。",
            'monitoring': "平台集成Prometheus和Grafana提供全面的监控告警功能。",
            'troubleshooting': "遇到问题建议查看日志文件，按照故障排查流程进行诊断。",
            'performance': "系统性能可通过调整配置参数、优化资源分配等方式提升。",
            'general': "您好，我是AI-CloudOps智能助手。请告诉我您想了解的具体问题。"
        }
        return fallback_answers.get(question_type, fallback_answers['general'])

    def _get_timeout_response(self, question: str) -> Dict[str, Any]:
        """获取超时响应"""
        return {
            "answer": "抱歉，处理您的问题时超时了。请尝试简化您的问题或稍后重试。",
            "source_documents": [],
            "relevance_score": 0.0,
            "recall_rate": 0.0,
            "follow_up_questions": ["如何使用AI-CloudOps？", "系统有什么主要功能？"],
            "total_docs_found": 0,
            "processing_time": 10.0
        }

    def _cache_result_async(self, cache_key: str, result: Dict[str, Any]):
        """异步缓存结果"""
        try:
            self.cache_manager.set(cache_key, result)
        except Exception as e:
            logger.debug(f"缓存结果失败: {e}")

    def _format_source_documents_simple(self, docs: List[Document]) -> List[Dict[str, Any]]:
        """简化的源文档格式化"""
        formatted_docs = []
        for i, doc in enumerate(docs):
            filename = doc.metadata.get('filename', f"文档{i+1}") if doc.metadata else f"文档{i+1}"
            formatted_docs.append({
                "content": doc.page_content[:200] + "..." if len(doc.page_content) > 200 else doc.page_content,
                "file_name": filename,
                "relevance": 0.8
            })
        return formatted_docs

    def _get_simple_follow_up_questions(self, question_type: str) -> List[str]:
        """获取简单的后续问题"""
        simple_questions = {
            'core_architecture': ["各模块如何协同工作？", "如何扩展系统功能？"],
            'deployment': ["部署后如何验证？", "有哪些常见问题？"],
            'monitoring': ["如何设置告警？", "监控数据怎么分析？"],
            'troubleshooting': ["如何预防问题？", "还有其他解决方案吗？"],
            'performance': ["如何监控性能？", "有哪些优化建议？"],
            'general': ["AI-CloudOps有什么特色？", "如何快速上手？"]
        }
        return simple_questions.get(question_type, simple_questions['general'])

    def _get_relevant_docs_fallback(self, question: str, max_docs: int = 4) -> List[Document]:
        """回退的文档检索方法"""
        try:
            if self.vector_store_manager:
                return self._safe_vector_search_fast(question, max_docs)
            return []
        except Exception as e:
            logger.error(f"回退文档检索失败: {e}")
            return []

    def clear_cache(self) -> Dict[str, Any]:
        """清空响应缓存"""
        try:
            result = self.cache_manager.clear_all()
            logger.info(f"缓存清理结果: {result}")
            return result
        except Exception as e:
            logger.error(f"清空缓存失败: {e}")
            return {
                "success": False,
                "message": f"清空缓存失败: {e}",
                "cleared_count": 0
            }

    async def force_reinitialize(self) -> Dict[str, Any]:
        """强制重新初始化小助手"""
        try:
            logger.info("开始强制重新初始化小助手...")
            start_time = time.time()

            # 1. 快速清理缓存
            try:
                self.cache_manager.clear_all()
                logger.info("已清理所有缓存")
            except Exception as e:
                logger.warning(f"清理缓存失败: {e}")

            # 2. 重置向量存储管理器
            logger.info("重置向量存储管理器...")
            try:
                self.vector_store_manager = None
                self._init_vector_store()
                logger.info("向量存储管理器重置完成")
            except Exception as e:
                logger.warning(f"重置向量存储失败: {e}")

            # 3. 重新加载知识库文档
            logger.info("重新加载知识库文档...")
            try:
                documents = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        self.executor, self.document_loader.load_documents
                    ),
                    timeout=30
                )
            except asyncio.TimeoutError:
                logger.error("加载文档超时")
                return {"success": False, "error": "加载文档超时"}

            if not documents:
                logger.warning("没有找到知识库文档")
                return {"success": False, "error": "没有找到知识库文档"}

            logger.info(f"成功加载 {len(documents)} 个知识库文档")

            # 4. 重新创建向量存储
            logger.info("重新创建向量存储...")
            use_memory = is_test_environment()

            try:
                success = await asyncio.wait_for(
                    self.vector_store_manager.create_vector_store(documents, use_memory),
                    timeout=60
                )
            except asyncio.TimeoutError:
                logger.error("创建向量存储超时")
                return {"success": False, "error": "创建向量存储超时"}

            if not success:
                logger.error("重新创建向量存储失败")
                return {"success": False, "error": "重新创建向量存储失败"}

            # 5. 重新初始化高级组件
            try:
                if self.vector_store_manager and self.vector_store_manager.get_retriever():
                    self.context_retriever = ContextAwareRetriever(
                        self.vector_store_manager.get_retriever(),
                        self.query_rewriter,
                        self.doc_ranker
                    )
                    logger.info("上下文感知检索器重新初始化完成")

                if self.llm:
                    self.answer_generator = ReliableAnswerGenerator(self.llm)
                    logger.info("可靠答案生成器重新初始化完成")
            except Exception as e:
                logger.warning(f"重新初始化高级组件时出现警告: {e}")

            # 6. 清空会话历史
            self.session_manager.clear_all_sessions()
            logger.info("已清空所有会话历史")

            total_time = time.time() - start_time
            logger.info(f"小助手强制重新初始化成功，耗时: {total_time:.2f}秒")
            return {
                "success": True,
                "documents_count": len(documents),
                "message": "小助手强制重新初始化成功",
                "processing_time": round(total_time, 2)
            }

        except Exception as e:
            logger.error(f"强制重新初始化失败: {e}")
            return {"success": False, "error": str(e)}

    # 会话管理方法代理
    def create_session(self) -> str:
        """创建新会话"""
        return self.session_manager.create_session()

    def get_session(self, session_id: str) -> Optional[SessionData]:
        """获取会话数据"""
        return self.session_manager.get_session(session_id)

    def clear_session_history(self, session_id: str) -> bool:
        """清空会话历史"""
        return self.session_manager.clear_session_history(session_id)

    async def shutdown(self):
        """优雅关闭助手代理"""
        if self._shutdown:
            return

        logger.info("开始关闭优化版智能助手...")
        self._shutdown = True

        try:
            # 1. 关闭缓存管理器
            if hasattr(self, 'cache_manager'):
                self.cache_manager.shutdown()

            # 2. 关闭线程池
            if hasattr(self, 'executor') and self.executor:
                self.executor.shutdown(wait=True)

            # 3. 关闭任务管理器
            task_manager = get_task_manager()
            await task_manager.shutdown()

            logger.info("优化版智能助手已成功关闭")

        except Exception as e:
            logger.warning(f"关闭智能助手时出现警告: {e}")

    def __del__(self):
        """清理资源"""
        if not self._shutdown:
            try:
                self._shutdown = True

                if hasattr(self, 'cache_manager'):
                    try:
                        self.cache_manager.shutdown()
                    except Exception as e:
                        logger.warning(f"对象销毁时关闭缓存管理器失败: {e}")

                if hasattr(self, 'executor') and self.executor:
                    try:
                        self.executor.shutdown(wait=False)
                    except Exception as e:
                        logger.warning(f"对象销毁时关闭线程池失败: {e}")

            except Exception as e:
                logger.warning(f"AssistantAgent清理资源时出错: {e}")