#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config.settings import config


class Base(DeclarativeBase):
    pass


_engine = None
_SessionFactory: sessionmaker[Session] | None = None


def _set_session_timezone_utc(dbapi_connection, connection_record):
    """Ensure DB session timezone is set to UTC (for MySQL); ignore otherwise."""
    try:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET time_zone = '+00:00'")
        finally:
            cursor.close()
    except Exception:
        # Non-MySQL backends may not support this; ignore
        pass


def get_engine():
    global _engine
    if _engine is None:
        # 只创建一次引擎，避免多进程/多线程重复初始化
        # 兼容 SQLAlchemy 2.0：移除已废弃的 future 参数
        _engine = create_engine(
            config.database.sqlalchemy_url,
            echo=bool(config.database.echo),
            pool_pre_ping=True,
            pool_recycle=int(config.database.pool_recycle),
            pool_size=int(config.database.pool_size),
            max_overflow=int(config.database.max_overflow),
        )
        # 尝试将数据库会话时区统一为 UTC（对 MySQL 生效，其他后端忽略）
        try:
            event.listen(_engine, "connect", _set_session_timezone_utc)
        except Exception:
            pass
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        # 兼容 SQLAlchemy 2.0：移除 autocommit 参数（默认即为 False）
        _SessionFactory = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            expire_on_commit=False,
        )
    return _SessionFactory


def get_session() -> Session:
    return get_session_factory()()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """提供事务范围的会话上下文，自动提交或回滚。
    失败时回滚并向外抛出异常，由调用方决定是否忽略。
    """
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
