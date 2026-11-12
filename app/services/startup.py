#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps应用启动服务
"""

import asyncio
import time
from typing import Any, Dict, List

from .base import BaseService


class StartupService(BaseService):
    """
    启动服务 - 管理应用启动流程和服务预热
    """

    def __init__(self) -> None:
        super().__init__("startup")
        self.start_time = time.time()
        self._managed_services: List[BaseService] = []
        self._service_instances: Dict[str, Any] = {}

    async def _do_initialize(self) -> None:
        """初始化启动服务"""
        self.logger.info("启动服务初始化完成")

    async def _do_health_check(self) -> bool:
        """启动服务健康检查"""
        return True

    async def warmup_services(self) -> Dict[str, Any]:
        """
        预热关键服务

        Returns:
            预热结果报告
        """
        self.logger.info("开始预热关键服务...")

        warmup_results = {
            "started_at": time.time(),
            "services": {},
            "total_time": 0,
            "success": True,
        }

        # 预热智能助手服务
        assistant_result = await self._warmup_assistant()
        warmup_results["services"]["assistant"] = assistant_result

        # 可以添加其他服务的预热逻辑
        # warmup_results["services"]["prediction"] = await self._warmup_prediction()
        # warmup_results["services"]["rca"] = await self._warmup_rca()

        warmup_results["total_time"] = time.time() - warmup_results["started_at"]
        warmup_results["success"] = all(
            service.get("success", False)
            for service in warmup_results["services"].values()
        )

        self.logger.info(f"服务预热完成，总耗时: {warmup_results['total_time']:.2f}秒")
        return warmup_results

    async def _warmup_assistant(self) -> Dict[str, Any]:
        """
        预热智能助手服务

        Returns:
            预热结果
        """
        result = {
            "service": "assistant",
            "success": False,
            "error": None,
            "duration": 0,
        }

        try:
            start_time = time.time()

            # 动态导入避免循环依赖
            from ..core.agents.enterprise_assistant import get_enterprise_assistant

            self.logger.info("正在初始化AI-CloudOps智能助手...")

            # 单次初始化，避免重复调用
            try:
                agent = await get_enterprise_assistant()
            except Exception as e:
                self.logger.warning("AI-CloudOps智能助手初始化失败，异常类型: %s", type(e).__name__)
                agent = None

            result["duration"] = time.time() - start_time

            if agent is not None:
                result["success"] = True
                self.logger.info(f"智能助手预热完成，耗时: {result['duration']:.2f}秒")
            else:
                result["error"] = "智能助手初始化失败"
                self.logger.warning("智能助手预热失败，将在首次使用时初始化")

        except Exception as e:
            result["duration"] = (
                time.time() - start_time if "start_time" in locals() else 0
            )
            result["error"] = "智能助手预热失败"
            self.logger.warning("智能助手预热失败，异常类型: %s，将在首次使用时初始化", type(e).__name__)

        return result

    def get_uptime(self) -> float:
        """
        获取应用运行时间

        Returns:
            运行时间（秒）
        """
        return time.time() - self.start_time

    def get_startup_info(self) -> Dict[str, Any]:
        """
        获取启动信息

        Returns:
            启动信息字典
        """
        return {
            "start_time": self.start_time,
            "uptime": self.get_uptime(),
            "initialized": self.is_initialized(),
            "healthy": self.is_healthy(),
            "managed_services": len(self._managed_services),
        }

    def register_service(self, service: BaseService) -> None:
        """注册需要管理的服务"""
        if service not in self._managed_services:
            self._managed_services.append(service)
            self.logger.info(f"注册服务: {service.service_name}")

    def register_service_instance(self, name: str, instance: Any) -> None:
        """注册服务实例（非BaseService类型）"""
        self._service_instances[name] = instance
        self.logger.info(f"注册服务实例: {name}")

    async def cleanup_all_services(self) -> Dict[str, Any]:
        """清理所有注册的服务"""
        cleanup_results = {
            "started_at": time.time(),
            "services": {},
            "instances": {},
            "total_time": 0,
            "success": True,
        }

        self.logger.info("开始清理所有服务...")

        # 清理BaseService类型的服务
        for service in reversed(self._managed_services):  # 反向清理
            service_result = await self._cleanup_service(service)
            cleanup_results["services"][service.service_name] = service_result

        # 清理其他服务实例
        for name, instance in self._service_instances.items():
            instance_result = await self._cleanup_instance(name, instance)
            cleanup_results["instances"][name] = instance_result

        cleanup_results["total_time"] = time.time() - cleanup_results["started_at"]
        cleanup_results["success"] = all(
            result.get("success", False)
            for result in {
                **cleanup_results["services"],
                **cleanup_results["instances"],
            }.values()
        )

        self.logger.info(f"服务清理完成，总耗时: {cleanup_results['total_time']:.2f}秒")
        return cleanup_results

    async def _cleanup_service(self, service: BaseService) -> Dict[str, Any]:
        """清理单个BaseService"""
        result = {
            "service": service.service_name,
            "success": False,
            "error": None,
            "duration": 0,
        }

        try:
            start_time = time.time()
            await service.cleanup()
            result["duration"] = time.time() - start_time
            result["success"] = True
            self.logger.info(
                f"服务 {service.service_name} 清理成功，耗时: {result['duration']:.2f}秒"
            )
        except Exception as e:
            result["duration"] = (
                time.time() - start_time if "start_time" in locals() else 0
            )
            result["error"] = "服务清理失败"
            self.logger.error("服务 %s 清理失败，异常类型: %s", service.service_name, type(e).__name__)

        return result

    async def _cleanup_instance(self, name: str, instance: Any) -> Dict[str, Any]:
        """清理单个服务实例"""
        result = {
            "instance": name,
            "success": False,
            "error": None,
            "duration": 0,
        }

        try:
            start_time = time.time()

            # 尝试不同的清理方法
            if hasattr(instance, "shutdown") and callable(
                getattr(instance, "shutdown")
            ):
                if asyncio.iscoroutinefunction(instance.shutdown):
                    await instance.shutdown()
                else:
                    instance.shutdown()
            elif hasattr(instance, "close") and callable(getattr(instance, "close")):
                if asyncio.iscoroutinefunction(instance.close):
                    await instance.close()
                else:
                    instance.close()
            elif hasattr(instance, "cleanup") and callable(
                getattr(instance, "cleanup")
            ):
                if asyncio.iscoroutinefunction(instance.cleanup):
                    await instance.cleanup()
                else:
                    instance.cleanup()
            else:
                self.logger.debug(f"实例 {name} 没有可用的清理方法")

            result["duration"] = time.time() - start_time
            result["success"] = True
            self.logger.info(f"实例 {name} 清理成功，耗时: {result['duration']:.2f}秒")
        except Exception as e:
            result["duration"] = (
                time.time() - start_time if "start_time" in locals() else 0
            )
            result["error"] = "实例清理失败"
            self.logger.error("实例 %s 清理失败，异常类型: %s", name, type(e).__name__)

        return result

    async def get_services_status(self) -> Dict[str, Any]:
        """获取所有服务状态"""
        services_status = {}

        # BaseService类型的服务状态
        for service in self._managed_services:
            services_status[service.service_name] = {
                "type": "BaseService",
                "initialized": service.is_initialized(),
                "healthy": service.is_healthy(),
            }

        # 其他服务实例状态
        for name, instance in self._service_instances.items():
            status = {"type": "Instance", "available": True}

            # 尝试获取健康状态
            try:
                if hasattr(instance, "health_check"):
                    if asyncio.iscoroutinefunction(instance.health_check):
                        status["healthy"] = await instance.health_check()
                    else:
                        status["healthy"] = instance.health_check()
                elif hasattr(instance, "is_healthy"):
                    status["healthy"] = instance.is_healthy()
            except Exception as e:
                status["healthy"] = False
                status["error"] = "获取服务状态失败"
                self.logger.warning("获取实例 %s 状态失败，异常类型: %s", name, type(e).__name__)

            services_status[name] = status

        return services_status
