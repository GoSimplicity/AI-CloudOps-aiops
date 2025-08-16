#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 应用配置模块
"""

from .logging import setup_logging
from .settings import config

__all__ = ["config", "setup_logging"]
