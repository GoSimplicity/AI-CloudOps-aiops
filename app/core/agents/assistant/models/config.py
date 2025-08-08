#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
助手配置模块
"""

from pathlib import Path
from app.config.settings import config


class AssistantConfig:
    """助手配置类"""

    def __init__(self):
        self.llm_provider = config.llm.provider.lower()

        # 路径配置
        base_dir = Path(__file__).parent.parent.parent.parent.parent.parent
        self.vector_db_path = base_dir / config.rag.vector_db_path
        self.knowledge_base_path = base_dir / config.rag.knowledge_base_path
        self.collection_name = config.rag.collection_name

        # Redis配置
        self.redis_config = {
            "host": config.redis.host,
            "port": config.redis.port,
            "db": config.redis.db,
            "password": config.redis.password,
            "connection_timeout": config.redis.connection_timeout,
            "socket_timeout": config.redis.socket_timeout,
            "max_connections": config.redis.max_connections,
            "decode_responses": config.redis.decode_responses,
        }

        # 缓存配置
        self.cache_config = {
            "redis_config": {**self.redis_config, "db": config.redis.db + 1},
            "cache_prefix": "aiops_assistant_cache:",
            "default_ttl": 3600,
            "max_cache_size": 1000,
            "enable_compression": True,
        }

    def ensure_directories(self):
        """确保必要目录存在"""
        self.vector_db_path.mkdir(parents=True, exist_ok=True)
        self.knowledge_base_path.mkdir(parents=True, exist_ok=True)


# 全局配置实例
assistant_config = AssistantConfig()
