#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 运行环境判定（与 Go 后端 pkg/envmode 保持一致）及生产环境 debug 启动校验
"""

from __future__ import annotations

from typing import Any, Optional

PRODUCTION = "production"
DEVELOPMENT = "development"
TEST = "test"

# 与 Go 后端 pkg/envmode 的别名表一致：先 strip 再 lower；test 与 testing 都按测试环境（复用 dev 配置）
_ALIASES = {
    "production": PRODUCTION,
    "prod": PRODUCTION,
    "prd": PRODUCTION,
    "development": DEVELOPMENT,
    "dev": DEVELOPMENT,
    "test": TEST,
    "testing": TEST,
}

_MAX_SHOWN_LEN = 16

DEBUG_IN_PRODUCTION_MESSAGE = (
    "生产环境禁止开启 debug：debug 模式下 5xx 响应会带出 message/details 等内部信息。"
    "请设置 DEBUG=false（或 app.debug: false）后再启动；"
    "若并非生产环境，请把 ENV 设为 development/dev 或 test/testing"
)


def parse_env_mode(raw: Any) -> Optional[str]:
    """返回规范化的环境名；空值或无法识别的取值返回 None"""
    if not isinstance(raw, str):
        return None
    return _ALIASES.get(raw.strip().lower())


def is_production(raw: Any) -> bool:
    """与 Go envmode.IsProduction 一致：production/prod/prd 为生产；空值、无法识别的取值也按生产处理（fail-closed）。

    aiops 的环境取值为 config.env（ENV 环境变量 > YAML app.env > 默认 development）。
    ENV 与 app.env 都未设置时按默认 development 处理，这是 aiops 既有的默认值（同时决定加载 config/config.yaml）。
    """
    mode = parse_env_mode(raw)
    return mode is None or mode == PRODUCTION


def debug_details_allowed(env: Any, debug: Any) -> bool:
    """5xx 响应是否允许带出 message/details：只有 debug 开启且非生产环境"""
    return bool(debug) and not is_production(env)


def describe_env(raw: Any) -> str:
    """用于启动报错的环境取值描述；过长取值不回显"""
    if not isinstance(raw, str):
        return f"(类型 {type(raw).__name__})"
    value = raw.strip().lower()
    if len(value) > _MAX_SHOWN_LEN:
        return f"(长度{len(value)}，不回显)"
    return repr(value)


class DebugInProductionError(RuntimeError):
    """生产环境开启 debug 时拒绝启动"""


def ensure_debug_safe(env: Any, debug: Any) -> None:
    """生产环境（含无法识别的 ENV 取值）开启 debug 时拒绝启动"""
    if bool(debug) and is_production(env):
        raise DebugInProductionError(f"{DEBUG_IN_PRODUCTION_MESSAGE}（当前 ENV={describe_env(env)}）")
