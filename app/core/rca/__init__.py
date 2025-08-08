#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 根因分析模块初始化文件，提供智能故障诊断和根因分析功能
"""

from .analyzer import RCAAnalyzer
from .correlator import CorrelationAnalyzer
from .detector import AnomalyDetector

# 向外导出 RCA 作业管理器（按需导入，避免硬耦合）
try:
    from .jobs.job_manager import RCAJobManager  # type: ignore

    __all__ = [
        "RCAAnalyzer",
        "AnomalyDetector",
        "CorrelationAnalyzer",
        "RCAJobManager",
    ]
except Exception:  # 允许在无 Redis 场景下仍可导入核心模块
    __all__ = ["RCAAnalyzer", "AnomalyDetector", "CorrelationAnalyzer"]
