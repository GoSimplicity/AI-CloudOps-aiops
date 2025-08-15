#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 数据库初始化/建表
"""

from sqlalchemy import text

from .base import Base, get_engine


def init_engine_and_session() -> None:
    """预热数据库连接，验证基本连通性（失败不抛出）。"""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        # 保持静默，避免影响服务启动
        pass


def create_all_tables() -> None:
    """创建当前模块声明的所有表（仅 cl_aiops_ 前缀，不会影响其他表）。"""
    # 确保模型已加载到元数据中（惰性导入）
    try:
        from app.db import models as _models  # type: ignore  # noqa: F401
    except Exception:
        # 即使加载失败也尝试继续创建（可能没有任何模型）
        pass
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
