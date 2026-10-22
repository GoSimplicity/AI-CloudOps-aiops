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
from datetime import datetime
import json
import logging
import os
import time
import traceback
from typing import TYPE_CHECKING, Any, Dict, Optional, Type, Union
import uuid

from pydantic import BaseModel
from starlette.websockets import WebSocket

from ..common.exceptions import AssistantError, ValidationError
from ..config.settings import config
from ..core.agents.enterprise_assistant import get_enterprise_assistant
from ..models import AssistantRequest
from ..models.assistant_models import StreamResponse
from .base import BaseService
from .factory import ServiceFactory

if TYPE_CHECKING:
    from .mcp_service import MCPService

logger = logging.getLogger("aiops.services.assistant")


class OptimizedAssistantService(BaseService):
    """助手服务"""

    def __init__(self) -> None:
        super().__init__("assistant")
        self._assistant = None
        self._performance_monitor = PerformanceMonitor()
        self.websocket = WebSocketManager(self.handle_ws_answer, AssistantRequest)

    async def handle_ws_answer(self, websocket: WebSocket, req: AssistantRequest):
        """WebSocket 消息处理适配器"""
        try:
            result = await self.get_answer(
                question=req.question,
                mode=req.mode,
                session_id=req.session_id
            )
            await websocket.send_json(StreamResponse(
                data=result,
                success=True,
                timestamp=datetime.now().isoformat(),
            ).dict())

        except Exception as e:

            await websocket.send_json(StreamResponse(
                success=False,
                timestamp=datetime.now().isoformat(),
                message=str(e),
            ).dict())

    async def _do_initialize(self) -> None:
        try:
            # 通过服务注入，避免 Core 直接依赖
            from app.services.llm import LLMService

            llm_service = LLMService()
            self._assistant = await get_enterprise_assistant(llm_client=llm_service)
            logger.info("智能助手服务初始化完成")
        except Exception as e:
            logger.warning(f"服务初始化失败: {str(e)}，将在首次使用时重试")
            # 允许服务启动，延迟初始化
            self._assistant = None

    async def _do_health_check(self) -> bool:
        try:
            # 如果assistant为None，尝试获取但不强制失败
            if not self._assistant:
                try:
                    from app.services.llm import LLMService

                    llm_service = LLMService()
                    self._assistant = await get_enterprise_assistant(
                        llm_client=llm_service
                    )
                except Exception as e:
                    logger.debug(f"获取助手实例失败: {str(e)}")
                    # 返回部分健康状态，表示服务框架可用但助手未初始化
                    return True  # 允许服务保持可用状态

            if self._assistant:
                health = await self._assistant.health_check()
                return health.get("status") == "healthy"
            else:
                # 助手未初始化，但服务框架正常
                return True

        except Exception as e:
            logger.warning(f"健康检查失败: {str(e)}")
            return True  # 即使健康检查失败，也保持服务可用

    async def get_answer(
        self,
        question: str,
        mode: int = 1,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """获取回答"""
        # 会话管理（若未提供则自动创建）
        if not session_id:
            session_id = str(uuid.uuid4())
            logger.info(f"为请求创建新会话: {session_id}")

        # 参数验证
        self._validate_question(question)
        self._validate_mode(mode)

        # 根据模式选择处理方式
        if mode == 2:  # MCP模式
            return await self._handle_mcp_mode(question, session_id)
        else:  # RAG模式 (mode == 1 或其他值默认为RAG)
            return await self._handle_rag_mode(question, session_id)

    async def _use_fallback_response(
        self, question: str, session_id: Optional[str], error_reason: str
    ) -> Dict[str, Any]:
        try:
            logger.warning(f"使用备用实现: {error_reason}")

            from app.core.agents.fallback_models import (
                ResponseContext,
                SessionManager,
                generate_fallback_answer,
                sanitize_input,
            )

            cleaned_question = sanitize_input(question)
            session_manager = SessionManager()
            session = None
            if session_id:
                session = session_manager.get_session(session_id)
                if not session:
                    session = session_manager.create_session(session_id)

            context = ResponseContext(
                user_input=cleaned_question,
                session=session,
                additional_context={
                    "error_reason": error_reason,
                    "service": "assistant_service",
                },
            )

            fallback_answer = generate_fallback_answer(context)

            if session:
                session_manager.update_session(session_id, cleaned_question)

            return {
                "answer": fallback_answer,
                "confidence_score": config.rag.similarity_threshold * 0.4,
                "source_documents": [],
                "cache_hit": False,
                "processing_time": 0.1,
                "session_id": session_id,
                "success": True,
                "fallback_used": True,
                "fallback_reason": error_reason,
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as fallback_e:
            logger.error(f"备用实现失败: {fallback_e}")
            return {
                "answer": "抱歉，当前服务不可用，请稍后重试或联系技术支持。",
                "confidence_score": 0.0,
                "source_documents": [],
                "cache_hit": False,
                "processing_time": 0.0,
                "session_id": session_id,
                "success": False,
                "error": f"主要服务: {error_reason}, 备用服务: {str(fallback_e)}",
                "timestamp": datetime.now().isoformat(),
            }

    async def refresh_knowledge_base(self) -> Dict[str, Any]:
        self._ensure_initialized()

        try:
            result = await self._assistant.refresh_knowledge_base()
            self._performance_monitor.reset()

            return {
                "refreshed": result.get("success", False),
                "message": result.get("message", "知识库刷新完成"),
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as e:
            logger.error(f"刷新知识库失败: {str(e)}")
            raise AssistantError(f"刷新失败: {str(e)}")

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
            logger.error(f"清除缓存失败: {str(e)}")
            self._performance_monitor.reset()
            return {
                "cache_cleared": False,
                "message": f"缓存清除失败: {str(e)}",
                "timestamp": datetime.now().isoformat(),
            }

    async def create_session(self, request) -> Dict[str, Any]:
        """创建会话"""
        try:
            # 确保服务就绪
            await self._ensure_ready()

            session_id = str(uuid.uuid4())

            # 如果助手支持会话创建，调用助手方法
            if self._assistant and hasattr(self._assistant, "create_session"):
                result = await self._assistant.create_session(
                    session_id,
                    {
                        "mode": request.mode,
                        "question": (
                            request.question if hasattr(request, "question") else None
                        ),
                        "chat_history": (
                            request.chat_history
                            if hasattr(request, "chat_history")
                            else None
                        ),
                        "created_time": datetime.now().isoformat(),
                    },
                )
                return {
                    "session_id": result.get("session_id", session_id),
                    "created": True,
                    "message": "会话创建成功",
                    "timestamp": datetime.now().isoformat(),
                }
            else:
                # 使用备用会话管理器
                from app.core.agents.fallback_models import SessionManager

                session_manager = SessionManager()
                session_manager.create_session(session_id)

                return {
                    "session_id": session_id,
                    "created": True,
                    "message": "会话创建成功（使用备用实现）",
                    "timestamp": datetime.now().isoformat(),
                }

        except Exception as e:
            logger.error(f"创建会话失败: {str(e)}")
            raise AssistantError(f"创建会话失败: {str(e)}")

    async def create_or_get_session(
        self, user_id: str, mode: int = 1
    ) -> Dict[str, Any]:
        """根据用户ID创建或获取会话"""
        try:
            # 确保服务就绪
            await self._ensure_ready()

            # 生成基于用户ID的会话ID
            import hashlib

            session_id = f"user_{user_id}_{hashlib.md5(f'{user_id}_{mode}'.encode()).hexdigest()[:8]}"

            # 检查是否已存在会话
            session_info = None
            try:
                if self._assistant:
                    session_info = await self._assistant.get_session_info(session_id)
                    if session_info.get("success") and session_info.get("session_id"):
                        # 会话已存在，更新最后活动时间
                        if hasattr(self._assistant, "context_manager"):
                            self._assistant.context_manager.update_context(
                                session_id,
                                {
                                    "mode": mode,
                                    "last_access": datetime.now().isoformat(),
                                },
                            )

                        session_details = (
                            self._assistant.context_manager.get_session_details(
                                session_id
                            )
                        )
                        return {
                            "session_id": session_id,
                            "created": False,
                            "status": "active",
                            "created_time": session_details.get("created_time", ""),
                            "message": "会话已存在，返回现有会话",
                            "timestamp": datetime.now().isoformat(),
                        }
            except Exception as e:
                logger.debug(f"获取现有会话失败，将创建新会话: {str(e)}")

            # 创建新会话
            if self._assistant and hasattr(self._assistant, "create_session"):
                await self._assistant.create_session(
                    session_id,
                    {
                        "mode": mode,
                        "user_id": user_id,
                        "created_time": datetime.now().isoformat(),
                    },
                )

                session_details = self._assistant.context_manager.get_session_details(
                    session_id
                )
                return {
                    "session_id": session_id,
                    "created": True,
                    "status": "active",
                    "created_time": session_details.get(
                        "created_time", datetime.now().isoformat()
                    ),
                    "message": "新会话创建成功",
                    "timestamp": datetime.now().isoformat(),
                }
            else:
                # 使用备用会话管理器
                from app.core.agents.fallback_models import SessionManager

                session_manager = SessionManager()
                session = session_manager.get_session(session_id)

                if not session:
                    session = session_manager.create_session(session_id)
                    created = True
                    message = "新会话创建成功（使用备用实现）"
                else:
                    created = False
                    message = "会话已存在，返回现有会话（使用备用实现）"

                return {
                    "session_id": session_id,
                    "created": created,
                    "status": "active",
                    "created_time": (
                        session.created_at.isoformat()
                        if session
                        else datetime.now().isoformat()
                    ),
                    "message": message,
                    "timestamp": datetime.now().isoformat(),
                }

        except Exception as e:
            logger.error(f"创建或获取会话失败: {str(e)}")
            raise AssistantError(f"创建或获取会话失败: {str(e)}")

    async def get_session_info(self, session_id: str) -> Dict[str, Any]:
        """获取会话信息"""
        self._ensure_initialized()

        try:
            return await self._assistant.get_session_info(session_id)
        except Exception as e:
            logger.error(f"获取会话信息失败: {str(e)}")
            raise AssistantError(f"获取会话失败: {str(e)}")

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
                "status": (
                    "healthy" if health.get("status") == "healthy" else "degraded"
                ),
                "components": components,
                "performance": perf_stats,
                "fallback_capabilities": fallback_status,
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as e:
            return {
                "service": "optimized_assistant",
                "status": "unhealthy",
                "error": str(e),
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

    async def get_assistant_config(self) -> Dict[str, Any]:
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
            raise ValidationError(
                "question", f"问题长度不能超过{config.rag.max_context_length}字符"
            )

    async def _ensure_ready(self) -> None:
        """确保服务就绪"""
        if not self._assistant:
            try:
                # 运行时注入依赖，避免 Core 直接依赖
                from app.services.llm import LLMService

                llm_service = LLMService()
                self._assistant = await get_enterprise_assistant(llm_client=llm_service)
                logger.info("智能助手在运行时成功初始化")
            except Exception as e:
                logger.error(f"无法初始化智能助手: {str(e)}")
                raise AssistantError(f"服务暂未就绪: {str(e)}")

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

    def _enhance_result(
        self, result: Dict[str, Any], question: str, session_id: Optional[str]
    ) -> Dict[str, Any]:
        """补充返回结果"""
        enhanced = result.copy()

        # 添加元数据
        enhanced.update(
            {
                "question": question,
                "session_id": session_id,
                "timestamp": datetime.now().isoformat(),
                "service_version": config.app.version
                if hasattr(config, "app")
                else "1.0.0",
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

    async def _handle_rag_mode(
        self, question: str, session_id: Optional[str]
    ) -> Dict[str, Any]:
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
                    self._assistant.get_answer(
                        question=question, session_id=session_id
                    ),
                    timeout=timeout,
                )

                # 记录成功指标
                self._performance_monitor.record_success()

                # 增强结果
                enhanced_result = self._enhance_result(result, question, session_id)
                enhanced_result["mode"] = "rag"  # 标记模式
                return enhanced_result

            except asyncio.TimeoutError:
                logger.error(f"RAG请求超时: {timeout}秒")
                return await self._use_fallback_response(question, session_id, "超时")
            except Exception as e:
                self._performance_monitor.record_failure()
                logger.error(f"RAG获取答案失败: {str(e)}")
                return await self._use_fallback_response(question, session_id, str(e))

    async def _handle_mcp_mode(
        self, question: str, session_id: Optional[str]
    ) -> Dict[str, Any]:
        """处理工具协议模式请求"""
        try:
            # 通过工厂延迟获取服务实例，避免循环依赖
            from .mcp_service import MCPService

            mcp_service: MCPService = await ServiceFactory.get_service(
                "mcp", MCPService
            )

            # 调用服务处理
            result = await mcp_service.get_answer(
                question=question, session_id=session_id
            )

            # 记录成功指标
            self._performance_monitor.record_success()

            # 增强结果，保持返回结构一致
            enhanced_result = self._enhance_result(result, question, session_id)
            enhanced_result["mode"] = "mcp"
            return enhanced_result

        except Exception as e:
            self._performance_monitor.record_failure()
            logger.error(f"MCP获取答案失败: {str(e)}")
            return await self._use_mcp_fallback_response(question, session_id, str(e))

    async def _get_mcp_service(self) -> "MCPService":
        """兼容旧接口：通过工厂获取MCP服务单例"""
        from .mcp_service import MCPService

        return await ServiceFactory.get_service("mcp", MCPService)

    async def _use_mcp_fallback_response(
        self, question: str, session_id: Optional[str], error_reason: str
    ) -> Dict[str, Any]:
        """工具协议模式的备用响应"""
        logger.warning(f"使用MCP备用实现处理请求，原因: {error_reason}")

        # 简单的关键词匹配
        question_lower = question.lower()

        if any(
            keyword in question_lower
            for keyword in ["时间", "几点", "现在", "当前时间"]
        ):
            from datetime import datetime

            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fallback_answer = f"当前时间是: {current_time}"
        else:
            fallback_answer = "抱歉，MCP服务当前不可用，请稍后重试或切换到RAG模式。"

        return {
            "answer": fallback_answer,
            "confidence_score": config.rag.similarity_threshold * 0.4,
            "source_documents": [],
            "cache_hit": False,
            "processing_time": 0.1,
            "session_id": session_id,
            "success": True,
            "fallback_used": True,
            "fallback_reason": error_reason,
            "mode": "mcp",
            "timestamp": datetime.now().isoformat(),
        }

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
                logger.debug(f"获取MCP健康信息失败: {e}")
                mcp_health = {"status": "unavailable", "error": str(e)}

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

        except Exception as e:
            return {
                "service": "assistant_service_unified",
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }

    async def upload_knowledge_file(self, file) -> Dict[str, Any]:
        try:
            # 确保服务就绪
            await self._ensure_ready()

            # 验证文件
            if not file:
                raise ValidationError("file", "文件不能为空")

            # 读取文件内容
            content = await file.read()

            # 检查文件大小
            max_file_size = 10 * 1024 * 1024  # 10MB
            if len(content) > max_file_size:
                raise ValidationError(
                    "file", f"文件大小不能超过{max_file_size // (1024 * 1024)}MB"
                )

            # 获取原始文件名
            original_filename = file.filename or "unknown.txt"
            file_extension = (
                original_filename.split(".")[-1].lower()
                if "." in original_filename
                else "txt"
            )

            # 验证文件格式
            supported_formats = ["md", "txt", "doc", "docx"]
            if file_extension not in supported_formats:
                raise ValidationError(
                    "file",
                    f"不支持的文件格式: {file_extension}。支持的格式: {', '.join(supported_formats)}",
                )

            # 解析文件内容
            try:
                if file_extension in ["txt", "md", "markdown"]:
                    file_content = content.decode("utf-8")
                elif file_extension in ["doc", "docx"]:
                    # 暂时作为文本处理，后续可以集成python-docx库
                    try:
                        file_content = content.decode("utf-8")
                    except UnicodeDecodeError:
                        raise ValidationError(
                            "file", "Word文档内容解析暂不支持，请转换为文本格式上传"
                        )
                else:
                    file_content = content.decode("utf-8")
            except UnicodeDecodeError:
                raise ValidationError(
                    "file", "文件编码不支持，请使用UTF-8编码的文本文件"
                )

            # 获取知识库根路径
            knowledge_base_path = config.rag.knowledge_base_path

            # 确保知识库目录存在
            os.makedirs(knowledge_base_path, exist_ok=True)

            # 处理文件名冲突
            final_filename = self._resolve_filename_conflict(
                knowledge_base_path, original_filename
            )

            # 文件完整路径
            file_path = os.path.join(knowledge_base_path, final_filename)

            # 写入文件到知识库路径
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(file_content)

            # 生成文档ID
            import hashlib

            document_id = hashlib.md5(
                f"{final_filename}{file_content}".encode()
            ).hexdigest()

            logger.info(f"知识库文件已保存: {file_path}")

            # 立即更新向量索引
            try:
                # 准备文档元数据
                metadata = {
                    "title": os.path.splitext(final_filename)[0],
                    "source": "user_upload",
                    "category": "general",
                    "filename": final_filename,
                    "file_path": file_path,
                    "upload_time": datetime.now().isoformat(),
                }

                # 调用助手服务添加到向量存储
                if self._assistant:
                    upload_result = await self._assistant.upload_knowledge(
                        {
                            "content": file_content,
                            "metadata": metadata,
                            "title": metadata["title"],
                            "source": "user_upload",
                        }
                    )

                    if upload_result.get("success"):
                        logger.info(
                            f"文档已添加到向量索引: {final_filename}, 生成块数: {upload_result.get('document_count', 0)}"
                        )
                    else:
                        logger.warning(
                            f"向量索引更新失败: {upload_result.get('message', '未知错误')}"
                        )
                else:
                    logger.warning("助手服务未初始化，向量索引未更新")

            except Exception as vector_error:
                # 向量索引更新失败不应该影响文件上传成功
                logger.error(f"更新向量索引失败: {vector_error}")

            return {
                "uploaded": True,
                "document_id": document_id,
                "filename": final_filename,
                "file_size": len(content),
                "message": f"文件上传成功，已保存为: {final_filename}",
                "timestamp": datetime.now().isoformat(),
                "file_path": file_path,
            }

        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"上传文件失败: {str(e)}")
            raise AssistantError(f"文件上传失败: {str(e)}")

    def _resolve_filename_conflict(self, directory: str, filename: str) -> str:
        """解决文件名冲突，如果文件已存在则添加UUID"""
        if not filename or filename in {".", ".."} or os.path.basename(filename) != filename or "\\" in filename or "\x00" in filename:
            raise ValidationError("file_name", "文件名不能包含路径或控制字符")
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

    async def add_document(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            # 确保服务就绪
            await self._ensure_ready()

            # 验证payload
            if not payload:
                raise ValidationError("payload", "文档数据不能为空")

            # 验证必要字段
            title = payload.get("title")
            content = payload.get("content")
            file_name = payload.get("file_name")

            if not title or not content or not file_name:
                raise ValidationError(
                    "required_fields", "title、content和file_name字段不能为空"
                )

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

            # 获取知识库根路径
            knowledge_base_path = config.rag.knowledge_base_path

            # 确保知识库目录存在
            os.makedirs(knowledge_base_path, exist_ok=True)

            # 处理文件名冲突
            final_filename = self._resolve_filename_conflict(
                knowledge_base_path, file_name
            )

            # 构建文件内容（将title作为一级标题，然后是content）
            if file_extension == "md":
                file_content = f"# {title}\n\n{content}"
            else:  # txt格式
                file_content = f"{title}\n\n{content}"

            # 文件完整路径
            file_path = os.path.join(knowledge_base_path, final_filename)

            # 写入文件到知识库路径
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(file_content)

            # 生成文档ID
            import hashlib

            document_id = hashlib.md5(
                f"{final_filename}{file_content}".encode()
            ).hexdigest()

            logger.info(f"知识库文档已保存: {file_path}")

            # 立即更新向量索引
            try:
                # 准备文档元数据
                metadata = {
                    "title": title,
                    "source": "user_add",
                    "category": payload.get("category", "general"),
                    "filename": final_filename,
                    "file_path": file_path,
                    "add_time": datetime.now().isoformat(),
                }

                # 调用助手服务添加到向量存储
                if self._assistant:
                    upload_result = await self._assistant.upload_knowledge(
                        {
                            "content": file_content,
                            "metadata": metadata,
                            "title": title,
                            "source": "user_add",
                        }
                    )

                    if upload_result.get("success"):
                        logger.info(
                            f"文档已添加到向量索引: {final_filename}, 生成块数: {upload_result.get('document_count', 0)}"
                        )
                    else:
                        logger.warning(
                            f"向量索引更新失败: {upload_result.get('message', '未知错误')}"
                        )
                else:
                    logger.warning("助手服务未初始化，向量索引未更新")

            except Exception as vector_error:
                # 向量索引更新失败不应该影响文档添加成功
                logger.error(f"更新向量索引失败: {vector_error}")

            return {
                "added": True,
                "document_id": document_id,
                "message": f"文档添加成功，已保存为: {final_filename}",
                "timestamp": datetime.now().isoformat(),
                "file_path": file_path,
                "filename": final_filename,
            }

        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"添加文档失败: {str(e)}")
            raise AssistantError(f"文档添加失败: {str(e)}")


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
                asyncio.create_task(
                    self.monitor._record_latency(self.operation, latency)
                )

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
            "success_rate": self.metrics["successful_requests"]
            / max(self.metrics["total_requests"], 1),
            "avg_latency": self.metrics["total_latency"]
            / max(self.metrics["total_requests"], 1),
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

    def __init__(self, message_handler, message_model: Optional[Type[BaseModel]]=None, heartbeat_interval: float = 30.0):
        self.message_handler = message_handler
        self.message_model = message_model

        self.checker = self.Checker(interval=heartbeat_interval)

    async def handle_connection(self, websocket: WebSocket, **kwargs) -> None:
        await websocket.accept()
        await self.checker.start(websocket)

        message_queue = asyncio.Queue()
        receiver_task = asyncio.create_task(
            self.websocket_receiver(websocket, message_queue)
        )
        try:
            await self.websocket_handler(websocket, message_queue)

        except Exception as e:
            logger.error(f"Error handling WebSocket connection: {str(e)}")
            logger.error(traceback.format_exc())
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
                logger.debug(f"Received raw WebSocket message: {msg}")
                if "text" in msg and msg["text"] is not None:
                    txt = msg["text"]
                    await self.checker.on_message(websocket, txt)
                    await message_queue.put(txt)
                elif "bytes" in msg and msg["bytes"] is not None:
                    await message_queue.put(msg["bytes"])
                else:
                    logger.warning(f"Unknown or empty message: {msg}")
        except Exception as e:
            logger.error(f"WebSocket receiver error: {str(e)}")
        finally:
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
            logger.error(f"Message parse/validation error: {str(e)}")
            await websocket.send_json({"error": f"参数校验失败: {str(e)}"})
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