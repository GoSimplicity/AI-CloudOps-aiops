#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
from fastapi import FastAPI

from .cors import setup_cors
from .error_handler import setup_error_handlers


def register_middleware(app: FastAPI) -> None:
    """注册所有中间件"""
    setup_cors(app)
    setup_error_handlers(app)
