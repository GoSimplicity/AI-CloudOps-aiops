#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Huey 队列实例（RCA专用）

说明：
- 默认在开发/测试环境开启 immediate（同步执行，便于本地与CI运行），生产可通过 ENV=production 或 HUEY_IMMEDIATE=false 关闭。
- 采用 Redis 作为后端，参数复用全局 Redis 配置。
"""

from __future__ import annotations

import os
_huey_mod = None
try:
    _huey_mod = __import__("huey")
except Exception:
    _huey_mod = None

from app.config.settings import config, ENV


def _bool_env(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


# 在非生产环境默认开启 immediate，生产环境默认关闭（可被 HUEY_IMMEDIATE 覆盖）
_default_immediate = ENV != "production"
_immediate = _bool_env(os.getenv("HUEY_IMMEDIATE"), _default_immediate)

if _huey_mod is not None and hasattr(_huey_mod, "RedisHuey"):
    RedisHuey = getattr(_huey_mod, "RedisHuey")
    rca_huey = RedisHuey(
        "aiops-rca",
        host=config.redis.host,
        port=config.redis.port,
        db=config.redis.db,
        password=config.redis.password or None,
        immediate=_immediate,
    )
else:
    # 兜底：未安装 huey 时提供最小可用的装饰器，直接同步执行
    class _DummyHuey:
        def task(self, *args, **kwargs):
            def _decorator(fn):
                def _wrapper(*f_args, **f_kwargs):
                    return fn(*f_args, **f_kwargs)

                return _wrapper

            return _decorator

    rca_huey = _DummyHuey()

__all__ = ["rca_huey"]

