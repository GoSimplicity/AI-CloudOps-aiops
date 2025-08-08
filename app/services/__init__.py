#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 服务模块初始化文件，集成Prometheus、Kubernetes等外部服务
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
