#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 自动修复模块模型定义
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AutoFixRequest(BaseModel):
    """自动修复请求模型"""

    deployment: str = Field(..., min_length=1)
    namespace: str = Field(default="default", min_length=1)
    event: str = Field(..., min_length=1)
    force: bool = Field(default=False)
    auto_restart: bool = Field(default=True)
    container: Optional[str] = Field(default=None, description="目标容器名(可选)")
    wait_rollout: bool = Field(default=True, description="是否等待rollout完成")


class AutoFixResponse(BaseModel):
    """自动修复响应模型"""

    status: str = "completed"
    result: str = ""
    deployment: str
    namespace: str
    event: str
    actions_taken: List[str] = []
    timestamp: str
    execution_time: float
    success: bool = True
    error_message: Optional[str] = None


class AutoFixWorkflowConfirmRequest(BaseModel):
    """人工确认后继续执行自动修复工作流请求模型"""

    plan_id: str = Field(..., min_length=1, description="待确认工作流的计划ID")
    approved_action_ids: List[str] = Field(
        default_factory=list,
        description="人工确认允许继续执行的动作ID列表，必须显式传入",
    )
