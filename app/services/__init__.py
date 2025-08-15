#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 服务层（__init__）
"""

try:
    from .kubernetes import KubernetesService
except Exception:
    KubernetesService = None

# 仅导出可用的服务，避免未使用导入
__all__ = [
    name
    for name in ["KubernetesService"]
    if name != "KubernetesService" or KubernetesService is not None
]
