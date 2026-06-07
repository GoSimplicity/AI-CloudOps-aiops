#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基础服务抽象类
"""

from abc import ABC, abstractmethod
import asyncio
from datetime import datetime
import logging
from typing import Any, Dict, Optional

from app.common.constants import ServiceConstants
from app.common.exceptions import ServiceUnavailableError


class BaseService(ABC):
    """业务服务基类"""

    def __init__(self, service_name: str) -> None:
        self.service_name = service_name
        try:
            from app.common.logger import get_logger

            self.logger = get_logger(f"aiops.services.{service_name}")
        except Exception:
            self.logger = logging.getLogger(f"aiops.services.{service_name}")
        self._initialized = False
        self._initialize_lock = asyncio.Lock()
        self._last_health_check = None
        self._health_status = False

    async def initialize(self) -> None:
        if self._initialized:
            return

        async with self._initialize_lock:
            if self._initialized:
                return

            self.logger.info(f"正在初始化服务: {self.service_name}")

            try:
                await self._do_initialize()
                self._initialized = True
                self.logger.info(f"服务 {self.service_name} 初始化完成")
            except Exception as e:
                self._initialized = False
                self.logger.error(f"服务 {self.service_name} 初始化失败: {str(e)}")
                raise ServiceUnavailableError(
                    service_name=self.service_name,
                    details={"error": str(e), "phase": "initialization"},
                )

    @abstractmethod
    async def _do_initialize(self) -> None:
        pass

    async def health_check(self) -> bool:
        try:
            # 缓存健康检查结果，避免频繁检查
            now = datetime.now()
            if (
                self._last_health_check
                and (now - self._last_health_check).seconds
                < ServiceConstants.HEALTH_CHECK_CACHE_SECONDS
            ):
                return self._health_status

            self._health_status = await self._do_health_check()
            self._last_health_check = now

            return self._health_status

        except Exception as e:
            self.logger.warning(f"服务 {self.service_name} 健康检查失败: {str(e)}")
            self._health_status = False
            return False

    @abstractmethod
    async def _do_health_check(self) -> bool:
        """子类实现具体的健康检查逻辑"""

    def is_healthy(self) -> bool:
        """同步获取健康状态"""
        return self._health_status

    def is_initialized(self) -> bool:
        """检查服务是否已初始化"""
        return self._initialized

    async def execute_with_timeout(
        self,
        operation,
        timeout: float = ServiceConstants.DEFAULT_SERVICE_TIMEOUT,
        operation_name: str = "operation",
    ) -> Any:
        """执行带超时保护的操作"""
        try:
            # 直接传入协程对象的情况
            if asyncio.iscoroutine(operation):
                return await asyncio.wait_for(operation, timeout=timeout)

            # 传入的是协程函数（未调用）
            if asyncio.iscoroutinefunction(operation):
                return await asyncio.wait_for(operation(), timeout=timeout)

            # 其他可调用对象（可能是同步函数，或返回协程的函数）
            if callable(operation):
                result = await asyncio.wait_for(
                    asyncio.to_thread(operation), timeout=timeout
                )
                # 如果返回的是协程，则继续等待其完成
                if asyncio.iscoroutine(result):
                    return await asyncio.wait_for(result, timeout=timeout)
                return result

            raise ValueError(
                "operation must be a coroutine, coroutine function, or callable"
            )

        except asyncio.TimeoutError:
            self.logger.error(f"服务 {self.service_name} 执行 {operation_name} 超时")
            raise ServiceUnavailableError(
                service_name=self.service_name,
                details={"error": f"操作 {operation_name} 超时", "timeout": timeout},
            )
        except Exception as e:
            self.logger.error(
                f"服务 {self.service_name} 执行 {operation_name} 失败: {str(e)}"
            )
            raise ServiceUnavailableError(
                service_name=self.service_name,
                details={"error": str(e), "operation": operation_name},
            )

    def get_service_info(self) -> Dict[str, Any]:
        """获取服务信息"""
        return {
            "name": self.service_name,
            "initialized": self._initialized,
            "healthy": self._health_status,
            "last_health_check": (
                self._last_health_check.isoformat() if self._last_health_check else None
            ),
        }

    def _ensure_initialized(self) -> None:
        """确保服务已初始化"""
        if not self._initialized:
            from app.common.exceptions import PredictionError

            raise PredictionError("服务未初始化")

    async def cleanup(self) -> None:
        """清理服务资源"""
        try:
            self._initialized = False
            self._health_status = False
            self.logger.info(f"服务 {self.service_name} 资源清理完成")
        except Exception as e:
            self.logger.error(f"服务 {self.service_name} 资源清理失败: {str(e)}")
            raise


class HealthCheckMixin:
    """健康检查混合类"""

    @staticmethod
    def create_health_response(
        service_name: str,
        is_healthy: bool,
        components: Optional[Dict[str, bool]] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """创建标准的健康检查响应"""
        response = {
            "service": service_name,
            "status": (
                ServiceConstants.STATUS_HEALTHY
                if is_healthy
                else ServiceConstants.STATUS_UNHEALTHY
            ),
            "healthy": is_healthy,
            "timestamp": datetime.now().isoformat(),
        }

        if components:
            response["components"] = components

        if details:
            response.update(details)

        return response
