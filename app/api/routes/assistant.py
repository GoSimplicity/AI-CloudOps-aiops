#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 智能助手API接口
"""

from datetime import datetime
import logging
from typing import Any, Dict
import uuid

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from starlette.websockets import WebSocket

from app.api.decorators import api_response, log_api_call
from app.common.constants import (
    ApiEndpoints,
    AppConstants,
    HttpStatusCodes,
    ServiceConstants,
)
from app.common.exceptions import (
    AIOpsException,
    ServiceUnavailableError,
    ValidationError as DomainValidationError,
)
from app.config.settings import config
from app.models import BaseResponse
from app.models.assistant_models import (
    AddDocumentResponse,
    AssistantRequest,
    AssistantResponse,
    ClearCacheResponse,
    RefreshKnowledgeResponse,
    ServiceConfigResponse,
    ServiceInfoResponse,
    ServiceReadyResponse,
    SessionInfoResponse,
    UploadKnowledgeResponse,
)
from app.services.assistant_service import OptimizedAssistantService
from app.services.factory import ServiceFactory

try:
    from app.common.logger import get_logger

    logger = get_logger("aiops.api.assistant")
except Exception:
    logger = logging.getLogger("aiops.api.assistant")

router = APIRouter(tags=["assistant"])
assistant_service = None


async def get_assistant_service() -> OptimizedAssistantService:
    global assistant_service
    if assistant_service is None:
        assistant_service = await ServiceFactory.get_service(
            "assistant", OptimizedAssistantService
        )
    return assistant_service


@router.post(
    "/session",
    summary="创建会话",
    response_model=BaseResponse,
)
@api_response("创建会话")
async def create_session() -> Dict[str, Any]:
    await (await get_assistant_service()).initialize()
    # 复用服务层的创建逻辑（不需要请求体）
    result = await (await get_assistant_service()).create_session(
        request=AssistantRequest(question="placeholder")
    )
    # 只暴露必要字段
    return {
        "session_id": result.get("session_id"),
        "timestamp": result.get("timestamp"),
    }


@router.post(
    "/query",
    summary="智能助手查询",
    response_model=BaseResponse,
    responses={
        400: {
            "description": "question缺失或为空",
            "content": {
                "application/json": {
                    "examples": {
                        "missing_question": {
                            "summary": "缺少question字段",
                            "value": {
                                "code": 400,
                                "message": "question不能为空",
                                "data": {
                                    "status_code": 400,
                                    "path": "/api/v1/assistant/query",
                                    "method": "POST",
                                    "detail": "question不能为空",
                                    "timestamp": "2025-01-01T00:00:00",
                                },
                            },
                        },
                        "empty_question": {
                            "summary": "question为空字符串",
                            "value": {
                                "code": 400,
                                "message": "question不能为空",
                                "data": {
                                    "status_code": 400,
                                    "path": "/api/v1/assistant/query",
                                    "method": "POST",
                                    "detail": "question不能为空",
                                    "timestamp": "2025-01-01T00:00:00",
                                },
                            },
                        },
                    }
                }
            },
        }
    },
)
@api_response("智能助手查询")
@log_api_call(log_request=True)
async def assistant_query(
    request: Dict[str, Any] = Body(
        ...,
        examples={
            "default": {
                "value": {
                    "question": "如何排查Pod频繁重启的问题？",
                    "mode": 1,
                    "use_web_search": False,
                }
            }
        },
    ),
) -> Dict[str, Any]:
    # 手动校验缺失或空question，按测试期望返回400
    question = (request or {}).get("question")
    if question is None or (isinstance(question, str) and question.strip() == ""):
        raise HTTPException(
            status_code=HttpStatusCodes.BAD_REQUEST, detail="question不能为空"
        )

    await (await get_assistant_service()).initialize()

    # 将dict映射为模型，确保其余字段验证与默认值
    valid_req = AssistantRequest(**request)

    result = await (await get_assistant_service()).get_answer(
        question=valid_req.question,
        mode=valid_req.mode,
        session_id=valid_req.session_id,
    )

    response = AssistantResponse(
        answer=result.get("answer", ""),
        source_documents=result.get("source_documents"),
        relevance_score=result.get("relevance_score"),
        recall_rate=result.get("recall_rate"),
        follow_up_questions=result.get("follow_up_questions"),
        session_id=result.get("session_id"),
    )

    return response.dict()


@router.get(
    "/session/{session_id}",
    summary="获取会话信息",
    response_model=BaseResponse,
)
@api_response("获取会话信息")
async def get_session_info(session_id: str) -> Dict[str, Any]:
    await (await get_assistant_service()).initialize()

    session_info = await (await get_assistant_service()).get_session_info(session_id)

    response = SessionInfoResponse(
        session_id=session_id,
        created_time=session_info.get("created_time", ""),
        last_activity=session_info.get("last_activity", ""),
        message_count=session_info.get("message_count", 0),
        mode=session_info.get("mode", 1),
        status=session_info.get("status", "active"),
    )

    return response.dict()


@router.post(
    "/refresh",
    summary="刷新知识库",
    response_model=BaseResponse,
)
@api_response("刷新知识库")
async def refresh_knowledge_base() -> Dict[str, Any]:
    await (await get_assistant_service()).initialize()

    refresh_result = await (await get_assistant_service()).refresh_knowledge_base()

    response = RefreshKnowledgeResponse(
        refreshed=refresh_result.get("refreshed", True),
        documents_count=refresh_result.get("documents_count", 0),
        vector_count=refresh_result.get("vector_count", 0),
        timestamp=datetime.now().isoformat(),
        message=refresh_result.get("message", "知识库刷新成功"),
    )

    return response.dict()


@router.get(
    "/health",
    summary="智能助手健康检查",
    response_model=BaseResponse,
)
@api_response("智能助手健康检查")
async def assistant_health() -> Dict[str, Any]:
    """兼容前端健康检查入口，复用就绪检查逻辑。"""
    return await assistant_ready()


@router.get(
    "/ready",
    summary="智能助手就绪检查",
    response_model=BaseResponse,
)
@api_response("智能助手就绪检查")
async def assistant_ready() -> Dict[str, Any]:
    try:
        await (await get_assistant_service()).initialize()
        health_status = await (
            await get_assistant_service()
        ).get_service_health_info_with_mode()

        rag_status = health_status.get("modes", {}).get("rag", {}).get("status")
        mcp_status = health_status.get("modes", {}).get("mcp", {}).get("status")
        is_ready = (
            rag_status == ServiceConstants.STATUS_HEALTHY
            or mcp_status == ServiceConstants.STATUS_HEALTHY
        )

        if not is_ready:
            raise ServiceUnavailableError("assistant")

        response = ServiceReadyResponse(
            ready=True,
            service="assistant",
            timestamp=datetime.now().isoformat(),
            message="服务就绪",
            initialized=True,
            healthy=True,
            status="ready",
        )
        return response.dict()
    except (AIOpsException, DomainValidationError):
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"就绪检查失败: {str(e)}")
        raise ServiceUnavailableError("assistant", {"error": str(e)})


@router.post(
    "/clear-cache",
    summary="清除缓存",
    response_model=BaseResponse,
)
@api_response("清除缓存")
async def clear_cache() -> Dict[str, Any]:
    await (await get_assistant_service()).initialize()
    clear_result = await (await get_assistant_service()).clear_cache()

    response = ClearCacheResponse(
        cleared=clear_result.get("cleared", True),
        cache_keys_cleared=clear_result.get("cache_keys_cleared", 0),
        timestamp=datetime.now().isoformat(),
        message=clear_result.get("message", "缓存清除成功"),
    )
    data = response.dict()
    data["cleared_items"] = data.get("cache_keys_cleared", 0)
    return data


@router.post(
    "/upload-knowledge-file",
    summary="上传知识库文件",
    response_model=BaseResponse,
)
@api_response("上传知识库文件")
async def upload_knowledge_file(
    file: UploadFile = File(...),
) -> Dict[str, Any]:
    await (await get_assistant_service()).initialize()

    upload_result = await (await get_assistant_service()).upload_knowledge_file(
        file=file
    )

    response = UploadKnowledgeResponse(
        uploaded=upload_result.get("uploaded", True),
        document_id=upload_result.get("document_id"),
        filename=upload_result.get("filename"),
        file_size=upload_result.get("file_size"),
        message=upload_result.get("message", "知识库上传成功"),
        timestamp=datetime.now().isoformat(),
    )
    return response.dict()


@router.post(
    "/add-document",
    summary="添加知识库文档",
    response_model=BaseResponse,
)
@api_response("添加知识库文档")
async def add_document(request: Dict[str, Any]) -> Dict[str, Any]:
    await (await get_assistant_service()).initialize()
    # 兼容测试payload: { content, metadata }
    payload: Dict[str, Any] = {}
    if isinstance(request, dict) and "content" in request:
        content = request.get("content")
        metadata = request.get("metadata") or {}
        title = metadata.get("title") or metadata.get("source") or "用户文档"
        file_name = metadata.get("filename") or f"{uuid.uuid4().hex[:8]}.md"
        payload = {"title": title, "content": content, "file_name": file_name}
    else:
        payload = request

    result = await (await get_assistant_service()).add_document(payload)

    response = AddDocumentResponse(
        added=result.get("added", True),
        document_id=result.get("document_id", ""),
        message=result.get("message", "文档添加成功"),
        timestamp=datetime.now().isoformat(),
    )
    return response.dict()


@router.get(
    "/config",
    summary="获取智能助手配置",
    response_model=BaseResponse,
)
@api_response("获取智能助手配置")
async def get_assistant_config() -> Dict[str, Any]:
    await (await get_assistant_service()).initialize()

    config_info = await (await get_assistant_service()).get_assistant_config()

    response = ServiceConfigResponse(
        service="assistant", config=config_info, timestamp=datetime.now().isoformat()
    )

    return response.dict()


@router.get(
    "/info",
    summary="AI-CloudOps智能助手服务信息",
    response_model=BaseResponse,
)

@api_response("AI-CloudOps智能助手服务信息")
async def assistant_info() -> Dict[str, Any]:
    info = {
        "service": "AI-CloudOps智能助手",
        "service_type": "enterprise_assistant",
        "workflow_engine": "langgraph + mcp",
        "version": AppConstants.APP_VERSION,
        "description": "智能运维助手，支持RAG和MCP双模式",
        "supported_modes": [
            {"mode": 1, "name": "RAG", "description": "基于知识库的检索增强生成"},
            {"mode": 2, "name": "MCP", "description": "基于工具调用的模型上下文协议"},
        ],
        "capabilities": [
            "智能意图识别",
            "知识库问答",
            "工具调用",
            "故障排查指导",
            "操作步骤指导",
            "上下文对话",
            "文档检索",
            "智能推荐",
            "质量评估",
            "自动重试",
            "Kubernetes操作",
            "系统信息查询",
        ],
        "enterprise_features": [
            "双模式架构(RAG+MCP)",
            "LangGraph工作流引擎",
            "MCP工具调用协议",
            "多级缓存机制",
            "智能质量评估",
            "自动错误恢复",
            "性能监控",
            "并行处理",
            "意图路由",
            "模式隔离",
        ],
        "endpoints": {
            "query": ApiEndpoints.ASSISTANT_QUERY,
            "session": ApiEndpoints.ASSISTANT_SESSION,
            "refresh": ApiEndpoints.ASSISTANT_REFRESH,
            "config": ApiEndpoints.ASSISTANT_CONFIG,
            "ready": ApiEndpoints.ASSISTANT_READY,
            "info": ApiEndpoints.ASSISTANT_INFO,
        },
        "workflow_nodes": [
            "输入验证",
            "缓存检查",
            "意图识别",
            "知识检索",
            "上下文增强",
            "答案生成",
            "质量评估",
            "后处理",
            "缓存存储",
        ],
        "model_info": {
            "provider": "OpenAI/Ollama",
            "architecture": "LangGraph + LLM + MCP",
            "capabilities": [
                "text_generation",
                "conversation",
                "reasoning",
                "workflow_management",
                "tool_calling",
                "kubernetes_operations",
            ],
        },
        "constraints": {
            "max_question_length": config.rag.max_context_length,
            "max_context_docs": ServiceConstants.ASSISTANT_MAX_CONTEXT_DOCS,
            "default_context_docs": ServiceConstants.ASSISTANT_DEFAULT_CONTEXT_DOCS,
            "timeout": ServiceConstants.ASSISTANT_TIMEOUT,
            "session_timeout": ServiceConstants.ASSISTANT_SESSION_TIMEOUT,
            "max_retries": config.mcp.max_retries,
            "quality_threshold": config.rag.similarity_threshold,
        },
        "performance": {
            "caching_enabled": bool(config.rag.cache_expiry > 0),
            "parallel_processing": True,
        },
        "status": "available",
        "initialized": True,
    }

    response = ServiceInfoResponse(
        service=info["service"],
        version=info["version"],
        description=info["description"],
        capabilities=info["capabilities"],
        endpoints=info["endpoints"],
        constraints=info["constraints"],
        status=info["status"],
    )
    data = response.dict()
    data["initialized"] = True
    return data

@router.websocket("/stream", name="AI-CloudOps智能助手服务流式交互")
@api_response("AI-CloudOps智能助手服务流式交互")
async def ws_query(ws: WebSocket):
    await (await get_assistant_service()).initialize()
    await (await get_assistant_service()).websocket.handle_connection(ws)

__all__ = ["router"]
