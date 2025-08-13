#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

# 导入重新组织后的助手类
from app.core.agents.assistant.core import AssistantAgent

# 向后兼容，保持原有接口
__all__ = ["AssistantAgent"]
