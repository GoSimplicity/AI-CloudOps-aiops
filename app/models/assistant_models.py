#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 智能助手模型定义
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# 请求模型
class AssistantRequest(BaseModel):
    question: str = Field(..., min_length=1, description="用户提问")
    mode: str = Field(default="rag", description="助手模式:rag或mcp")
    chat_history: Optional[List[Dict[str, str]]] = Field(
        default=None, description="对话历史"
    )
    use_web_search: bool = Field(default=False, description="是否使用网络搜索")
    session_id: Optional[str] = Field(default=None, description="会话ID")


class AddDocumentRequest(BaseModel):
    title: str = Field(..., min_length=1, description="文档标题")
    content: str = Field(..., min_length=1, description="文档内容")
    file_name: str = Field(..., min_length=1, description="文件名")


class AssistantResponse(BaseModel):
    answer: str
    source_documents: Optional[List[Dict[str, Any]]] = None
    relevance_score: Optional[float] = None
    recall_rate: Optional[float] = None
    follow_up_questions: Optional[List[str]] = None
    session_id: Optional[str] = None


class SessionInfoResponse(BaseModel):
    session_id: str
    created_time: str
    last_activity: str
    message_count: int
    mode: int
    status: str


class ServiceInfoResponse(BaseModel):
    service: str
    version: str
    description: str
    capabilities: List[str]
    endpoints: Dict[str, str]
    constraints: Optional[Dict[str, Any]] = None
    status: str


class ServiceReadyResponse(BaseModel):
    ready: bool
    service: str
    timestamp: str
    message: Optional[str] = None
    initialized: Optional[bool] = None
    healthy: Optional[bool] = None
    status: Optional[str] = None


class ServiceHealthResponse(BaseModel):
    status: str
    service: str
    version: Optional[str] = None
    dependencies: Optional[Dict[str, bool]] = None
    last_check_time: str
    uptime: Optional[float] = None


class ServiceConfigResponse(BaseModel):
    service: str
    config: Dict[str, Any]
    version: Optional[str] = None
    timestamp: str


class RefreshKnowledgeResponse(BaseModel):
    refreshed: bool
    documents_count: int
    vector_count: int
    timestamp: str
    message: str


class ClearCacheResponse(BaseModel):
    cleared: bool
    cache_keys_cleared: int
    timestamp: str
    message: str


class StreamResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    timestamp: str
    message: Optional[str] = None

class UploadKnowledgeResponse(BaseModel):
    uploaded: bool
    document_id: Optional[str] = None
    filename: Optional[str] = None
    file_size: Optional[int] = None
    message: str
    timestamp: str


class AddDocumentResponse(BaseModel):
    added: bool
    document_id: str
    message: str
    timestamp: str


__all__ = [
    # 请求模型
    "AssistantRequest",
    "AddDocumentRequest",
    # 响应模型
    "AssistantResponse",
    "SessionInfoResponse",
    "ServiceInfoResponse",
    "ServiceReadyResponse",
    "ServiceHealthResponse",
    "ServiceConfigResponse",
    "RefreshKnowledgeResponse",
    "ClearCacheResponse",
    "UploadKnowledgeResponse",
    "AddDocumentResponse",
]
