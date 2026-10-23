#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Prometheus监控数据服务
"""

import asyncio
from datetime import datetime
import logging
import math
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from app.common.constants import HttpStatusCodes
from app.common.exceptions import ExternalServiceError, HistoricalDataError
from app.config.settings import config
from app.services.base import BaseService

logger = logging.getLogger("aiops.prometheus")


class PrometheusService(BaseService):
    """
    Prometheus监控数据服务 - 提供指标查询和数据获取功能
    """

    # API端点常量
    API_QUERY = "/api/v1/query"
    API_QUERY_RANGE = "/api/v1/query_range"
    API_LABEL_VALUES = "/api/v1/label/{}/values"

    def __init__(self) -> None:
        super().__init__("prometheus")
        self.base_url = config.prometheus.url
        self.timeout = config.prometheus.timeout
        self.logger.info(f"初始化Prometheus服务: {self.base_url}")

    async def _do_initialize(self) -> None:
        """初始化Prometheus服务"""
        # 测试连接
        await self._test_connection()

    async def _do_health_check(self) -> bool:
        """健康检查"""
        try:
            response = await asyncio.to_thread(
                requests.get,
                f"{self.base_url}{self.API_QUERY}",
                params={"query": "up"},
                timeout=self.timeout,
            )
            return response.status_code == HttpStatusCodes.OK
        except Exception:
            return False

    async def _test_connection(self) -> None:
        """测试Prometheus连接"""
        try:
            response = await asyncio.to_thread(
                requests.get,
                f"{self.base_url}{self.API_QUERY}",
                params={"query": "up"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            self.logger.info("Prometheus连接测试成功")
        except Exception as e:
            self.logger.error(f"Prometheus连接测试失败: {str(e)}")
            raise

    async def query_range(
        self, query: str, start_time: datetime, end_time: datetime, step: str = "1m"
    ) -> Optional[pd.DataFrame]:
        try:
            url = f"{self.base_url}{self.API_QUERY_RANGE}"
            params = {
                "query": query,
                "start": start_time.timestamp(),
                "end": end_time.timestamp(),
                "step": step,
            }

            self.logger.debug(f"查询Prometheus: {query}")
            response = await asyncio.to_thread(requests.get, url, params=params, timeout=self.timeout)
            response.raise_for_status()

            data = response.json()
            if data["status"] != "success":
                if data.get("errorType") == "bad_data":
                    raise HistoricalDataError("PromQL query was rejected; check the metric query")
                raise ExternalServiceError("prometheus", "Upstream query did not succeed")
            if not data["data"]["result"]:
                self.logger.warning(f"Prometheus查询无结果: {query}")
                return None

            # 处理多个时间序列
            all_series = []
            for result in data["data"]["result"]:
                if not result.get("values"):
                    continue

                timestamps = [
                    datetime.utcfromtimestamp(float(val[0])) for val in result["values"]
                ]
                values = []

                for val in result["values"]:
                    try:
                        value = float(val[1])
                        values.append(value if math.isfinite(value) else float("nan"))
                    except (ValueError, TypeError):
                        values.append(float("nan"))

                series_df = pd.DataFrame(
                    {"value": values}, index=pd.DatetimeIndex(timestamps)
                )

                # 添加标签信息
                labels = result.get("metric", {})
                for label, value in labels.items():
                    series_df[f"label_{label}"] = value

                all_series.append(series_df)

            if all_series:
                # 保留采样时间、缺失值及标签，不插值或隐式聚合不同序列
                return pd.concat(all_series, ignore_index=False).sort_index()

            return None

        except (HistoricalDataError, ExternalServiceError):
            raise
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in {400, 422}:
                raise HistoricalDataError("PromQL query was rejected; check the metric query") from e
            raise ExternalServiceError("prometheus", "Upstream HTTP request failed") from e
        except requests.exceptions.Timeout as e:
            raise ExternalServiceError("prometheus", "Upstream query timed out") from e
        except requests.exceptions.RequestException as e:
            raise ExternalServiceError("prometheus", "Upstream connection failed") from e
        except Exception as e:
            raise ExternalServiceError("prometheus", "Upstream response could not be parsed") from e

    async def query_instant(
        self, query: str, timestamp: Optional[datetime] = None
    ) -> Optional[List[Dict]]:
        """查询Prometheus即时数据"""
        try:
            url = f"{self.base_url}{self.API_QUERY}"
            params = {"query": query}

            if timestamp:
                params["time"] = timestamp.timestamp()

            self.logger.debug(f"即时查询Prometheus: {query}")
            response = await asyncio.to_thread(requests.get, url, params=params, timeout=self.timeout)
            response.raise_for_status()

            data = response.json()
            if data["status"] != "success" or not data["data"]["result"]:
                self.logger.warning(f"Prometheus即时查询无结果: {query}")
                return None

            return data["data"]["result"]

        except Exception as e:
            self.logger.error(f"Prometheus即时查询失败: {str(e)}")
            return None

    def _is_successful_response(self, data: Dict[str, Any]) -> bool:
        """
        检查Prometheus响应是否成功

        Args:
            data: Prometheus API响应数据

        Returns:
            是否成功
        """
        return (
            data.get("status") == "success"
            and data.get("data", {}).get("result") is not None
            and len(data["data"]["result"]) > 0
        )

    async def get_available_metrics(self) -> List[str]:
        """获取可用的监控指标"""
        try:
            url = f"{self.base_url}/api/v1/label/__name__/values"
            response = await asyncio.to_thread(requests.get, url, timeout=self.timeout)
            response.raise_for_status()

            data = response.json()
            if data["status"] == "success":
                metrics = sorted(data["data"])
                self.logger.info(f"获取到 {len(metrics)} 个可用指标")
                return metrics

            return []

        except Exception as e:
            self.logger.error(f"获取可用指标失败: {str(e)}")
            return []

    def is_healthy(self) -> bool:
        """检查Prometheus健康状态"""
        try:
            url = f"{self.base_url}/-/healthy"
            response = requests.get(url, timeout=self.timeout)
            is_healthy = response.status_code == 200
            self.logger.debug(f"Prometheus健康状态: {is_healthy}")
            return is_healthy
        except Exception as e:
            self.logger.error(f"Prometheus健康检查失败: {str(e)}")
            return False

    async def health_check(self) -> bool:
        """异步健康检查方法 - 为RCA模块提供兼容接口"""
        return await asyncio.to_thread(self.is_healthy)

    async def get_metric_metadata(self, metric_name: str) -> Optional[Dict[str, Any]]:
        """获取指标元数据"""
        try:
            url = f"{self.base_url}/api/v1/metadata"
            params = {"metric": metric_name}

            response = await asyncio.to_thread(requests.get, url, params=params, timeout=self.timeout)
            response.raise_for_status()

            data = response.json()
            if data["status"] == "success" and data["data"]:
                return data["data"].get(metric_name, [{}])[0]

            return None

        except Exception as e:
            self.logger.error(f"获取指标元数据失败: {str(e)}")
            return None

    async def get_service_info(self) -> Dict[str, Any]:
        """获取服务信息"""
        return {
            "service_name": self.service_name,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "healthy": await self.health_check(),
            "initialized": self.is_initialized(),
            "api_endpoints": {
                "query": self.API_QUERY,
                "query_range": self.API_QUERY_RANGE,
                "label_values": self.API_LABEL_VALUES,
            },
            "supported_operations": [
                "query_range",
                "query_instant",
                "get_available_metrics",
                "get_metric_metadata",
                "health_check",
            ],
        }
