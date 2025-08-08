#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops - 智能助手代理入口
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 智能助手代理 - 基于RAG技术提供运维知识问答和决策支持服务
"""

# 导入重新组织后的助手类
from app.core.agents.assistant.core import AssistantAgent

# 向后兼容，保持原有接口
__all__ = ["AssistantAgent"]
