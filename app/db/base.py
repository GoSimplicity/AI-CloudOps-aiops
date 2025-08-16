#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 数据库引擎与会话管理
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
        pass


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            config.database.sqlalchemy_url,
            echo=bool(config.database.echo),
            pool_pre_ping=True,
            pool_recycle=int(config.database.pool_recycle),
            pool_size=int(config.database.pool_size),
            max_overflow=int(config.database.max_overflow),
        )
        try:
            event.listen(_engine, "connect", _set_session_timezone_utc)
        except Exception:
            pass
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
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
