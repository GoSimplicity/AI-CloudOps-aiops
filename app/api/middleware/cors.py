#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: CORS中间件配置 - 处理跨域资源共享，支持浏览器端API访问
"""

from fastapi.middleware.cors import CORSMiddleware
import logging

logger = logging.getLogger("aiops.cors")

def setup_cors(app):
    """设置CORS中间件"""
    try:
        # 配置CORS中间件
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],  # 在生产环境中应该设置具体的域名
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
        )
        
        logger.info("CORS中间件设置完成")
        
    except Exception as e:
        logger.error(f"CORS中间件设置失败: {str(e)}")