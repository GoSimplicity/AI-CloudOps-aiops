#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 依赖注入注册
"""

from __future__ import annotations

from threading import RLock
from typing import Dict, Type, TypeVar

_T = TypeVar("_T")
_SINGLETONS: Dict[Type[object], object] = {}
_LOCK = RLock()


def get_service(cls: Type[_T]) -> _T:
    """按类型获取单例服务实例，若不存在则懒加载创建。

    说明：
    - 服务类应支持无参构造；如需自定义构造参数，请在此处封装工厂逻辑
    - 全局单例以避免在路由处理中重复初始化重资源对象（如K8s、Prometheus客户端等）
    """
    with _LOCK:
        instance = _SINGLETONS.get(cls)
        if instance is None:
            instance = cls()  # type: ignore[call-arg]
            _SINGLETONS[cls] = instance
        return instance  # type: ignore[return-value]


def reset_container() -> None:
    """清空容器（主要用于测试隔离）。"""
    with _LOCK:
        _SINGLETONS.clear()
