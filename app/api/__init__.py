#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: API模块初始化文件，提供路由注册和中间件配置功能
"""

from .middleware import register_middleware
from .routes import register_routes

__all__ = ["register_routes", "register_middleware"]
