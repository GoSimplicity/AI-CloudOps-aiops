#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 跨域请求处理中间件
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("aiops.cors")


def setup_cors(app: FastAPI):
    try:
        # 配置CORS中间件
        # 凭据模式不支持通配符
        from app.config.settings import config

        allowed_origins = [origin.strip() for origin in config.security.cors_origins.split(",") if origin.strip()]
        if "*" in allowed_origins:
            raise ValueError("CORS_ORIGINS must list explicit origins")

        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

        logger.info("CORS中间件设置完成")

    except Exception as e:
        logger.error(f"CORS中间件设置失败: {str(e)}")
        raise
