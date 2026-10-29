#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps智能助手服务
"""

import asyncio
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Dict, Optional, Type, Union
import uuid

from pydantic import BaseModel
from starlette.websockets import WebSocket

from ..common.exceptions import AIOpsException, AssistantError, KnowledgeRefreshError, KnowledgeWriteError, ServiceUnavailableError, SessionAccessError, ValidationError
from ..common.identity import conversation_context, require_principal
from ..config.settings import config
from ..core.agents.enterprise_assistant import get_enterprise_assistant
from ..models import AssistantRequest
from ..models.assistant_models import StreamResponse
from .assistant_session_redis import MAX_ITEM_BYTES, RedisAssistantSessions
from .assistant_sessions import AssistantSessions, OwnedSession
from .base import BaseService
from .factory import ServiceFactory

if TYPE_CHECKING:
    from .mcp_service import MCPService

logger = logging.getLogger("aiops.services.assistant")
_LEASE_HEARTBEAT_SECONDS = 5


class OptimizedAssistantService(BaseService):
    """助手服务"""

    MAX_KNOWLEDGE_FILE_SIZE = 10 * 1024 * 1024

    def __init__(self, sessions: AssistantSessions | RedisAssistantSessions | None = None) -> None:
        super().__init__("assistant")
        self._assistant = None
        self._sessions = sessions if sessions is not None else RedisAssistantSessions()
        if isinstance(self._sessions, AssistantSessions):
            self._sessions.on_expire = self._remove_session_state
        self._performance_monitor = PerformanceMonitor()
        self.websocket = WebSocketManager(self.handle_ws_answer, AssistantRequest)

    async def handle_ws_answer(self, websocket: WebSocket, req: AssistantRequest):
        """WebSocket 消息处理适配器"""
        try:
            result = await self.get_answer(
                question=req.question,
                mode=req.mode,
                session_id=req.session_id,
                turn_id=req.turn_id,
            )
            await websocket.send_json(
                StreamResponse(
                    data=result,
                    success=True,
                    timestamp=datetime.now().isoformat(),
                ).dict()
            )

        except SessionAccessError as error:
            await websocket.send_json(
                StreamResponse(
                    success=False,
                    data={"code": error.status_code},
                    timestamp=datetime.now().isoformat(),
                    message=error.message,
                ).model_dump()
            )
            await websocket.close(code=1008)
        except Exception:
            await websocket.send_json(
                StreamResponse(
                    success=False,
                    timestamp=datetime.now().isoformat(),
                    message="请求处理失败，请稍后重试",
                ).dict()
            )

    async def _do_initialize(self) -> None:
        if config.llm.provider.lower() == "openai" and (not config.llm.api_key or config.llm.api_key == "sk-xxx"):
            raise ServiceUnavailableError("assistant")
        from app.services.llm import LLMService

        self._assistant = await get_enterprise_assistant(llm_client=LLMService())
        if self._assistant is None:
            raise ServiceUnavailableError("assistant")

    async def _do_health_check(self) -> bool:
        if self._assistant is None:
            return False
        try:
            if isinstance(self._sessions, RedisAssistantSessions) and not await self._sessions.health_check():
                return False
            return (await self._assistant.health_check()).get("status") == "healthy"
        except Exception:
            return False

    async def get_answer(
        self,
        question: str,
        mode: int = 1,
        session_id: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """获取回答"""
        require_principal()
        self._validate_question(question)
        self._validate_mode(mode)
        if isinstance(self._sessions, RedisAssistantSessions):
            return await self._get_durable_answer(question, mode, session_id, turn_id)
        created = not session_id
        if created:
            session_id = self._sessions.create(mode)
        session = self._sessions.get(session_id)
        try:
            async with session.turn():
                token = conversation_context.set((session.owner, session_id, session.revision))
                try:
                    if mode == 2:
                        result = await self._handle_mcp_mode(question, session_id)
                    else:
                        result = await self._handle_rag_mode(question, session_id)
                    session.revision += 1
                    session.message_count += 2
                    session.mode = mode
                    session.last_activity = datetime.now(timezone.utc).isoformat()
                    session.last_access = self._sessions.clock()
                    return result
                finally:
                    conversation_context.reset(token)
        except (Exception, asyncio.CancelledError):
            if created:
                self._sessions.discard(session_id)
            raise

    async def _get_durable_answer(self, question: str, mode: int, session_id: Optional[str], turn_id: Optional[str]) -> Dict[str, Any]:
        owner = require_principal()
        store = self._sessions
        assert isinstance(store, RedisAssistantSessions)
        try:
            turn_id = str(uuid.uuid4()) if turn_id is None else str(uuid.UUID(turn_id))
        except (ValueError, TypeError, AttributeError):
            raise SessionAccessError(400, "turn_id 必须为 UUID") from None
        if len(question.encode()) > MAX_ITEM_BYTES:
            raise SessionAccessError(400, "问题内容超过安全上限")
        sid = session_id or await store.create(mode)
        request_hash = hashlib.sha256(json.dumps({"question": question, "mode": mode}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        # Ownership is checked before initializing RAG/MCP or making any call.
        token, revision, history, replay = await store.begin(sid, turn_id, request_hash, question, mode)
        if replay is not None:
            return replay
        assert token is not None
        context_token = conversation_context.set((owner, sid, revision))
        call_task = None
        heartbeat = None
        lost = asyncio.Event()

        async def keep_lease() -> None:
            try:
                while True:
                    await asyncio.sleep(_LEASE_HEARTBEAT_SECONDS)
                    await store.renew(sid, token)
            except asyncio.CancelledError:
                raise
            except Exception:
                lost.set()
                if call_task is not None:
                    call_task.cancel()

        async def invoke() -> Dict[str, Any]:
            await self._restore_durable_history(mode, sid, owner, history)
            if lost.is_set():
                raise SessionAccessError(503, "会话租约失效，结果未知")
            return await (self._handle_mcp_mode(question, sid) if mode == 2 else self._handle_rag_mode(question, sid))

        try:
            call_task = asyncio.create_task(invoke())
            heartbeat = asyncio.create_task(keep_lease())
            result = await call_task
            if lost.is_set():
                raise SessionAccessError(503, "会话租约失效，结果未知")
            answer = result.get("answer")
            if not isinstance(answer, str) or not answer:
                raise AssistantError("助手未返回有效答案")
            result["turn_id"] = turn_id
            await store.complete(sid, token, revision, history, question, answer, mode, turn_id, result)
            return result
        except BaseException as error:
            if call_task is not None and not call_task.done():
                call_task.cancel()
                await asyncio.gather(call_task, return_exceptions=True)
            try:
                await asyncio.shield(store.unknown(sid, token))
            except BaseException:
                # The durable running lease will become unknown when inspected.
                pass
            if isinstance(error, asyncio.CancelledError):
                if lost.is_set():
                    raise SessionAccessError(503, "会话租约失效，结果未知") from None
                raise
            if isinstance(error, SessionAccessError):
                raise
            raise SessionAccessError(502, f"会话 {sid} 的请求结果未知，请先查询会话状态") from error
        finally:
            try:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
            finally:
                try:
                    self._clear_durable_local_history(sid, owner)
                except Exception as cleanup_error:
                    logger.error("清理本机会话状态失败，异常类型: %s", type(cleanup_error).__name__)
                finally:
                    conversation_context.reset(context_token)

    async def _restore_durable_history(self, mode: int, sid: str, owner: str, history: list[dict[str, str]]) -> None:
        if mode == 1:
            await self._ensure_ready()
            context = getattr(self._assistant, "context_manager", None)
            if context is None or not hasattr(context, "session_contexts"):
                raise SessionAccessError(503, "RAG 会话历史恢复不可用")
            entries = []
            for turn in history:
                entries.extend([{"type": "user", "content": turn["q"]}, {"type": "assistant", "content": turn["a"]}])
            now = datetime.now(timezone.utc)
            context.session_contexts[sid] = {
                "created_at": now,
                "last_activity": now,
                "query_count": len(history),
                "message_count": len(entries),
                "recent_queries": deque((turn["q"] for turn in history), maxlen=10),
                "conversation_history": entries,
                "user_preferences": [],
                "domain": None,
                "mode": mode,
                "status": "active",
            }
            from langgraph.checkpoint.memory import MemorySaver

            checkpoint = getattr(getattr(self._assistant, "graph", None), "checkpointer", None)
            if isinstance(checkpoint, MemorySaver):
                checkpoint.delete_thread(f"{owner}:{sid}")
        else:
            mcp = await self._get_mcp_service()
            await mcp.initialize()
            await mcp._ensure_ready()
            assistant = getattr(mcp, "_mcp_assistant", None)
            if assistant is None or not isinstance(getattr(assistant, "_history", None), dict):
                raise SessionAccessError(503, "MCP 会话历史恢复不可用")
            assistant._history[f"{owner}:{sid}"] = [{"q": turn["q"], "a": turn["a"]} for turn in history]

    def _clear_durable_local_history(self, sid: str, owner: str) -> None:
        self._remove_session_state(sid, OwnedSession(owner=owner, mode=1, last_access=0))

    async def refresh_knowledge_base(self) -> Dict[str, Any]:
        self._ensure_initialized()

        try:
            result = await self._assistant.refresh_knowledge_base()
            if not result.get("success"):
                if result.get("outcome_unknown") is True or result.get("published") is True:
                    raise KnowledgeRefreshError(
                        published=None if result.get("outcome_unknown") is True else True,
                        outcome_unknown=result.get("outcome_unknown") is True,
                    )
                raise AssistantError("知识库刷新未完成，请检查失败文件与索引服务")
            self._performance_monitor.reset()

            return {
                "refreshed": result.get("success", False),
                "message": result.get("message", "知识库刷新完成"),
                "timestamp": datetime.now().isoformat(),
            }

        except AIOpsException:
            raise
        except Exception as e:
            logger.error("刷新知识库失败，异常类型: %s", type(e).__name__)
            raise AssistantError("刷新失败") from e

    async def clear_cache(self) -> Dict[str, Any]:
        try:
            await self._ensure_ready()

            cache_cleared = False
            cleared_items = 0
            if self._assistant and hasattr(self._assistant, "clear_cache"):
                result = await self._assistant.clear_cache()
                cache_cleared = result.get("success", False)
                cleared_items = result.get("cleared_items", 0)
            else:
                self._performance_monitor.reset()
                cache_cleared = True

            return {
                "cleared": cache_cleared,
                "cache_keys_cleared": cleared_items,
                "message": "缓存清除完成" if cache_cleared else "缓存清除部分完成",
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as e:
            logger.error("清除缓存失败，异常类型: %s", type(e).__name__)
            self._performance_monitor.reset()
            return {
                "cleared": False,
                "cache_keys_cleared": 0,
                "message": "缓存清除失败",
                "timestamp": datetime.now().isoformat(),
            }

    async def create_session(self, request) -> Dict[str, Any]:
        """创建明确绑定可信主体的会话，无需调用模型"""
        self._validate_mode(request.mode)
        session_id = await self._sessions.create(request.mode) if isinstance(self._sessions, RedisAssistantSessions) else self._sessions.create(request.mode)
        return {
            "session_id": session_id,
            "created": True,
            "message": "会话创建成功",
            "timestamp": datetime.now().isoformat(),
        }

    async def create_or_get_session(self, user_id: str, mode: int = 1) -> Dict[str, Any]:
        """旧内部入口保留，但 user_id 必须与已认证网关主体一致"""
        principal = require_principal()
        if principal != f"user:{user_id}":
            raise SessionAccessError(403, "不能为其他身份创建或读取会话")
        self._validate_mode(mode)
        if isinstance(self._sessions, RedisAssistantSessions):
            session_id, created = await self._sessions.create_or_get(mode)
            info = await self._sessions.info(session_id)
            return {**info, "created": created, "timestamp": datetime.now().isoformat()}
        key = (principal, mode)
        session_id = self._sessions.user_sessions.get(key)
        created = session_id is None
        if session_id is None:
            session_id = self._sessions.create(mode)
            self._sessions.user_sessions[key] = session_id
        info = self._sessions.info(session_id)
        return {**info, "created": created, "timestamp": datetime.now().isoformat()}

    async def get_session_info(self, session_id: str) -> Dict[str, Any]:
        """只读取当前主体拥有的会话，未知或旧无归属会话不自动认领"""
        return await self._sessions.info(session_id) if isinstance(self._sessions, RedisAssistantSessions) else self._sessions.info(session_id)

    async def get_service_health_info(self) -> Dict[str, Any]:
        """获取详细健康信息"""
        try:
            health = await self._assistant.health_check() if self._assistant else {}

            # 检查备用实现的可用性
            fallback_status = self._check_fallback_availability()

            # 添加性能指标
            perf_stats = self._performance_monitor.get_stats()

            # 合并组件状态和备用状态
            components = health.get("components", {})
            components.update(fallback_status)

            return {
                "service": "optimized_assistant",
                "status": ("healthy" if health.get("status") == "healthy" else "degraded"),
                "components": components,
                "performance": perf_stats,
                "fallback_capabilities": fallback_status,
                "timestamp": datetime.now().isoformat(),
            }

        except Exception:
            return {
                "service": "optimized_assistant",
                "status": "unhealthy",
                "error": "健康检查失败",
                "timestamp": datetime.now().isoformat(),
            }

    def _check_fallback_availability(self) -> Dict[str, bool]:
        """检查备用实现的可用性"""
        fallback_status = {
            "fallback_chat_model": False,
            "fallback_embeddings": False,
            "session_manager": False,
            "response_templates": False,
        }

        try:
            # 检查备用聊天模型
            from app.core.agents.fallback_models import FallbackChatModel

            FallbackChatModel()
            fallback_status["fallback_chat_model"] = True
        except Exception:
            pass

        try:
            # 检查备用嵌入模型
            from app.core.agents.fallback_models import FallbackEmbeddings

            FallbackEmbeddings()
            fallback_status["fallback_embeddings"] = True
        except Exception:
            pass

        try:
            # 检查会话管理器
            from app.core.agents.fallback_models import SessionManager

            SessionManager()
            fallback_status["session_manager"] = True
        except Exception:
            pass

        try:
            # 检查响应模板管理器
            from app.core.agents.fallback_models import ResponseTemplateManager

            ResponseTemplateManager()
            fallback_status["response_templates"] = True
        except Exception:
            pass

        return fallback_status

    @staticmethod
    async def get_assistant_config() -> Dict[str, Any]:
        """获取配置信息"""
        return {
            "service_type": "optimized_rag",
            "workflow_engine": "langgraph",
            "features": {
                "query_expansion": True,
                "document_reranking": True,
                "hybrid_search": True,
                "intelligent_caching": True,
                "performance_monitoring": True,
            },
            "model_config": {
                "provider": config.llm.provider,
                "model": config.llm.model,
                "temperature": config.rag.temperature,
                "max_tokens": config.llm.max_tokens,
            },
            "retrieval_config": {
                "strategy": "hybrid",
                "semantic_weight": config.rag.similarity_threshold + 0.1,
                "lexical_weight": 1.0 - (config.rag.similarity_threshold + 0.1),
                "similarity_threshold": config.rag.similarity_threshold,
                "max_candidates": config.rag.max_docs_per_query * 2,
                "rerank_top_k": config.rag.top_k,
            },
            "cache_config": {
                "enabled": True,
                "ttl": config.rag.cache_expiry,
                "min_confidence": config.rag.similarity_threshold,
            },
            "performance_targets": {
                "p50_latency_ms": 500,  # 目标P50延迟
                "p95_latency_ms": 2000,  # 目标P95延迟
                "p99_latency_ms": 5000,  # 目标P99延迟
                "target_accuracy": config.rag.similarity_threshold + 0.15,
            },
        }

    def _validate_question(self, question: str) -> None:
        """验证问题"""
        if not question or not isinstance(question, str):
            raise ValidationError("question", "问题不能为空")

        question = question.strip()
        if not question:
            raise ValidationError("question", "问题内容不能为空")

        if len(question) > config.rag.max_context_length:
            raise ValidationError("question", f"问题长度不能超过{config.rag.max_context_length}字符")

    async def _ensure_ready(self) -> None:
        await self.initialize()
        if self._assistant is None:
            raise ServiceUnavailableError("assistant")

    def _calculate_timeout(self, question: str) -> float:
        """计算超时时间"""
        # 使用配置文件中的超时时间作为基础超时
        base_timeout = float(config.rag.timeout)

        # 根据问题长度调整
        question_length_divisor = 100
        max_length_factor = 3.0
        length_factor = min(len(question) / question_length_divisor, max_length_factor)

        # 根据历史性能调整
        perf_stats = self._performance_monitor.get_stats()
        if perf_stats and "avg_latency" in perf_stats:
            avg_latency = perf_stats["avg_latency"]
            latency_divisor = 1000
            performance_multiplier = 2
            min_performance_factor = 1.0
            performance_factor = max(
                avg_latency / latency_divisor * performance_multiplier,
                min_performance_factor,
            )
        else:
            performance_factor = 1.0

        timeout = base_timeout * (1 + length_factor * 0.2) * performance_factor
        # 使用配置文件中的超时时间作为最大值
        return min(timeout, base_timeout * 2)  # 最大为配置超时时间的2倍

    def _enhance_result(self, result: Dict[str, Any], question: str, session_id: Optional[str]) -> Dict[str, Any]:
        """补充返回结果"""
        enhanced = result.copy()

        # 添加元数据
        enhanced.update(
            {
                "question": question,
                "session_id": session_id,
                "timestamp": datetime.now().isoformat(),
                "service_version": config.app.version if hasattr(config, "app") else "1.0.0",
            }
        )

        # 确保必要字段
        enhanced.setdefault("answer", "暂无答案")
        enhanced.setdefault("confidence_score", 0.0)
        enhanced.setdefault("source_documents", [])
        enhanced.setdefault("success", True)

        # 添加性能指标
        if "processing_time" in enhanced:
            enhanced["performance"] = {
                "latency_ms": enhanced["processing_time"] * 1000,  # 转换为毫秒
                "cache_hit": enhanced.get("cache_hit", False),
            }

        return enhanced

    async def _handle_rag_mode(self, question: str, session_id: Optional[str]) -> Dict[str, Any]:
        """处理检索模式请求"""
        # 确保服务就绪
        await self._ensure_ready()

        # 记录性能
        with self._performance_monitor.measure("get_answer_rag"):
            try:
                # 计算超时时间
                timeout = self._calculate_timeout(question)

                # 调用服务接口
                result = await asyncio.wait_for(
                    self._assistant.get_answer(question=question, session_id=session_id),
                    timeout=timeout,
                )

                if not result.get("success", True):
                    raise AssistantError("RAG处理失败，请检查知识检索与模型服务")
                # 记录成功指标
                self._performance_monitor.record_success()

                # 增强结果
                enhanced_result = self._enhance_result(result, question, session_id)
                enhanced_result["mode"] = "rag"  # 标记模式
                return enhanced_result

            except asyncio.TimeoutError as e:
                self._performance_monitor.record_failure()
                raise AssistantError("RAG请求超时") from e
            except AssistantError:
                self._performance_monitor.record_failure()
                raise
            except Exception as e:
                self._performance_monitor.record_failure()
                logger.error("RAG获取答案失败，异常类型: %s", type(e).__name__)
                raise AssistantError("RAG调用失败") from e

    async def _handle_mcp_mode(self, question: str, session_id: Optional[str]) -> Dict[str, Any]:
        """处理工具协议模式请求"""
        try:
            # 通过工厂延迟获取服务实例，避免循环依赖
            mcp_service = await self._get_mcp_service()

            # 调用服务处理
            result = await mcp_service.get_answer(question=question, session_id=session_id)

            # 记录成功指标
            self._performance_monitor.record_success()

            # 增强结果，保持返回结构一致
            enhanced_result = self._enhance_result(result, question, session_id)
            enhanced_result["mode"] = "mcp"
            return enhanced_result

        except AssistantError:
            self._performance_monitor.record_failure()
            raise
        except Exception as e:
            self._performance_monitor.record_failure()
            logger.error("MCP获取答案失败，异常类型: %s", type(e).__name__)
            raise AssistantError("MCP调用失败") from e

    async def _get_mcp_service(self) -> "MCPService":
        """兼容旧接口：通过工厂获取MCP服务单例"""
        from .mcp_service import MCPService

        return await ServiceFactory.get_service("mcp", MCPService)

    def _remove_session_state(self, session_id: str, session: OwnedSession) -> None:
        if self._assistant is not None:
            context = getattr(self._assistant, "context_manager", None)
            if context is not None:
                context.session_contexts.pop(session_id, None)
            from langgraph.checkpoint.memory import MemorySaver

            checkpoint = getattr(getattr(self._assistant, "graph", None), "checkpointer", None)
            if isinstance(checkpoint, MemorySaver):
                checkpoint.delete_thread(f"{session.owner}:{session_id}")
        mcp = ServiceFactory.peek("mcp")
        assistant = getattr(mcp, "_mcp_assistant", None) if mcp else None
        if assistant is not None:
            assistant._history.pop(f"{session.owner}:{session_id}", None)

    async def cleanup(self) -> None:
        if isinstance(self._sessions, RedisAssistantSessions):
            await self._sessions.close()
        else:
            self._sessions.clear()
        await super().cleanup()

    def _validate_mode(self, mode: int) -> None:
        """验证模式参数"""
        if not isinstance(mode, int) or mode not in [1, 2]:
            raise ValidationError("mode", "模式参数必须是1(RAG)或2(MCP)")

    async def get_service_health_info_with_mode(self) -> Dict[str, Any]:
        """获取包含两种模式的详细健康信息"""
        try:
            # 获取检索模式健康信息
            rag_health = await self.get_service_health_info()

            # 获取工具模式健康信息
            mcp_health = {"status": "unavailable"}
            try:
                mcp_service = await self._get_mcp_service()
                mcp_health = await mcp_service.get_service_health_info()
            except Exception as e:
                logger.debug("获取MCP健康信息失败，异常类型: %s", type(e).__name__)
                mcp_health = {"status": "unavailable", "error": "健康检查失败"}

            return {
                "service": "assistant_service_unified",
                "modes": {"rag": rag_health, "mcp": mcp_health},
                "supported_modes": [
                    {
                        "mode": 1,
                        "name": "RAG",
                        "description": "基于知识库的检索增强生成",
                    },
                    {
                        "mode": 2,
                        "name": "MCP",
                        "description": "基于工具调用的模型上下文协议",
                    },
                ],
                "timestamp": datetime.now().isoformat(),
            }

        except Exception:
            return {
                "service": "assistant_service_unified",
                "status": "unhealthy",
                "error": "健康检查失败",
                "timestamp": datetime.now().isoformat(),
            }

    async def upload_knowledge_file(self, file) -> Dict[str, Any]:
        require_principal()
        try:
            if not file:
                raise ValidationError("file", "文件不能为空")
            original_filename = file.filename
            self._resolve_filename_conflict(config.rag.knowledge_base_path, original_filename)
            file_extension = original_filename.rsplit(".", 1)[-1].lower()
            if file_extension not in {"md", "txt"}:
                raise ValidationError("file", "仅支持 UTF-8 编码的 .md 和 .txt 文件")

            max_file_size = self.MAX_KNOWLEDGE_FILE_SIZE
            content = await file.read(max_file_size + 1)
            if len(content) > max_file_size:
                raise ValidationError("file", f"文件大小不能超过{max_file_size // (1024 * 1024)}MB")
            try:
                file_content = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ValidationError("file", "文件编码不支持，请使用UTF-8编码的文本文件")
            if not file_content.strip() or "\x00" in file_content:
                raise ValidationError("file", "文件内容不能为空或包含空字符")

            # 无效上传不得初始化模型与外部依赖
            await self._ensure_ready()

            # 获取知识库根路径
            knowledge_base_path = config.rag.knowledge_base_path

            # 确保知识库目录存在
            os.makedirs(knowledge_base_path, exist_ok=True)

            final_filename, file_path = self._save_knowledge_file(knowledge_base_path, original_filename, file_content)

            # 生成文档ID
            import hashlib

            document_id = hashlib.md5(f"{final_filename}{file_content}".encode()).hexdigest()

            logger.info("知识库文件已保存，大小: %d 字节", len(content))

            await self._index_saved_document(
                file_content,
                {
                    "title": os.path.splitext(final_filename)[0],
                    "source": "user_upload",
                    "category": "general",
                    "filename": final_filename,
                    "file_path": file_path,
                    "upload_time": datetime.now().isoformat(),
                },
            )

            return {
                "uploaded": True,
                "document_id": document_id,
                "filename": final_filename,
                "file_size": len(content),
                "message": f"文件上传成功，已保存为: {final_filename}",
                "timestamp": datetime.now().isoformat(),
                "file_path": file_path,
            }

        except AIOpsException:
            raise
        except Exception as e:
            logger.error("上传文件失败，异常类型: %s", type(e).__name__)
            raise AssistantError("文件上传失败") from e

    async def _index_saved_document(self, content: str, metadata: Dict[str, Any]) -> None:
        """文件保留用于恢复，但未成功索引时不报告知识添加完成"""
        if self._assistant is None:
            raise AssistantError("文件已保存，但助手索引尚未就绪")
        result = await self._assistant.upload_knowledge(
            {
                "content": content,
                "metadata": metadata,
                "title": metadata["title"],
                "source": metadata["source"],
            }
        )
        if not result.get("success") or result.get("document_count", 0) <= 0:
            if result.get("outcome_unknown") is True or result.get("published") is True:
                raise KnowledgeWriteError(
                    published=None if result.get("outcome_unknown") is True else True,
                    outcome_unknown=result.get("outcome_unknown") is True,
                )
            raise AssistantError("文件已保存，但知识索引未完成，请检查索引服务后刷新知识库")

    def _resolve_filename_conflict(self, directory: str, filename: str) -> str:
        """解决文件名冲突，如果文件已存在则添加UUID"""
        if not isinstance(filename, str) or not filename.strip() or filename in {".", ".."}:
            raise ValidationError("file_name", "文件名不能为空")
        if os.path.basename(filename) != filename or "\\" in filename or any(ord(character) < 32 or ord(character) == 127 for character in filename):
            raise ValidationError("file_name", "文件名不能包含路径或控制字符")
        try:
            filename_size = len(filename.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValidationError("file_name", "文件名必须使用有效UTF-8字符")
        if filename_size > 255:
            raise ValidationError("file_name", "文件名不能超过255字节")
        candidate = os.path.join(directory, filename)
        if os.path.islink(candidate):
            raise ValidationError("file_name", "不允许写入符号链接")
        if not os.path.exists(candidate):
            return filename

        # 分离文件名和扩展名
        name_part, ext_part = os.path.splitext(filename)

        # 生成唯一文件名
        unique_id = str(uuid.uuid4())
        new_filename = f"{name_part}_{unique_id}{ext_part}"

        return new_filename

    def _save_knowledge_file(self, directory: str, filename: str, content: str):
        """在固定目录描述符中完整写入，再原子发布，绝不覆盖已有文件"""
        import stat

        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None or not hasattr(os, "O_DIRECTORY"):
            raise RuntimeError("当前平台不支持安全文件创建")
        from app.utils.knowledge_directory import open_knowledge_directory, verify_knowledge_directory

        directory_fd, resolved = open_knowledge_directory(directory)
        temporary = f".cloudops-upload-{uuid.uuid4().hex}"
        file_fd = None
        owned = None
        linked = False
        try:
            file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600, dir_fd=directory_fd)
            owned = os.fstat(file_fd)
            remaining = memoryview(content.encode("utf-8"))
            while remaining:
                written = os.write(file_fd, remaining)
                if written <= 0:
                    raise OSError("文件写入未完成")
                remaining = remaining[written:]
            os.fsync(file_fd)
            name, ext = os.path.splitext(filename)
            for attempt in range(32):
                if attempt == 0:
                    candidate = filename
                else:
                    suffix = "_" + uuid.uuid4().hex
                    budget = 255 - len(suffix.encode()) - len(ext.encode("utf-8"))
                    candidate = name.encode("utf-8")[:budget].decode("utf-8", "ignore") + suffix + ext
                try:
                    os.link(temporary, candidate, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
                    linked = True
                    break
                except FileExistsError:
                    try:
                        existing = os.stat(candidate, dir_fd=directory_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISLNK(existing.st_mode):
                        raise ValidationError("file_name", "不允许写入符号链接") from None
            else:
                raise RuntimeError("无法生成唯一文件名")
            actual = os.stat(candidate, dir_fd=directory_fd, follow_symlinks=False)
            if (actual.st_dev, actual.st_ino) != (owned.st_dev, owned.st_ino) or not stat.S_ISREG(actual.st_mode):
                raise RuntimeError("文件在创建时发生变化")
            verify_knowledge_directory(directory, resolved, directory_fd)
            os.fsync(directory_fd)
            return candidate, os.path.join(directory, candidate)
        except Exception as error:
            if linked:
                # 已发布的文件可能被其他流程观察，不能将结果报告为安全重试
                raise KnowledgeWriteError(published=False, outcome_unknown=True, file_saved=None) from error
            raise
        finally:
            cleanup_error = None
            try:
                if file_fd is not None:
                    os.close(file_fd)
                if owned is not None:
                    try:
                        current = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                            os.unlink(temporary, dir_fd=directory_fd)
                            os.fsync(directory_fd)
                    except FileNotFoundError:
                        pass
            except OSError as error:
                cleanup_error = error
            finally:
                os.close(directory_fd)
            if cleanup_error is not None:
                if linked:
                    raise KnowledgeWriteError(published=False, outcome_unknown=True, file_saved=True) from cleanup_error
                raise RuntimeError("暂存文件清理失败") from cleanup_error

    async def add_document(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        require_principal()
        try:
            # 验证payload
            if not isinstance(payload, dict) or not payload:
                raise ValidationError("payload", "文档数据不能为空")

            # 验证必要字段
            title = payload.get("title")
            content = payload.get("content")
            file_name = payload.get("file_name")

            if any(not isinstance(value, str) or not value.strip() for value in (title, content, file_name)):
                raise ValidationError("required_fields", "title、content和file_name字段不能为空")

            # 验证文件名格式
            if not file_name.strip() or "." not in file_name:
                raise ValidationError("file_name", "文件名必须包含文件扩展名")

            file_extension = file_name.split(".")[-1].lower()
            supported_formats = ["md", "txt"]
            if file_extension not in supported_formats:
                raise ValidationError(
                    "file_name",
                    f"不支持的文件扩展名: {file_extension}。支持的格式: {', '.join(supported_formats)}",
                )

            knowledge_base_path = config.rag.knowledge_base_path
            self._resolve_filename_conflict(knowledge_base_path, file_name)
            file_content = f"# {title}\n\n{content}" if file_extension == "md" else f"{title}\n\n{content}"
            try:
                encoded_content = file_content.encode("utf-8")
            except UnicodeEncodeError:
                raise ValidationError("content", "文档内容必须使用有效UTF-8字符")
            if "\x00" in file_content:
                raise ValidationError("content", "文档内容不能包含空字符")
            if len(encoded_content) > self.MAX_KNOWLEDGE_FILE_SIZE:
                raise ValidationError("content", "文档大小不能超过10 MiB")

            await self._ensure_ready()
            os.makedirs(knowledge_base_path, exist_ok=True)
            final_filename, file_path = self._save_knowledge_file(knowledge_base_path, file_name, file_content)

            # 生成文档ID
            import hashlib

            document_id = hashlib.md5(f"{final_filename}{file_content}".encode()).hexdigest()

            logger.info("知识库文档已保存，大小: %d 字节", len(encoded_content))

            await self._index_saved_document(
                file_content,
                {
                    "title": title,
                    "source": "user_add",
                    "category": payload.get("category", "general"),
                    "filename": final_filename,
                    "file_path": file_path,
                    "add_time": datetime.now().isoformat(),
                },
            )

            return {
                "added": True,
                "document_id": document_id,
                "message": f"文档添加成功，已保存为: {final_filename}",
                "timestamp": datetime.now().isoformat(),
                "file_path": file_path,
                "filename": final_filename,
            }

        except AIOpsException:
            raise
        except Exception as e:
            logger.error("添加文档失败，异常类型: %s", type(e).__name__)
            raise AssistantError("文档添加失败") from e


class PerformanceMonitor:
    """性能监控器"""

    def __init__(self):
        self.metrics = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "total_latency": 0.0,
            "latencies": [],
        }
        self._lock = asyncio.Lock()

    class Timer:
        def __init__(self, monitor, operation):
            self.monitor = monitor
            self.operation = operation
            self.start_time = None

        def __enter__(self):
            self.start_time = asyncio.get_event_loop().time()
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            if self.start_time:
                latency = asyncio.get_event_loop().time() - self.start_time
                asyncio.create_task(self.monitor._record_latency(self.operation, latency))

    def measure(self, operation: str):
        """测量操作耗时"""
        return self.Timer(self, operation)

    async def _record_latency(self, operation: str, latency: float):
        """记录延迟"""
        async with self._lock:
            self.metrics["total_requests"] += 1
            self.metrics["total_latency"] += latency
            self.metrics["latencies"].append(latency)

            # 保持最近样本数量
            max_samples = 1000
            if len(self.metrics["latencies"]) > max_samples:
                self.metrics["latencies"] = self.metrics["latencies"][-max_samples:]

    def record_success(self):
        """记录成功"""
        self.metrics["successful_requests"] += 1

    def record_failure(self):
        """记录失败"""
        self.metrics["failed_requests"] += 1

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        if not self.metrics["latencies"]:
            return {}

        latencies = sorted(self.metrics["latencies"])
        n = len(latencies)

        return {
            "total_requests": self.metrics["total_requests"],
            "success_rate": self.metrics["successful_requests"] / max(self.metrics["total_requests"], 1),
            "avg_latency": self.metrics["total_latency"] / max(self.metrics["total_requests"], 1),
            "p50_latency": latencies[n // 2] if n > 0 else 0,
            "p95_latency": latencies[int(n * 0.95)] if n > 0 else 0,
            "p99_latency": latencies[int(n * 0.99)] if n > 0 else 0,
        }

    def reset(self):
        """重置统计"""
        self.metrics = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "total_latency": 0.0,
            "latencies": [],
        }


class WebSocketManager:
    def __init__(self, message_handler, message_model: Optional[Type[BaseModel]] = None, heartbeat_interval: float = 30.0):
        self.message_handler = message_handler
        self.message_model = message_model

        self.checker = self.Checker(interval=heartbeat_interval)

    async def handle_connection(self, websocket: WebSocket, **kwargs) -> None:
        try:
            require_principal()
        except SessionAccessError:
            await websocket.close(code=1008)
            return
        protocols = websocket.scope.get("subprotocols", [])
        await websocket.accept(subprotocol="cloudops-v1" if "cloudops-v1" in protocols else None)
        await self.checker.start(websocket)

        message_queue = asyncio.Queue(maxsize=32)
        receiver_task = asyncio.create_task(self.websocket_receiver(websocket, message_queue))
        try:
            await self.websocket_handler(websocket, message_queue)

        except Exception as e:
            logger.error("WebSocket处理失败，异常类型: %s", type(e).__name__)
        finally:
            receiver_task.cancel()
            await self.checker.stop(websocket)
            try:
                await receiver_task
            except asyncio.CancelledError:
                pass

    async def websocket_receiver(self, websocket: WebSocket, message_queue: asyncio.Queue) -> None:
        """接收客户端消息推入队列"""
        try:
            while True:
                msg = await websocket.receive()
                logger.debug("收到WebSocket消息，类型: %s", msg.get("type"))
                if "text" in msg and msg["text"] is not None:
                    txt = msg["text"]
                    await self.checker.on_message(websocket, txt)
                    await message_queue.put(txt)
                elif "bytes" in msg and msg["bytes"] is not None:
                    await message_queue.put(msg["bytes"])
                else:
                    logger.debug("WebSocket接收结束或空消息")
        except Exception as e:
            logger.error("WebSocket接收失败，异常类型: %s", type(e).__name__)
        finally:
            task = asyncio.current_task()
            if task is None or not task.cancelling():
                await message_queue.put(None)

    async def websocket_handler(self, websocket: WebSocket, message_queue: asyncio.Queue, **kwargs) -> None:
        """循环消费消息队列并调用业务处理器"""
        while True:
            info_raw = await message_queue.get()
            if info_raw is None:  # 结束信号
                break
            # 尝试解析数据
            parsed = await self._parse_message(info_raw, websocket)
            if parsed is None:  # 解析失败，继续下一条
                continue
            # 调用业务逻辑
            if self.message_handler:
                await self.message_handler(websocket, parsed, **kwargs)
                from starlette.websockets import WebSocketState

                if websocket.application_state != WebSocketState.CONNECTED:
                    break

    async def _parse_message(self, info_raw: Union[str, bytes], websocket: WebSocket) -> Optional[BaseModel] | None:
        """解析原始数据"""
        try:
            if isinstance(info_raw, bytes):
                info_raw = info_raw.decode()
            data_dict = json.loads(info_raw)
            if self.message_model:  # 用 Pydantic Model校验
                return self.message_model(**data_dict)
            else:
                return data_dict
        except Exception as e:
            logger.error("WebSocket消息校验失败，异常类型: %s", type(e).__name__)
            await websocket.send_json({"error": "参数校验失败"})
            return None

    class Checker:
        """ws connection 心跳检查"""

        def __init__(self, interval=30.0, timeout=10.0):
            self.interval = interval
            self.timeout = timeout
            self.last_received_time: Dict[WebSocket, float] = {}
            self.tasks: Dict[WebSocket, asyncio.Task] = {}

        async def start(self, websocket):
            self.last_received_time[websocket] = time.time()

            async def loop():
                while True:
                    await asyncio.sleep(self.interval)
                    now = time.time()
                    if now - self.last_received_time[websocket] > self.interval + self.timeout:
                        await websocket.close()
                        break
                    try:
                        await websocket.send_text("__ping__")
                    except Exception:
                        continue

            self.tasks[websocket] = asyncio.create_task(loop())

        async def stop(self, websocket: WebSocket):
            task = self.tasks.pop(websocket, None)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self.last_received_time.pop(websocket, None)

        async def on_message(self, websocket, message):
            if message == "__pong__":
                self.last_received_time[websocket] = time.time()
