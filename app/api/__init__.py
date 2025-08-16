#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 模块：__init__
"""

from .middleware import register_middleware
from .routes import register_routes

__all__ = ["register_routes", "register_middleware"]
