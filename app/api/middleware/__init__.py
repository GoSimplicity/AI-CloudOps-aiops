#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 中间件模块初始化文件，配置CORS和错误处理中间件
"""

from .cors import setup_cors
from .error_handler import setup_error_handlers


def register_middleware(app):
    """注册所有中间件"""
    setup_cors(app)
    setup_error_handlers(app)
