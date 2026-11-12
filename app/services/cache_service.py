#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 缓存聚合服务 - 提供跨服务的缓存统计/健康/清理/配置能力
"""

from datetime import datetime
import logging
from typing import Any, Dict, Optional

from app.core.cache.cache_config import cache_monitor
from app.services.base import BaseService
from app.services.prediction_service import PredictionService
from app.services.rca_service import RCAService

logger = logging.getLogger("aiops.services.cache")


class CacheService(BaseService):
    """缓存聚合服务，聚合预测与RCA服务的缓存信息与操作"""

    def __init__(self) -> None:
        super().__init__("cache")
        self._prediction_service: Optional[PredictionService] = None
        self._rca_service: Optional[RCAService] = None

    async def _do_initialize(self) -> None:
        # 懒初始化具体服务
        self._prediction_service = PredictionService()
        self._rca_service = RCAService()

    async def _do_health_check(self) -> bool:
        # 聚合子服务健康状态
        try:
            if self._prediction_service and self._rca_service:
                pred_ok = await self._prediction_service.health_check()
                rca_ok = await self._rca_service.health_check()
                return bool(pred_ok or rca_ok)
            return False
        except Exception:
            return False

    async def get_cache_stats(self) -> Dict[str, Any]:
        """获取系统缓存统计信息"""
        try:
            # 初始化子服务
            await self._prediction_service.initialize()
            await self._rca_service.initialize()

            stats: Dict[str, Any] = {}

            # 预测服务缓存统计
            if (
                hasattr(self._prediction_service, "_cache_manager")
                and self._prediction_service._cache_manager
            ):  # noqa: SLF001
                pred_stats = self._prediction_service._cache_manager.get_stats()  # noqa: SLF001
                stats["prediction_service"] = {
                    "status": "active",
                    "stats": pred_stats,
                    "cache_prefix": self._prediction_service._cache_manager.cache_prefix,  # noqa: SLF001
                    "default_ttl": self._prediction_service._cache_manager.default_ttl,  # noqa: SLF001
                    "max_cache_size": self._prediction_service._cache_manager.max_cache_size,  # noqa: SLF001
                }
            else:
                stats["prediction_service"] = {
                    "status": "inactive",
                    "reason": "cache_manager_not_initialized",
                }

            # RCA服务缓存统计
            if (
                hasattr(self._rca_service, "_cache_manager")
                and self._rca_service._cache_manager
            ):  # noqa: SLF001
                rca_stats = self._rca_service._cache_manager.get_stats()  # noqa: SLF001
                stats["rca_service"] = {
                    "status": "active",
                    "stats": rca_stats,
                    "cache_prefix": self._rca_service._cache_manager.cache_prefix,  # noqa: SLF001
                    "default_ttl": self._rca_service._cache_manager.default_ttl,  # noqa: SLF001
                    "max_cache_size": self._rca_service._cache_manager.max_cache_size,  # noqa: SLF001
                }
            else:
                stats["rca_service"] = {
                    "status": "inactive",
                    "reason": "cache_manager_not_initialized",
                }

            # 全局缓存监控
            monitor_stats = cache_monitor.get_cache_stats()
            performance_insights = cache_monitor.get_performance_insights()

            return {
                "timestamp": datetime.now().isoformat(),
                "service_stats": stats,
                "monitor_stats": monitor_stats,
                "performance_insights": performance_insights,
                "overall_status": "healthy"
                if any(s.get("status") == "active" for s in stats.values())
                else "unhealthy",
            }
        except Exception as e:
            logger.error("获取缓存统计失败，异常类型: %s", type(e).__name__)
            raise

    async def cache_health_check(self) -> Dict[str, Any]:
        """缓存系统健康检查"""
        try:
            health_info: Dict[str, Any] = {
                "timestamp": datetime.now().isoformat(),
                "overall_status": "healthy",
                "services": {},
            }

            services_healthy = 0
            total_services = 0

            # 预测服务
            try:
                await self._prediction_service.initialize()
                if (
                    hasattr(self._prediction_service, "_cache_manager")
                    and self._prediction_service._cache_manager
                ):  # noqa: SLF001
                    pred_health = self._prediction_service._cache_manager.health_check()  # noqa: SLF001
                    health_info["services"]["prediction"] = pred_health
                    if (
                        (pred_health.get("status") == "healthy")
                        if isinstance(pred_health, dict)
                        else bool(pred_health)
                    ):
                        services_healthy += 1
                else:
                    health_info["services"]["prediction"] = {
                        "status": "unavailable",
                        "redis_connected": False,
                        "error": "cache_manager_not_initialized",
                    }
                total_services += 1
            except Exception:
                health_info["services"]["prediction"] = {
                    "status": "error",
                    "redis_connected": False,
                    "error": "缓存健康检查失败",
                }
                total_services += 1

            # RCA服务
            try:
                await self._rca_service.initialize()
                if (
                    hasattr(self._rca_service, "_cache_manager")
                    and self._rca_service._cache_manager
                ):  # noqa: SLF001
                    rca_health = self._rca_service._cache_manager.health_check()  # noqa: SLF001
                    health_info["services"]["rca"] = rca_health
                    if (
                        (rca_health.get("status") == "healthy")
                        if isinstance(rca_health, dict)
                        else bool(rca_health)
                    ):
                        services_healthy += 1
                else:
                    health_info["services"]["rca"] = {
                        "status": "unavailable",
                        "redis_connected": False,
                        "error": "cache_manager_not_initialized",
                    }
                total_services += 1
            except Exception:
                health_info["services"]["rca"] = {
                    "status": "error",
                    "redis_connected": False,
                    "error": "缓存健康检查失败",
                }
                total_services += 1

            # 总体状态
            if services_healthy == total_services:
                health_info["overall_status"] = "healthy"
            elif services_healthy > 0:
                health_info["overall_status"] = "degraded"
            else:
                health_info["overall_status"] = "unhealthy"

            health_info["summary"] = {
                "healthy_services": services_healthy,
                "total_services": total_services,
                "health_percentage": round(services_healthy / total_services * 100, 1)
                if total_services > 0
                else 0,
            }

            return health_info
        except Exception as e:
            logger.error("缓存健康检查失败，异常类型: %s", type(e).__name__)
            raise

    async def clear_cache(self, service: str, pattern: Optional[str]) -> Dict[str, Any]:
        """清空指定服务的缓存"""
        results: Dict[str, Any] = {}

        if service in ["prediction", "all"]:
            await self._prediction_service.initialize()
            if (
                hasattr(self._prediction_service, "_cache_manager")
                and self._prediction_service._cache_manager
            ):  # noqa: SLF001
                if pattern:
                    result = self._prediction_service._cache_manager.clear_pattern(
                        pattern
                    )  # noqa: SLF001
                else:
                    result = self._prediction_service._cache_manager.clear_all()  # noqa: SLF001
                results["prediction"] = result
            else:
                results["prediction"] = {
                    "success": False,
                    "message": "cache_manager_not_available",
                }

        if service in ["rca", "all"]:
            await self._rca_service.initialize()
            if (
                hasattr(self._rca_service, "_cache_manager")
                and self._rca_service._cache_manager
            ):  # noqa: SLF001
                if pattern:
                    result = self._rca_service._cache_manager.clear_pattern(pattern)  # noqa: SLF001
                else:
                    result = self._rca_service._cache_manager.clear_all()  # noqa: SLF001
                results["rca"] = result
            else:
                results["rca"] = {
                    "success": False,
                    "message": "cache_manager_not_available",
                }

        return results

    async def get_cache_performance(self) -> Dict[str, Any]:
        """获取缓存性能报告"""
        performance_insights = cache_monitor.get_performance_insights()
        cache_stats = cache_monitor.get_cache_stats()

        total_requests = sum(
            stats.get("total_requests", 0) for stats in cache_stats.values()
        )
        total_hits = sum(stats.get("cache_hits", 0) for stats in cache_stats.values())
        overall_hit_rate = (
            (total_hits / total_requests * 100) if total_requests > 0 else 0
        )

        if overall_hit_rate >= 80:
            performance_grade = "A"
            performance_desc = "优秀"
        elif overall_hit_rate >= 60:
            performance_grade = "B"
            performance_desc = "良好"
        elif overall_hit_rate >= 40:
            performance_grade = "C"
            performance_desc = "一般"
        else:
            performance_grade = "D"
            performance_desc = "需要优化"

        recommendations = []
        for cache_type, stats in cache_stats.items():
            hit_rate = stats.get("hit_rate", 0)
            if hit_rate < 50:
                recommendations.append(
                    f"{cache_type}: 命中率过低({hit_rate:.1f}%)，建议检查缓存键生成逻辑"
                )
            elif hit_rate < 70:
                recommendations.append(
                    f"{cache_type}: 命中率偏低({hit_rate:.1f}%)，建议调整TTL配置"
                )

        if not recommendations:
            recommendations.append("缓存性能良好，无需特别优化")

        return {
            "timestamp": datetime.now().isoformat(),
            "overall_metrics": {
                "total_requests": total_requests,
                "total_hits": total_hits,
                "overall_hit_rate": round(overall_hit_rate, 2),
                "performance_grade": performance_grade,
                "performance_description": performance_desc,
            },
            "performance_insights": performance_insights,
            "cache_stats_by_type": cache_stats,
            "recommendations": recommendations,
            "cache_effectiveness": performance_insights.get(
                "cache_effectiveness", "unknown"
            ),
        }

    async def get_cache_config(self) -> Dict[str, Any]:
        """获取缓存配置信息（包含服务配置）"""
        from app.core.cache.cache_config import CacheStrategy, CacheType

        config_info: Dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "cache_types": [cache_type.value for cache_type in CacheType],
            "ttl_config": {
                cache_type.value: CacheStrategy.CACHE_TTL_CONFIG[cache_type]
                for cache_type in CacheType
            },
            "priority_config": {
                cache_type.value: CacheStrategy.CACHE_PRIORITY_CONFIG[cache_type]
                for cache_type in CacheType
            },
            "prefix_config": {
                cache_type.value: CacheStrategy.CACHE_PREFIX_CONFIG[cache_type]
                for cache_type in CacheType
            },
            "compression_thresholds": {
                cache_type.value: CacheStrategy.get_cache_compression_threshold(
                    cache_type
                )
                for cache_type in CacheType
            },
        }

        service_configs: Dict[str, Any] = {}

        try:
            await self._prediction_service.initialize()
            if (
                hasattr(self._prediction_service, "_cache_manager")
                and self._prediction_service._cache_manager
            ):  # noqa: SLF001
                service_configs["prediction"] = {
                    "cache_prefix": self._prediction_service._cache_manager.cache_prefix,  # noqa: SLF001
                    "default_ttl": self._prediction_service._cache_manager.default_ttl,  # noqa: SLF001
                    "max_cache_size": self._prediction_service._cache_manager.max_cache_size,  # noqa: SLF001
                    "enable_compression": self._prediction_service._cache_manager.enable_compression,  # noqa: SLF001
                }
        except Exception:
            service_configs["prediction"] = {"status": "not_available"}

        try:
            await self._rca_service.initialize()
            if (
                hasattr(self._rca_service, "_cache_manager")
                and self._rca_service._cache_manager
            ):  # noqa: SLF001
                service_configs["rca"] = {
                    "cache_prefix": self._rca_service._cache_manager.cache_prefix,  # noqa: SLF001
                    "default_ttl": self._rca_service._cache_manager.default_ttl,  # noqa: SLF001
                    "max_cache_size": self._rca_service._cache_manager.max_cache_size,  # noqa: SLF001
                    "enable_compression": self._rca_service._cache_manager.enable_compression,  # noqa: SLF001
                }
        except Exception:
            service_configs["rca"] = {"status": "not_available"}

        config_info["service_configs"] = service_configs
        return config_info
