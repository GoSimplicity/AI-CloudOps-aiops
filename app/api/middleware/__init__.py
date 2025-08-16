#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: API 中间件
"""

from fastapi import FastAPI

from .cors import setup_cors
from .error_handler import setup_error_handlers


def register_middleware(app: FastAPI) -> None:
    """注册所有中间件"""
    setup_cors(app)
    setup_error_handlers(app)
