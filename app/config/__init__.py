#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 配置模块初始化文件，提供配置设置和日志配置的访问接口
"""

from .logging import setup_logging
from .settings import config

__all__ = ["config", "setup_logging"]
