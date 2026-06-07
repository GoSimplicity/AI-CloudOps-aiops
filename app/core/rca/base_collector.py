#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 数据收集器基础类
"""

from abc import ABC, abstractmethod
import asyncio
from datetime import datetime
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("aiops.rca.collectors")


class BaseDataCollector(ABC):
    """
    数据收集器基类

    定义所有数据收集器必须实现的接口，遵循SOLID原则中的接口隔离原则。
    提供统一的错误处理、日志记录和配置管理。
    """

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        """
        初始化数据收集器

        Args:
            name: 收集器名称
            config: 收集器配置
        """
        self.name = name
        self.config = config or {}
        self.logger = logging.getLogger(f"aiops.rca.collectors.{name}")
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """
        初始化收集器

        可能涉及建立连接、验证配置等操作
        """
        if self._initialized:
            return

        async with self._initialize_lock:
            if self._initialized:
                return

            try:
                await self._do_initialize()
                self._initialized = True
                self.logger.info(f"数据收集器 {self.name} 初始化成功")
            except Exception as e:
                self._initialized = False
                self.logger.error(f"数据收集器 {self.name} 初始化失败: {str(e)}")
                raise

    @abstractmethod
    async def _do_initialize(self) -> None:
        """
        执行具体的初始化逻辑

        子类必须实现此方法
        """

    @abstractmethod
    async def collect(
        self, namespace: str, start_time: datetime, end_time: datetime, **kwargs
    ) -> List[Any]:
        """
        收集数据

        Args:
            namespace: Kubernetes命名空间
            start_time: 开始时间
            end_time: 结束时间
            **kwargs: 其他参数

        Returns:
            收集到的数据列表
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """
        健康检查

        Returns:
            bool: 收集器是否健康
        """

    def is_initialized(self) -> bool:
        """
        检查是否已初始化

        Returns:
            bool: 是否已初始化
        """
        return self._initialized

    def _ensure_initialized(self) -> None:
        """
        确保收集器已初始化

        Raises:
            RuntimeError: 如果收集器未初始化
        """
        if not self._initialized:
            raise RuntimeError(f"数据收集器 {self.name} 尚未初始化")

    def _validate_time_range(self, start_time: datetime, end_time: datetime) -> None:
        """
        验证时间范围

        Args:
            start_time: 开始时间
            end_time: 结束时间

        Raises:
            ValueError: 时间范围无效
        """
        if not isinstance(start_time, datetime) or not isinstance(end_time, datetime):
            raise ValueError("时间参数必须是datetime对象")

        if start_time >= end_time:
            raise ValueError("开始时间必须早于结束时间")

        # 限制时间范围不能超过24小时
        time_diff = (end_time - start_time).total_seconds()
        if time_diff > 24 * 3600:
            raise ValueError(
                f"时间范围不能超过24小时，当前范围: {time_diff / 3600:.2f}小时"
            )

        if time_diff < 0:
            raise ValueError("时间范围不能为负数")

    def _validate_namespace(self, namespace: str) -> None:
        """
        验证命名空间参数

        Args:
            namespace: Kubernetes命名空间

        Raises:
            ValueError: 命名空间无效
        """
        if not namespace or not isinstance(namespace, str):
            raise ValueError("命名空间不能为空且必须是字符串")

        if len(namespace) > 253:
            raise ValueError("命名空间长度不能超过253个字符")

        # 基本的Kubernetes命名空间名称验证
        import re

        if not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", namespace):
            raise ValueError(
                "命名空间名称必须符合Kubernetes规范（小写字母、数字、连字符）"
            )

    async def collect_with_retry(
        self,
        namespace: str,
        start_time: datetime,
        end_time: datetime,
        max_retries: int = 3,
        **kwargs,
    ) -> List[Any]:
        """
        带重试的数据收集

        Args:
            namespace: Kubernetes命名空间
            start_time: 开始时间
            end_time: 结束时间
            max_retries: 最大重试次数
            **kwargs: 其他参数

        Returns:
            收集到的数据列表
        """
        self._ensure_initialized()
        self._validate_namespace(namespace)
        self._validate_time_range(start_time, end_time)

        last_exception = None

        for attempt in range(max_retries + 1):
            try:
                self.logger.debug(f"开始收集数据，尝试次数: {attempt + 1}")
                data = await self.collect(namespace, start_time, end_time, **kwargs)
                self.logger.info(f"成功收集 {len(data)} 条数据")
                return data

            except Exception as e:
                last_exception = e
                self.logger.warning(
                    f"数据收集失败 (尝试 {attempt + 1}/{max_retries + 1}): {str(e)}"
                )

                if attempt == max_retries:
                    break

                # 简单的退避策略
                import asyncio

                await asyncio.sleep(2**attempt)

        # 如果所有重试都失败，抛出最后一个异常
        self.logger.error(
            f"数据收集器 {self.name} 在 {max_retries + 1} 次尝试后仍然失败"
        )
        raise last_exception
