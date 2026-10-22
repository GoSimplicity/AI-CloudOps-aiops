#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 模块初始化文件
"""

from fastapi import FastAPI

from .auth import setup_auth
from .cors import setup_cors
from .error_handler import setup_error_handlers
from .request_context import setup_request_context


def register_middleware(app: FastAPI) -> None:
    """统一注册应用所需中间件。

    Args:
        app: FastAPI 应用实例。
    """
    setup_auth(app)
    setup_request_context(app)
    setup_cors(app)
    setup_error_handlers(app)
