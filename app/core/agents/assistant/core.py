#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
import asyncio
import logging
import math
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langgraph.graph import END, START, StateGraph

from app.config.settings import config
from app.core.cache.redis_cache_manager import RedisCacheManager

from .models.base import FallbackChatModel, FallbackEmbeddings, SessionData
from .models.config import assistant_config
from .retrieval.vector_store_manager import VectorStoreManager
from .session.session_manager import SessionManager
from .storage.document_loader import DocumentLoader

logger = logging.getLogger("aiops.assistant")


class AssistantState(TypedDict, total=False):
    session_id: Optional[str]
    question: str
    history: List[Dict[str, Any]]
    normalized_question: str
    intent: str
    safe: bool
    queries: List[str]
    retrieved_docs: List[Document]
    reranked: List[Tuple[Document, float]]
    context_docs: List[Document]
    draft_answer: str
    confidence: float
    telemetry: Dict[str, Any]
    iter_count: int


def _now_ms() -> int:
    return int(time.time() * 1000)


class AssistantAgent:
    """智能助手代理（LangGraph实现）"""

    def __init__(self):
        self.llm_provider = assistant_config.llm_provider
        self.vector_db_path = assistant_config.vector_db_path
        self.knowledge_base_path = assistant_config.knowledge_base_path
        self.collection_name = assistant_config.collection_name

        assistant_config.ensure_directories()

        # 组件
        self.embedding = None
        self.llm = None
        self.vector_store_manager = None
        self.session_manager = SessionManager()
        self.cache_manager = RedisCacheManager(
            redis_config=assistant_config.cache_config["redis_config"],
            cache_prefix=assistant_config.cache_config["cache_prefix"],
            default_ttl=assistant_config.cache_config["default_ttl"],
        )
        self.document_loader = DocumentLoader(str(self.knowledge_base_path))

        self._shutdown = False

        # 初始化底层能力
        self._init_embedding()
        self._init_llm()
        self._init_vector_store()

        # LangGraph
        # 从配置读取可调参数
        self.retrieve_k = int(getattr(config.rag, "retrieve_k", 12))
        self.score_threshold = float(getattr(config.rag, "score_threshold", 0.0))
        self.iter_max_loops = int(getattr(config.rag, "iter_max_loops", 1))
        self.retry_confidence_threshold = float(getattr(config.rag, "retry_confidence_threshold", 0.6))
        self.mmr_top_k = int(getattr(config.rag, "mmr_top_k", 6))
        self.mmr_lambda = float(getattr(config.rag, "mmr_lambda", 0.7))
        self.answer_max_chars = int(getattr(config.rag, "answer_max_chars", 300))
        self.source_limit = int(getattr(config.rag, "source_limit", 4))
        # 新增：最大改写查询条数限制，降低并发检索开销
        try:
            self.max_rewrite_queries = int(getattr(config.rag, "max_rewrite_queries", 6))
        except Exception:
            self.max_rewrite_queries = 6

        self.graph = self._build_graph()
        logger.info(f"智能小助手(LangGraph)初始化完成，提供商: {self.llm_provider}")

    # --------------------------- 基础初始化 --------------------------- #

    def _init_embedding(self):
        try:
            if self.llm_provider.lower() == "openai":
                self.embedding = OpenAIEmbeddings(
                    api_key=config.llm.api_key,
                    base_url=config.llm.base_url,
                    model=config.llm.embedding_model or "text-embedding-3-small",
                )
            else:
                self.embedding = OllamaEmbeddings(
                    base_url=config.llm.ollama_base_url,
                    model=config.llm.embedding_model or "nomic-embed-text",
                )
            logger.info("嵌入模型初始化成功")
        except Exception as e:
            logger.error(f"嵌入模型初始化失败: {e}")
            self.embedding = FallbackEmbeddings()

    def _init_llm(self):
        try:
            if self.llm_provider.lower() == "openai":
                # 将最大输出长度与全局配置对齐，并设一个合理上限以降低延迟
                safe_max_tokens = max(256, min(int(getattr(config.llm, "max_tokens", 2048)), 800))
                self.llm = ChatOpenAI(
                    api_key=config.llm.api_key,
                    base_url=config.llm.base_url,
                    model=config.llm.model,
                    temperature=0.1,
                    max_tokens=safe_max_tokens,
                )
            else:
                self.llm = ChatOllama(
                    base_url=config.llm.ollama_base_url,
                    model=config.llm.ollama_model,
                    temperature=0.1,
                    num_ctx=4096,
                )
            logger.info("语言模型初始化成功")
        except Exception as e:
            logger.error(f"语言模型初始化失败: {e}")
            self.llm = FallbackChatModel()

    def _init_vector_store(self):
        try:
            self.vector_store_manager = VectorStoreManager(
                self.vector_db_path,
                self.collection_name,
                self.embedding,
            )
            loaded = self.vector_store_manager.load_existing_db()
            if not loaded:
                documents = self.document_loader.load_documents()
                if documents:
                    logger.info("首次运行：构建向量库...")
                    # 异步添加
                    try:
                        asyncio.run(self.vector_store_manager.add_documents(documents))
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        loop.run_until_complete(self.vector_store_manager.add_documents(documents))
                        loop.close()
                    _ = self.vector_store_manager.load_existing_db()
                else:
                    logger.warning("知识库为空，RAG召回可能为空。")
        except Exception as e:
            logger.error(f"向量存储初始化失败: {e}")
            raise

    # --------------------------- LangGraph 构建 --------------------------- #

    def _build_graph(self):
        graph = StateGraph(AssistantState)

        graph.add_node("normalize", self._node_normalize)
        graph.add_node("intent_safety", self._node_intent_safety)
        graph.add_node("rewrite", self._node_rewrite)
        graph.add_node("retrieve", self._node_retrieve)
        graph.add_node("rerank", self._node_rerank)
        graph.add_node("compress", self._node_compress)
        graph.add_node("synthesize", self._node_synthesize)
        graph.add_node("grounding", self._node_grounding)
        graph.add_node("calibrate", self._node_calibrate)
        graph.add_node("cache_write", self._node_cache_write)

        graph.add_edge(START, "normalize")
        graph.add_edge("normalize", "intent_safety")
        graph.add_edge("intent_safety", "rewrite")
        graph.add_edge("rewrite", "retrieve")
        graph.add_edge("retrieve", "rerank")
        graph.add_edge("rerank", "compress")
        graph.add_edge("compress", "synthesize")
        graph.add_edge("synthesize", "grounding")
        graph.add_edge("grounding", "calibrate")

        def _need_iterate(state: AssistantState) -> str:
            # 允许一次自我迭代
            iter_count = int(state.get("iter_count") or 0)
            confidence = float(state.get("confidence") or 0.0)
            if confidence < self.retry_confidence_threshold and iter_count < self.iter_max_loops:
                return "rewrite"
            return "cache_write"

        graph.add_conditional_edges("calibrate", _need_iterate, {"rewrite": "rewrite", "cache_write": "cache_write"})
        graph.add_edge("cache_write", END)

        return graph.compile()

    # --------------------------- LangGraph 节点实现 --------------------------- #

    async def _node_normalize(self, state: AssistantState) -> AssistantState:
        q = (state.get("question") or "").strip()
        q = " ".join(q.split())
        state["normalized_question"] = q
        # 初始化
        state.setdefault("telemetry", {})
        state["telemetry"]["t0"] = _now_ms()
        logger.info(f"trace={state['telemetry'].get('trace_id')} node=normalize q_len={len(q)}")
        state["iter_count"] = int(state.get("iter_count") or 0)
        return state

    async def _node_intent_safety(self, state: AssistantState) -> AssistantState:
        q = state.get("normalized_question") or ""
        ql = q.lower()
        intent = "general"
        if any(k in q for k in ["部署", "安装", "配置", "启动"]):
            intent = "deployment"
        elif any(k in q for k in ["监控", "告警", "指标", "tracing", "prometheus"]):
            intent = "monitoring"
        elif any(k in q for k in ["故障", "错误", "异常", "排查", "诊断", "crashloop", "oom"]):
            intent = "troubleshooting"
        elif any(k in ql for k in ["architecture", "架构", "overview", "概览", "平台", "系统"]):
            intent = "overview"

        # 轻量安全：屏蔽明显PII/注入
        unsafe_tokens = ["password=", "secret", "令牌", "token=", "DROP TABLE", ";--"]
        safe = not any(tok.lower() in ql for tok in unsafe_tokens)

        state["intent"] = intent
        state["safe"] = safe
        logger.info(
            f"trace={state.get('telemetry', {}).get('trace_id')} node=intent_safety intent={intent} safe={safe}"
        )
        return state

    async def _node_rewrite(self, state: AssistantState) -> AssistantState:
        q = state.get("normalized_question") or ""
        base = [q]
        # 同义词扩展（简化版）
        synonyms = {
            "部署": ["安装", "配置", "搭建", "deploy"],
            "监控": ["观察", "检测", "monitor"],
            "故障": ["异常", "错误", "incident"],
            "性能": ["优化", "效率", "performance"],
            "平台": ["系统", "产品", "platform"],
        }
        for key, syns in synonyms.items():
            if key in q:
                for s in syns[:3]:
                    base.append(q.replace(key, s))

        # 关键词拆分组合
        tokens = [t for t in q.replace("/", " ").replace("-", " ").split() if len(t) > 1]
        if len(tokens) >= 2:
            base.append(" ".join(tokens[:2]))
        if len(tokens) >= 3:
            base.append(" ".join(tokens[-2:]))

        # 平台聚焦
        if any(k in q for k in ["平台", "系统", "概览", "overview", "AI-CloudOps", "AIOps"]):
            base.extend([
                f"{q} 平台架构 组件 模块",
                f"{q} 核心能力 特性 功能",
            ])

        # 去重截断
        # 限制改写查询数量
        limit_n = int(getattr(self, "max_rewrite_queries", 6))
        queries = list(dict.fromkeys(base))[: max(1, limit_n)]
        state["queries"] = queries
        state["iter_count"] = int(state.get("iter_count") or 0) + 1 if state.get("confidence") and state["confidence"] < 0.6 else int(state.get("iter_count") or 0)
        logger.info(
            f"trace={state.get('telemetry', {}).get('trace_id')} node=rewrite queries={len(queries)} iter_count={state['iter_count']}"
        )
        return state

    async def _node_retrieve(self, state: AssistantState) -> AssistantState:
        queries = state.get("queries") or [state.get("normalized_question") or ""]
        retriever = self.vector_store_manager.get_retriever(k=self.retrieve_k, score_threshold=self.score_threshold)

        all_docs: List[Document] = []
        async def _inv(q: str) -> List[Document]:
            try:
                res = retriever.invoke(q)
                return await res if hasattr(res, "__await__") else res
            except Exception as e:
                logger.warning(f"检索失败: {e}")
                return []

        logger.info(
            f"trace={state.get('telemetry', {}).get('trace_id')} node=retrieve start queries={len(queries[:6])}"
        )
        # 并发检索，最多使用 max_rewrite_queries 条
        results = await asyncio.gather(*[_inv(q) for q in queries[: int(getattr(self, "max_rewrite_queries", 6))]])
        for docs in results:
            if docs:
                all_docs.extend(docs)

        # 可选 Hybrid：关键词检索补充
        try:
            if bool(getattr(config.rag, "hybrid_enabled", False)):
                kw_docs = await self.vector_store_manager.keyword_search(state.get("normalized_question") or "", k=max(4, self.retrieve_k // 2))
                all_docs.extend(kw_docs or [])
        except Exception as e:
            logger.debug(f"Hybrid关键词检索失败: {e}")

        # 去重（基于内容前80字符）
        seen = set()
        unique_docs = []
        for d in all_docs:
            h = hash(d.page_content[:80])
            if h not in seen:
                seen.add(h)
                unique_docs.append(d)

        state["retrieved_docs"] = unique_docs
        logger.info(
            f"trace={state.get('telemetry', {}).get('trace_id')} node=retrieve end retrieved_total={len(all_docs)} unique_docs={len(unique_docs)}"
        )
        return state

    async def _node_rerank(self, state: AssistantState) -> AssistantState:
        docs = state.get("retrieved_docs") or []
        q = state.get("normalized_question") or ""
        if not docs:
            state["reranked"] = []
            return state

        # 轻量 TF-IDF + 关键词重叠评分
        # reranker 开关（预留：可接入 cross-encoder）
        use_reranker = bool(getattr(config.rag, "reranker_enabled", False))
        if not use_reranker:
            try:
                from sklearn.feature_extraction.text import TfidfVectorizer
                from sklearn.metrics.pairwise import cosine_similarity

                corpus = [d.page_content for d in docs]
                vectorizer = TfidfVectorizer(max_features=800, ngram_range=(1, 2))
                doc_vecs = vectorizer.fit_transform(corpus)
                q_vec = vectorizer.transform([q])
                sims = cosine_similarity(q_vec, doc_vecs).flatten()

                q_tokens = set(q.lower().split())
                scored: List[Tuple[Document, float]] = []
                for i, d in enumerate(docs):
                    text = d.page_content.lower()
                    overlap = sum(1 for t in list(q_tokens)[:5] if t in text) / max(len(q_tokens) or 1, 1)
                    length_score = 0.6 if len(d.page_content) < 150 else 1.0
                    meta_bonus = 1.0
                    if d.metadata:
                        name = (d.metadata.get("filename") or d.metadata.get("source") or "").lower()
                        for kw in ["aiops", "ai-cloudops", "平台", "overview", "架构"]:
                            if kw in name:
                                meta_bonus += 0.05
                    score = sims[i] * 0.55 + overlap * 0.25 + length_score * 0.1 + meta_bonus * 0.1
                    scored.append((d, float(score)))

                scored.sort(key=lambda x: x[1], reverse=True)
                state["reranked"] = scored[: min(int(getattr(config.rag, "reranker_top_k", 20)), len(scored))]
            except Exception:
                state["reranked"] = [(d, 0.6) for d in docs]
        else:
            # 预留：当接入 cross-encoder 时在此实现模型打分
            state["reranked"] = [(d, 0.7) for d in docs]
        logger.info(
            f"trace={state.get('telemetry', {}).get('trace_id')} node=rerank inputs={len(docs)} outputs={len(state.get('reranked') or [])}"
        )
        return state

    async def _node_compress(self, state: AssistantState) -> AssistantState:
        ranked = state.get("reranked") or []
        if not ranked:
            state["context_docs"] = []
            return state

        # 简化版MMR选择前N=6
        def mmr_select(cands: List[Tuple[Document, float]], top_k: int = self.mmr_top_k, lambda_coeff: float = self.mmr_lambda) -> List[Document]:
            selected: List[Document] = []
            selected_vecs: List[str] = []
            remains = cands.copy()
            while remains and len(selected) < top_k:
                best_i = -1
                best_score = -math.inf
                for i, (d, s) in enumerate(remains):
                    # 句级粗糙相似（Jaccard over tokens）
                    d_tokens = set(d.page_content.lower().split())
                    redundancy = 0.0
                    for sv in selected_vecs:
                        st = set(sv.split())
                        inter = len(d_tokens & st)
                        union = len(d_tokens | st) or 1
                        redundancy = max(redundancy, inter / union)
                    mmr = lambda_coeff * s - (1 - lambda_coeff) * redundancy
                    if mmr > best_score:
                        best_score = mmr
                        best_i = i
                if best_i >= 0:
                    d, _ = remains.pop(best_i)
                    selected.append(d)
                    selected_vecs.append(" ".join(d.page_content.lower().split()[:200]))
                else:
                    break
            return selected

        state["context_docs"] = mmr_select(ranked)
        logger.info(
            f"trace={state.get('telemetry', {}).get('trace_id')} node=compress selected={len(state['context_docs'])}"
        )
        return state

    async def _node_synthesize(self, state: AssistantState) -> AssistantState:
        docs = state.get("context_docs") or []
        q = state.get("normalized_question") or state.get("question") or ""
        if not docs:
            # 空召回时的最佳实践简答
            state["draft_answer"] = await self._best_practice_fallback(q)
            state["confidence"] = 0.55
            logger.info(
                f"trace={state.get('telemetry', {}).get('trace_id')} node=synthesize no_docs_fallback"
            )
            return state

        context_blocks = []
        for i, d in enumerate(docs[:6]):
            name = d.metadata.get("filename", f"Doc{i+1}") if d.metadata else f"Doc{i+1}"
            snippet = d.page_content
            if len(snippet) > 1200:
                snippet = snippet[:1200] + "..."
            context_blocks.append(f"[{name}]\n{snippet}")

        system_prompt = "你是资深SRE/DevOps助手，基于提供的文档作答，必须引用来源并保持简洁。"
        # 平台聚焦追加
        if any(k in q for k in ["平台", "系统", "产品", "概览", "overview", "AI-CloudOps", "AIOps"]):
            system_prompt += " 聚焦AI-CloudOps/AIOps平台，用简洁段落，先概述再要点。"

        user_prompt = "\n".join(
            [
                f"问题: {q}",
                "",
                "==== 相关文档 ====",
                "\n\n".join(context_blocks),
                "",
                "要求:",
                f"- 回答不超过{self.answer_max_chars}字，条理清晰",
                "- 引用格式 [filename]，合适时合并多个来源",
                "- 仅基于文档事实作答，避免臆测",
            ]
        )

        try:
            logger.info(
                f"trace={state.get('telemetry', {}).get('trace_id')} node=synthesize start docs={len(docs)}"
            )
            resp = await self.llm.ainvoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ])
            answer = (resp.content or "").strip()
            state["draft_answer"] = answer
            logger.info(
                f"trace={state.get('telemetry', {}).get('trace_id')} node=synthesize end answer_len={len(answer)}"
            )
        except Exception as e:
            logger.warning(f"LLM生成失败，降级: {e}")
            state["draft_answer"] = self._extract_summary_from_docs(q, docs)
            logger.info(
                f"trace={state.get('telemetry', {}).get('trace_id')} node=synthesize degraded answer_len={len(state['draft_answer'])}"
            )
        return state

    async def _node_grounding(self, state: AssistantState) -> AssistantState:
        # 简单归因覆盖率估计：答案中命中的 [filename] 数
        ans = state.get("draft_answer") or ""
        docs = state.get("context_docs") or []
        names = []
        for i, d in enumerate(docs[:6]):
            names.append(d.metadata.get("filename", f"Doc{i+1}") if d.metadata else f"Doc{i+1}")
        coverage = sum(1 for n in set(names) if f"[{n}]" in ans)
        state.setdefault("telemetry", {})["grounding_sources"] = coverage
        logger.info(
            f"trace={state.get('telemetry', {}).get('trace_id')} node=grounding sources_covered={coverage}"
        )
        return state

    async def _node_calibrate(self, state: AssistantState) -> AssistantState:
        ans = state.get("draft_answer") or ""
        ranked = state.get("reranked") or []
        docs = state.get("context_docs") or []
        coverage = int(state.get("telemetry", {}).get("grounding_sources", 0))

        base = 0.5
        has_docs = 0.15 if docs else -0.1
        top_score = float(ranked[0][1]) if ranked else 0.0
        score_term = min(max(top_score, 0.0), 1.0) * 0.2
        len_term = min(len(ans) / 350.0, 1.0) * 0.1
        cov_term = min(coverage / max(len(docs), 1), 1.0) * 0.15
        final = max(0.0, min(base + has_docs + score_term + len_term + cov_term, 1.0))
        state["confidence"] = float(f"{final:.3f}")
        logger.info(
            f"trace={state.get('telemetry', {}).get('trace_id')} node=calibrate confidence={state['confidence']} coverage={coverage} docs={len(docs)}"
        )
        return state

    async def _node_cache_write(self, state: AssistantState) -> AssistantState:
        # 写缓存由上层统一封装，这里仅留痕迹
        logger.info(
            f"trace={state.get('telemetry', {}).get('trace_id')} node=cache_write"
        )
        return state

    # --------------------------- 对外API（保持不变） --------------------------- #

    async def refresh_knowledge_base(self) -> Dict[str, Any]:
        try:
            t0 = time.time()
            documents = self.document_loader.load_documents()
            if not documents:
                return {"status": "warning", "message": "未找到文档"}
            await self.vector_store_manager.add_documents(documents)
            return {
                "status": "success",
                "message": f"知识库刷新完成，处理 {len(documents)} 个文档",
                "duration": f"{time.time() - t0:.2f}s",
                "document_count": len(documents),
            }
        except Exception as e:
            logger.error(f"刷新知识库失败: {e}")
            return {"status": "error", "message": f"刷新失败: {str(e)}"}

    def add_document(self, content: str, metadata: Dict[str, Any] = None) -> bool:
        try:
            doc = Document(page_content=content, metadata=metadata or {})

            async def _add():
                return await self.vector_store_manager.add_documents([doc])

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(_add())

            result_holder = {"ok": False}

            def _worker():
                try:
                    result_holder["ok"] = asyncio.run(_add())
                except Exception as ex:
                    logger.error(f"添加文档线程失败: {ex}")

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join(timeout=30)
            return bool(result_holder["ok"])
        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            return False

    async def add_document_async(self, content: str, metadata: Dict[str, Any] = None) -> bool:
        try:
            doc = Document(page_content=content, metadata=metadata or {})
            return await self.vector_store_manager.add_documents([doc])
        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            return False

    async def get_answer(self, question: str, session_id: str = None, max_context_docs: int = 4) -> Dict[str, Any]:
        try:
            t0 = time.time()
            trace_id = str(uuid.uuid4())
            logger.info(
                f"trace={trace_id} pipeline_start session={session_id} q_len={len(question or '')}"
            )
            history = self.session_manager.get_history(session_id) if session_id else []

            # 先查缓存
            cached_response = self.cache_manager.get(question, session_id, history)
            if cached_response:
                logger.info(f"trace={trace_id} cache_hit session={session_id}")
                return cached_response

            # 会话记忆
            if session_id:
                self.session_manager.add_message_to_history(session_id, "user", question)

            init_state: AssistantState = {
                "session_id": session_id,
                "question": question,
                "history": history,
                "iter_count": 0,
                "telemetry": {"trace_id": trace_id, "t0": _now_ms()},
            }
            final_state: AssistantState = await self.graph.ainvoke(init_state)

            docs = final_state.get("context_docs") or []
            # 限制输出的来源数量
            sources = []
            for d in docs[: min(max_context_docs, self.source_limit)]:
                sources.append({
                    "content": (d.page_content[:200] + "...") if len(d.page_content) > 200 else d.page_content,
                    "metadata": d.metadata,
                })

            result = {
                "answer": final_state.get("draft_answer") or "抱歉，我无法为您提供准确答案。",
                "sources": sources,
                "confidence": float(final_state.get("confidence") or 0.5),
                "processing_time": f"{time.time() - t0:.2f}s",
            }

            # 写缓存 + 会话历史
            if session_id:
                self.session_manager.add_message_to_history(session_id, "assistant", result["answer"])
                updated_history = self.session_manager.get_history(session_id)
                self.cache_manager.set(question, result, session_id, updated_history, ttl=3600)

            logger.info(
                f"trace={trace_id} pipeline_end session={session_id} confidence={result['confidence']} sources={len(sources)} duration={result['processing_time']}"
            )
            return result
        except Exception as e:
            logger.error(f"获取答案失败 trace={locals().get('trace_id', 'n/a')}: {e}")
            return {
                "answer": "抱歉，处理您的问题时出现了错误。",
                "sources": [],
                "confidence": 0.0,
                "error": str(e),
            }

    def _extract_summary_from_docs(self, question: str, docs: List[Document]) -> str:
        if not docs:
            return "抱歉，没有找到相关文档。"
        sentences = []
        for d in docs[:3]:
            parts = [s.strip() for s in d.page_content.split("。") if len(s.strip()) > 10]
            if parts:
                sentences.append(parts[0] + "。")
        if sentences:
            return f"关于{question}，根据文档：\n- " + "\n- ".join(sentences)
        return "找到了相关文档，但内容提取遇到问题。"

    async def _best_practice_fallback(self, question: str) -> str:
        steps = []
        ql = question.lower()
        if any(k in ql for k in ["crashloop", "crashloopbackoff", "重启", "反复重启"]):
            steps.extend([
                "查看事件: kubectl describe pod <pod> -n <ns>",
                "查看容器日志: kubectl logs <pod> -n <ns> --previous",
                "检查探针与资源限制",
            ])
        if any(k in ql for k in ["oom", "内存", "memory"]):
            steps.extend(["确认 OOMKilled 事件", "提高内存 requests/limits 或优化内存占用"])
        if any(k in ql for k in ["cpu", "高负载", "load"]):
            steps.extend(["kubectl top pod 观察CPU", "分析热点或增加副本", "合理设置HPA"])
        if not steps:
            steps = [
                "先看事件时间线: kubectl get events -n <ns> --sort-by=.lastTimestamp",
                "describe 目标对象，确认状态与最近变更",
                "查看应用日志与依赖连通性",
            ]
        try:
            resp = await self.llm.ainvoke([
                SystemMessage(content="你是资深SRE/DevOps助理。没有知识库时给出精简、可执行的排查步骤，200字内。"),
                HumanMessage(content=f"问题: {question}\n\n建议:\n- " + "\n- ".join(steps)),
            ])
            return (resp.content or "").strip()
        except Exception:
            return "建议:\n- " + "\n- ".join(steps)

    def clear_cache(self) -> Dict[str, Any]:
        try:
            return self.cache_manager.clear()
        except Exception as e:
            logger.error(f"清空缓存失败: {e}")
            return {"status": "error", "message": f"清空缓存失败: {str(e)}"}

    async def force_reinitialize(self) -> Dict[str, Any]:
        try:
            self._shutdown = True
            await asyncio.sleep(0.1)
            self._init_embedding()
            self._init_llm()
            self._init_vector_store()
            self.graph = self._build_graph()
            self._shutdown = False
            return {"status": "success", "message": "重新初始化完成"}
        except Exception as e:
            logger.error(f"重新初始化失败: {e}")
            return {"status": "error", "message": f"重新初始化失败: {str(e)}"}

    def create_session(self) -> str:
        return self.session_manager.create_session()

    def get_session(self, session_id: str) -> Optional[SessionData]:
        return self.session_manager.get_session(session_id)

    def clear_session_history(self, session_id: str) -> bool:
        return self.session_manager.clear_history(session_id)

    async def shutdown(self):
        self._shutdown = True
        try:
            if hasattr(self, "cache_manager"):
                self.cache_manager.shutdown()
            logger.info("助手代理已关闭")
        except Exception as e:
            logger.error(f"关闭助手时出错: {e}")
