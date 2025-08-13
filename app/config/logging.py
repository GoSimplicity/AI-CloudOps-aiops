#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
import logging
import sys
import time
from typing import Any, Optional

from app.config.settings import config


def setup_logging(app: Optional[Any] = None) -> None:
    """
    设置日志配置
    """

    # 日志格式（统一使用 UTC 时间，ISO8601，Z 后缀）
    formatter = logging.Formatter(
        "%(asctime)sZ - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # 统一日志时间为 UTC
    formatter.converter = time.gmtime

    # 控制台处理器（使用 stderr，避免测试环境下 stdout 捕获关闭导致的写入异常）
    console_handler = logging.StreamHandler(sys.stderr)
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
    if app and hasattr(app, "logger"):
        app.logger.setLevel(getattr(logging, config.log_level.upper()))
        for handler in app.logger.handlers[:]:
            app.logger.removeHandler(handler)
        app.logger.addHandler(console_handler)

    # 抑制第三方库冗余日志
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("kubernetes").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # 设置应用日志器
    app_logger = logging.getLogger("aiops")
    app_logger.setLevel(getattr(logging, config.log_level.upper()))
