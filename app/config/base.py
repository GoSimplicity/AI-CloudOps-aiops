#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 配置基础类 - 基于 Pydantic Settings，支持 ENV > YAML > 默认值
"""

import os
from pathlib import Path
import re
from typing import Any, Dict, Type

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict
import yaml

from app.config.envmode import parse_env_mode

# 预加载 .env 到进程环境，确保自定义读取函数也能获取
load_dotenv()


def _get_root_dir() -> Path:
    """项目根目录（app/config/base.py -> app -> 项目根）"""
    return Path(__file__).resolve().parents[2]


def _get_env_name() -> str:
    """已知环境别名使用统一规范；保留自定义环境文件名和既有默认值。"""
    raw = os.getenv("ENV", "development")
    return parse_env_mode(raw) or raw


def _get_yaml_path() -> Path:
    """根据 ENV 选择 YAML 配置文件路径"""
    root = _get_root_dir()
    env_name = _get_env_name()
    if env_name and env_name != "development":
        return root / "config" / f"config.{env_name}.yaml"
    return root / "config" / "config.yaml"


_YAML_BOOL_TAG = "tag:yaml.org,2002:bool"


class StrictBoolSafeLoader(yaml.SafeLoader):
    """SafeLoader 变体：去掉 YAML 1.1 的布尔别名，只把 true/True/TRUE、false/False/FALSE 解析为 bool。

    yes/no/on/off（及其大小写变体）保留为字符串，交由调用方按字面判定。
    SEC-26 第二层审查 P2-2：只用于经 settings._parse_autofix_bool 解析的布尔开关（见 settings.AUTOFIX_SWITCH_CONFIG）：
    autofix.auto_execute_enabled / repair_enabled / bootstrap_enabled，以及 SEC-33 的
    mcp.write_tools_enabled / mcp.secret_read_enabled。普通 CONFIG 保留 SafeLoader 的正常布尔解析合同。

    SEC-26 复审 P3-d：显式 !!bool 标签同样只接受不加引号的 true/True/TRUE、false/False/FALSE；
    !!bool yes / !!bool on / !!bool 'true' 等构造为 INVALID_EXPLICIT_BOOL 哨兵，由调用方按关闭处理并告警。
    构造器只注册在本子类上（add_constructor 会把表复制到子类），yaml.SafeLoader 不受影响。
    """


StrictBoolSafeLoader.yaml_implicit_resolvers = {first: [(tag, regexp) for tag, regexp in resolvers if tag != _YAML_BOOL_TAG] for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()}
StrictBoolSafeLoader.add_implicit_resolver(
    _YAML_BOOL_TAG,
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


class InvalidExplicitBool:
    """显式 !!bool 标签下非法取值的哨兵：不保存原值，布尔上下文中为 False（fail-closed）"""

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "INVALID_EXPLICIT_BOOL"


INVALID_EXPLICIT_BOOL = InvalidExplicitBool()
_STRICT_BOOL_VALUES = {"true": True, "True": True, "TRUE": True, "false": False, "False": False, "FALSE": False}


def _construct_strict_bool(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    """tag:yaml.org,2002:bool 构造器：只有不加引号的 true/false 三种大小写写法有效，其它返回哨兵"""
    if isinstance(node, yaml.ScalarNode) and node.style is None and node.value in _STRICT_BOOL_VALUES:
        return _STRICT_BOOL_VALUES[node.value]
    return INVALID_EXPLICIT_BOOL


StrictBoolSafeLoader.add_constructor(_YAML_BOOL_TAG, _construct_strict_bool)


class ConfigLoadError(RuntimeError):
    """配置存在但无法安全读取或解析；错误信息不包含文件内容。"""


class ConfigSafeLoader(yaml.SafeLoader):
    """普通配置保留 YAML 1.1 布尔值；非法显式布尔值先保留为哨兵。"""


def _construct_config_bool(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    try:
        return yaml.SafeLoader.construct_yaml_bool(loader, node)
    except (yaml.constructor.ConstructorError, KeyError):
        return INVALID_EXPLICIT_BOOL


ConfigSafeLoader.add_constructor(_YAML_BOOL_TAG, _construct_config_bool)
_GATED_BOOL_PATHS = {
    ("autofix", "auto_execute_enabled"),
    ("autofix", "repair_enabled"),
    ("autofix", "bootstrap_enabled"),
    ("mcp", "write_tools_enabled"),
    ("mcp", "secret_read_enabled"),
}
_GATED_BOOL_PREFIXES = {(), ("autofix",), ("mcp",)} | _GATED_BOOL_PATHS


def _normalize_invalid_config_bools(data: Dict[str, Any]) -> None:
    """仅保留五个写门禁的非法显式布尔值关闭合同，不吞掉整份配置。

    先检查再修改，避免 YAML alias 把同一非法值复用到其它字段时漏检。
    只记录已知字段的路径前缀，因此循环 alias 也会有界终止。
    """
    pending = [(data, ())]
    visited = set()
    while pending:
        value, path = pending.pop()
        if isinstance(value, InvalidExplicitBool):
            if path not in _GATED_BOOL_PATHS:
                raise ConfigLoadError("YAML 配置包含非法显式布尔值，请检查字段类型")
            continue
        if not isinstance(value, (dict, list, tuple, set)):
            continue
        identity = (id(value), path)
        if identity in visited:
            continue
        visited.add(identity)
        children = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in children:
            if isinstance(key, InvalidExplicitBool):
                raise ConfigLoadError("YAML 配置包含非法显式布尔键，请检查字段类型")
            child_path = (*path, key) if path is not None else None
            if child_path not in _GATED_BOOL_PREFIXES:
                child_path = None
            pending.append((child, child_path))

    for section, field in _GATED_BOOL_PATHS:
        values = data.get(section)
        if isinstance(values, dict) and isinstance(values.get(field), InvalidExplicitBool):
            values[field] = False


def _load_yaml(loader: Type[yaml.SafeLoader] = yaml.SafeLoader) -> Dict[str, Any]:
    """加载 YAML 映射；仅缺失文件允许回退，损坏或不可读的配置拒绝加载。

    默认保持 SafeLoader 的正常解析合同；仅五个写门禁的非法显式布尔值按单字段关闭。
    loader 只能是 SafeLoader 及其子类。
    """
    if not (isinstance(loader, type) and issubclass(loader, yaml.SafeLoader)):
        raise TypeError("YAML 配置只能用 yaml.SafeLoader 或其子类加载")
    yaml_path = _get_yaml_path()
    default_path = _get_root_dir() / "config" / "config.yaml"

    paths = [yaml_path] if yaml_path == default_path else [yaml_path, default_path]
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            raise ConfigLoadError("无法读取 YAML 配置，请检查文件权限与 UTF-8 编码") from None

        try:
            data = yaml.load(text, Loader=ConfigSafeLoader if loader is yaml.SafeLoader else loader)
        except (yaml.YAMLError, ValueError, TypeError, KeyError, OverflowError):
            raise ConfigLoadError("无法解析 YAML 配置，请检查语法和字段类型") from None
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ConfigLoadError("YAML 配置根节点必须是映射")
        if loader is yaml.SafeLoader:
            _normalize_invalid_config_bools(data)
        return data
    return {}


class BaseAppSettings(BaseSettings):
    """应用基础配置类，统一自定义配置源顺序：
    1) 环境变量 (.env + 进程环境)
    2) YAML 配置文件 (按 ENV 选择)
    3) 初始化参数 / 默认值
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @classmethod
    def load_yaml_config(cls) -> Dict[str, Any]:
        """提供给外部使用的 YAML 加载方法"""
        return _load_yaml()

    @classmethod
    def load_yaml_config_strict_bool(cls) -> Dict[str, Any]:
        """同一份 YAML，但不认 YAML 1.1 布尔别名（yes/on 等保留为字符串）；仅供 autofix 开关使用"""
        return _load_yaml(StrictBoolSafeLoader)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # 自定义 YAML 配置源
        def yaml_settings_source() -> Dict[str, Any]:
            return _load_yaml()

        # 优先级：环境变量 > .env > YAML > 初始化参数 > 文件密钥
        return (
            env_settings,
            dotenv_settings,
            yaml_settings_source,
            init_settings,
            file_secret_settings,
        )
