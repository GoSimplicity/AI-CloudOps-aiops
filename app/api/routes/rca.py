#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 根因分析API路由 - 提供异常检测、相关性分析和根本原因识别功能
"""

from fastapi import APIRouter, HTTPException
from datetime import datetime, timedelta
import asyncio
import logging
from app.core.rca.analyzer import RCAAnalyzer
from app.models.request_models import RCARequest
from app.utils.validators import validate_time_range, validate_metric_list
from app.config.settings import config
from app.models.response_models import APIResponse
from typing import Optional

logger = logging.getLogger("aiops.rca")

router = APIRouter(tags=["rca"])

# 初始化分析器
rca_analyzer = RCAAnalyzer()

@router.post('/rca')
async def root_cause_analysis(request_data: RCARequest):
    """
    根因分析接口
    """
    try:
        # 验证时间范围
        if not validate_time_range(request_data.start_time, request_data.end_time):
            raise HTTPException(status_code=400, detail="无效的时间范围")

        # 检查时间范围限制
        time_diff = (request_data.end_time - request_data.start_time).total_seconds() / 60
        max_minutes = getattr(config, 'rca_max_time_range_minutes', 1440)  # 默认24小时
        
        if time_diff > max_minutes:
            raise HTTPException(
                status_code=400, 
                detail=f"时间范围不能超过{max_minutes}分钟"
            )

        # 验证指标列表
        if request_data.metrics and not validate_metric_list(request_data.metrics):
            raise HTTPException(status_code=400, detail="无效的指标列表")

        logger.info(f"开始根因分析: {request_data.start_time} 到 {request_data.end_time}")

        # 调用根因分析服务
        try:
            analysis_result = await asyncio.to_thread(
                rca_analyzer.analyze,
                request_data.start_time,
                request_data.end_time,
                request_data.metrics,
                request_data.threshold
            )
        except Exception as analysis_error:
            logger.error(f"根因分析执行失败: {str(analysis_error)}")
            raise HTTPException(status_code=500, detail="根因分析执行失败")

        return APIResponse(code=0, message="根因分析完成", data=analysis_result).dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"根因分析请求处理失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"根因分析失败: {str(e)}")

@router.post('/rca/anomaly')
async def detect_anomaly(
    start_time: datetime,
    end_time: datetime,
    metrics: Optional[list] = None,
    sensitivity: Optional[float] = 0.8
):
    """
    异常检测接口
    """
    try:
        # 验证时间范围
        if not validate_time_range(start_time, end_time):
            raise HTTPException(status_code=400, detail="无效的时间范围")

        # 验证敏感度参数
        if sensitivity < 0.1 or sensitivity > 1.0:
            raise HTTPException(status_code=400, detail="敏感度参数必须在0.1-1.0之间")

        logger.info(f"开始异常检测: {start_time} 到 {end_time}")

        # 调用异常检测服务
        anomalies = await asyncio.to_thread(
            rca_analyzer.detect_anomalies,
            start_time,
            end_time,
            metrics,
            sensitivity
        )

        return APIResponse(
            code=0,
            message="异常检测完成",
            data={
                "anomalies": anomalies,
                "detection_period": {
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat()
                },
                "sensitivity": sensitivity
            }
        ).dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"异常检测失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"异常检测失败: {str(e)}")

@router.post('/rca/correlation')
async def analyze_correlation(
    start_time: datetime,
    end_time: datetime,
    target_metric: str,
    metrics: Optional[list] = None
):
    """
    相关性分析接口
    """
    try:
        # 验证时间范围
        if not validate_time_range(start_time, end_time):
            raise HTTPException(status_code=400, detail="无效的时间范围")

        # 验证目标指标
        if not target_metric or not target_metric.strip():
            raise HTTPException(status_code=400, detail="必须指定目标指标")

        logger.info(f"开始相关性分析: 目标指标={target_metric}")

        # 调用相关性分析服务
        correlations = await asyncio.to_thread(
            rca_analyzer.analyze_correlations,
            start_time,
            end_time,
            target_metric,
            metrics
        )

        return APIResponse(
            code=0,
            message="相关性分析完成",
            data={
                "target_metric": target_metric,
                "correlations": correlations,
                "analysis_period": {
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat()
                }
            }
        ).dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"相关性分析失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"相关性分析失败: {str(e)}")

@router.post('/rca/timeline')
async def generate_timeline(
    start_time: datetime,
    end_time: datetime,
    events: Optional[list] = None
):
    """
    事件时间线生成接口
    """
    try:
        # 验证时间范围
        if not validate_time_range(start_time, end_time):
            raise HTTPException(status_code=400, detail="无效的时间范围")

        logger.info(f"生成事件时间线: {start_time} 到 {end_time}")

        # 调用时间线生成服务
        timeline = await asyncio.to_thread(
            rca_analyzer.generate_timeline,
            start_time,
            end_time,
            events
        )

        return APIResponse(
            code=0,
            message="事件时间线生成完成",
            data={
                "timeline": timeline,
                "period": {
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat()
                }
            }
        ).dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"事件时间线生成失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"事件时间线生成失败: {str(e)}")

@router.get('/rca/history')
async def get_analysis_history(limit: Optional[int] = 50):
    """
    获取分析历史记录接口
    """
    try:
        # 验证限制参数
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=400, detail="limit参数必须在1-500之间")

        logger.info(f"获取分析历史记录，限制数量: {limit}")

        # 获取历史记录
        history = await asyncio.to_thread(rca_analyzer.get_analysis_history, limit)

        return APIResponse(
            code=0,
            message="分析历史记录获取成功",
            data={
                "history": history,
                "count": len(history),
                "limit": limit
            }
        ).dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取分析历史记录失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取分析历史记录失败: {str(e)}")

@router.get('/rca/health')
async def rca_health():
    """
    RCA服务健康检查接口
    """
    try:
        # 检查RCA服务健康状态
        health_status = await asyncio.to_thread(rca_analyzer.is_healthy)

        return APIResponse(
            code=0,
            message="RCA服务健康检查完成",
            data={
                "healthy": health_status,
                "timestamp": datetime.utcnow().isoformat(),
                "service": "rca"
            }
        ).dict()

    except Exception as e:
        logger.error(f"RCA服务健康检查失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"RCA服务健康检查失败: {str(e)}")