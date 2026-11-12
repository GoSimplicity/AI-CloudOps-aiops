#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 缓存管理API接口
"""

from datetime import datetime
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query

from app.api.decorators import api_response, log_api_call
from app.common.exceptions import (
    AIOpsException,
    ConfigurationError,
    ValidationError as DomainValidationError,
)
from app.models import BaseResponse
from app.services.cache_service import CacheService
from app.services.factory import ServiceFactory

logger = logging.getLogger("aiops.api.cache")

router = APIRouter(tags=["cache"])
cache_service = None


async def get_cache_service() -> CacheService:
    global cache_service
    if cache_service is None:
        cache_service = await ServiceFactory.get_service("cache", CacheService)
    return cache_service


@router.get(
    "/stats",
    summary="获取缓存统计信息",
    response_model=BaseResponse,
)
@api_response("获取缓存统计信息")
@log_api_call(log_request=False)
async def get_cache_stats() -> Dict[str, Any]:
    """获取系统缓存统计信息"""
    try:
        await (await get_cache_service()).initialize()
        return await (await get_cache_service()).get_cache_stats()
    except (AIOpsException, DomainValidationError) as e:
        raise e
    except Exception as e:
        logger.error("获取缓存统计失败，异常类型: %s", type(e).__name__)
        raise ConfigurationError("获取缓存统计失败") from e


@router.post(
    "/clear",
    summary="清空缓存",
    response_model=BaseResponse,
)
@api_response("清空缓存")
@log_api_call(log_request=True)
async def clear_cache(
    service: str = Query(..., description="服务名称: prediction, rca, 或 all"),
    pattern: str = Query(None, description="可选的模式匹配，用于部分清空"),
) -> Dict[str, Any]:
    """清空指定服务的缓存"""
    try:
        await (await get_cache_service()).initialize()
        if service not in ["prediction", "rca", "all"]:
            raise DomainValidationError("service", "必须是: prediction, rca, 或 all")

        results = await (await get_cache_service()).clear_cache(service, pattern)
        total_cleared = sum(r.get("cleared_count", 0) for r in results.values())
        all_successful = all(r.get("success", False) for r in results.values())

        return {
            "timestamp": datetime.now().isoformat(),
            "service": service,
            "pattern": pattern,
            "results": results,
            "summary": {
                "total_cleared": total_cleared,
                "all_successful": all_successful,
            },
        }
    except (AIOpsException, DomainValidationError):
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("清空缓存失败，异常类型: %s", type(e).__name__)
        raise ConfigurationError("清空缓存失败") from e


@router.get(
    "/performance",
    summary="获取缓存性能报告",
    response_model=BaseResponse,
)
@api_response("获取缓存性能报告")
@log_api_call(log_request=False)
async def get_cache_performance() -> Dict[str, Any]:
    """获取详细的缓存性能报告"""
    try:
        await (await get_cache_service()).initialize()
        return await (await get_cache_service()).get_cache_performance()
    except (AIOpsException, DomainValidationError) as e:
        raise e
    except Exception as e:
        logger.error("获取缓存性能报告失败，异常类型: %s", type(e).__name__)
        raise ConfigurationError("获取缓存性能报告失败") from e


@router.get(
    "/config",
    summary="获取缓存配置信息",
    response_model=BaseResponse,
)
@api_response("获取缓存配置信息")
@log_api_call(log_request=False)
async def get_cache_config() -> Dict[str, Any]:
    """获取当前缓存配置信息"""
    try:
        await (await get_cache_service()).initialize()
        return await (await get_cache_service()).get_cache_config()
    except (AIOpsException, DomainValidationError) as e:
        raise e
    except Exception as e:
        logger.error("获取缓存配置失败，异常类型: %s", type(e).__name__)
        raise ConfigurationError("获取缓存配置失败") from e


__all__ = ["router"]
