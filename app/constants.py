#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 常量定义模块，包含系统中使用的各种常量和配置
"""

# LLM 服务常量
LLM_TIMEOUT_SECONDS = 30
LLM_MAX_RETRIES = 3
OPENAI_TEST_MAX_TOKENS = 5
LLM_CONFIDENCE_THRESHOLD = 0.1
LLM_TEMPERATURE_MIN = 0.0
LLM_TEMPERATURE_MAX = 2.0

# 负载预测常量
LOW_QPS_THRESHOLD = 5.0
MAX_PREDICTION_HOURS = 168  # 7 天
DEFAULT_PREDICTION_HOURS = 24
PREDICTION_VARIATION_FACTOR = 0.1  # 10% 波动

# QPS置信度阈值
QPS_CONFIDENCE_THRESHOLDS = {"low": 100, "medium": 500, "high": 1000, "very_high": 2000}

# 时间模式常量
HOUR_FACTORS = {
    0: 0.3,
    1: 0.2,
    2: 0.15,
    3: 0.1,
    4: 0.1,
    5: 0.2,
    6: 0.4,
    7: 0.6,
    8: 0.8,
    9: 0.9,
    10: 1.0,
    11: 0.95,
    12: 0.9,
    13: 0.95,
    14: 1.0,
    15: 1.0,
    16: 0.95,
    17: 0.9,
    18: 0.8,
    19: 0.7,
    20: 0.6,
    21: 0.5,
    22: 0.4,
    23: 0.3,
}

DAY_FACTORS = {
    0: 0.95,  # 周一
    1: 1.0,  # 周二
    2: 1.05,  # 周三
    3: 1.05,  # 周四
    4: 0.95,  # 周五
    5: 0.7,  # 周六
    6: 0.6,  # 周日
}

# HTTP 状态码
HTTP_STATUS_BAD_REQUEST = 400
HTTP_STATUS_UNAUTHORIZED = 401
HTTP_STATUS_FORBIDDEN = 403
HTTP_STATUS_NOT_FOUND = 404
HTTP_STATUS_INTERNAL_ERROR = 500

# 错误消息字典
ERROR_MESSAGES = {
    "invalid_input": "输入参数无效",
    "service_unavailable": "服务暂时不可用",
    "timeout": "请求超时",
    "not_found": "请求的资源未找到",
    "unauthorized": "未授权访问",
    "rate_limited": "请求频率超限",
    "internal_error": "内部服务错误",
}

# 成功消息字典
SUCCESS_MESSAGES = {
    "operation_completed": "操作成功完成",
    "data_updated": "数据更新成功",
    "analysis_finished": "分析完成",
    "model_trained": "模型训练完成",
}
