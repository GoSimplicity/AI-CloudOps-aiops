#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 日志配置模块 - 为整个应用程序提供集中的日志设置和配置管理
"""

import logging
import sys
from typing import Optional
from typing import Any
from app.config.settings import config

def setup_logging(app: Optional[Any] = None) -> None:
    """
    设置日志配置
    """

    # 日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(getattr(logging, config.log_level.upper()))

    # 根日志器配置
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.log_level.upper()))

    # 清除已有的处理器
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    root_logger.addHandler(console_handler)

    # FastAPI应用日志配置 (FastAPI没有内置logger，使用根日志器)
    if app and hasattr(app, 'logger'):
        app.logger.setLevel(getattr(logging, config.log_level.upper()))
        for handler in app.logger.handlers[:]:
            app.logger.removeHandler(handler)
        app.logger.addHandler(console_handler)

    # 设置第三方库日志级别
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    logging.getLogger('kubernetes').setLevel(logging.WARNING)
    logging.getLogger('openai').setLevel(logging.WARNING)

    # 设置应用日志器
    app_logger = logging.getLogger('aiops')
    app_logger.setLevel(getattr(logging, config.log_level.upper()))
