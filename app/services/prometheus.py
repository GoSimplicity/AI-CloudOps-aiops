#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
import httpx

from app.config.settings import config

logger = logging.getLogger("aiops.prometheus")


class PrometheusService:
    def __init__(self):
        self.base_url = config.prometheus.url
        # 统一超时：对外部接口保留配置超时，对健康检查/列表接口采用更短超时以快速失败
        self.timeout = config.prometheus.timeout
        try:
            self._fast_timeout = max(2, min(int(self.timeout), 10))
        except Exception:
            self._fast_timeout = 5
        logger.info(f"初始化Prometheus服务: {self.base_url}")

    # 新增：同步健康检查，供单元测试使用
    def check_connectivity(self) -> bool:
        try:
            url = f"{self.base_url}/api/v1/labels"
            response = requests.get(url, timeout=self._fast_timeout)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Prometheus连通性检查失败: {str(e)}")
            return False

    # 新增：同步即时查询，供单元测试使用
    def query(self, query: str) -> Optional[Dict[str, Any]]:
        try:
            url = f"{self.base_url}/api/v1/query"
            resp = requests.get(url, params={"query": query}, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Prometheus同步查询失败: {str(e)}")
            return None

    # 新增：同步范围查询，供单元测试使用
    def query_range(
        self, query: str, start: str, end: str, step: str = "1m"
    ) -> Optional[Dict[str, Any]]:
        try:
            url = f"{self.base_url}/api/v1/query_range"
            params = {"query": query, "start": start, "end": end, "step": step}
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Prometheus同步范围查询失败: {str(e)}")
            return None

    def _to_utc_datetime(self, value: Any) -> datetime:
        """将输入安全转换为时区感知的UTC datetime。

        支持:
        - datetime: 直接归一化到UTC
        - str: ISO8601（支持Z结尾），尽力解析
        - int/float: 视为epoch秒
        """
        try:
            if isinstance(value, datetime):
                return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            if isinstance(value, (int, float)):
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            if isinstance(value, str):
                text = value.strip()
                # 兼容Z时区后缀
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                try:
                    dt = datetime.fromisoformat(text)
                except Exception:
                    # 回退到 dateutil 解析（如可用）
                    try:
                        from dateutil import parser as _dateparser  # type: ignore

                        dt = _dateparser.parse(value)
                    except Exception as _:
                        raise
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
                return dt
        except Exception as e:
            raise ValueError(f"无法解析时间参数: {value!r} ({e})") from e
        raise TypeError(f"不支持的时间参数类型: {type(value)}")

    async def query_range_async(
        self, query: str, start_time: Any, end_time: Any, step: str = "1m"
    ) -> Optional[pd.DataFrame]:
        """查询Prometheus范围数据（异步）"""
        try:
            # 兼容传入 str/datetime/epoch
            start_dt = self._to_utc_datetime(start_time)
            end_dt = self._to_utc_datetime(end_time)
            url = f"{self.base_url}/api/v1/query_range"
            params = {
                "query": query,
                "start": int(start_dt.timestamp()),
                "end": int(end_dt.timestamp()),
                "step": step,
            }

            logger.debug(f"查询Prometheus: {query}")
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()

                data = response.json()
            if data["status"] != "success":
                logger.warning(
                    f"Prometheus查询失败: {query}, 状态: {data.get('status', 'unknown')}"
                )
                return None

            if not data["data"]["result"]:
                logger.warning(
                    f"Prometheus查询无结果: {query}, 时间范围: {start_dt} - {end_dt}"
                )
                return None

            logger.debug(
                f"Prometheus查询成功: {query}, 结果数: {len(data['data']['result'])}"
            )

            # 处理多个时间序列：逐条重采样，避免跨序列求均值导致异常被稀释
            all_series = []
            for result in data["data"]["result"]:
                if not result.get("values"):
                    continue

                timestamps = [
                    datetime.fromtimestamp(float(val[0])) for val in result["values"]
                ]
                values = []

                for val in result["values"]:
                    try:
                        values.append(float(val[1]))
                    except (ValueError, TypeError):
                        values.append(0.0)

                # 单条时间序列重采样到 1 分钟，保持标签不变
                series_df = pd.DataFrame(
                    {"value": values}, index=pd.DatetimeIndex(timestamps)
                )
                series_df = series_df.resample("1min").mean().ffill()

                labels = result.get("metric", {})
                for label, value in labels.items():
                    series_df[f"label_{label}"] = value

                all_series.append(series_df)

            if all_series:
                # 合并后直接返回逐序列拼接的结果，不做全局平均
                combined_df = pd.concat(all_series, ignore_index=False)
                return combined_df

            return None

        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout, requests.exceptions.Timeout):
            logger.error(f"Prometheus查询超时: {query}")
            return None
        except (httpx.HTTPError, requests.exceptions.RequestException) as e:
            logger.error(f"Prometheus请求失败: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"查询Prometheus失败: {str(e)}", exc_info=True)
            return None

    async def query_instant_async(
        self, query: str, timestamp: Optional[Any] = None
    ) -> Optional[List[Dict]]:
        """查询Prometheus即时数据（异步）"""
        try:
            url = f"{self.base_url}/api/v1/query"
            params = {"query": query}

            if timestamp:
                ts_dt = self._to_utc_datetime(timestamp)
                params["time"] = ts_dt.timestamp()

            logger.debug(f"即时查询Prometheus: {query}")
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()

                data = response.json()
            if data["status"] != "success" or not data["data"]["result"]:
                logger.warning(f"Prometheus即时查询无结果: {query}")
                return None

            return data["data"]["result"]

        except (httpx.HTTPError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            logger.error(f"Prometheus即时查询失败: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Prometheus即时查询失败: {str(e)}")
            return None

    async def get_available_metrics(self) -> List[str]:
        """获取可用的监控指标"""
        try:
            url = f"{self.base_url}/api/v1/label/__name__/values"
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url)
                response.raise_for_status()

                data = response.json()
            if data["status"] == "success":
                metrics = sorted(data["data"])
                logger.info(f"获取到 {len(metrics)} 个可用指标")
                return metrics

            return []

        except (httpx.HTTPError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            logger.error(f"获取可用指标失败: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"获取可用指标失败: {str(e)}")
            return []

    def is_healthy(self) -> bool:
        """检查Prometheus健康状态"""
        try:
            url = f"{self.base_url}/-/healthy"
            response = requests.get(url, timeout=self._fast_timeout)
            is_healthy = response.status_code == 200
            logger.debug(f"Prometheus健康状态: {is_healthy}")
            return is_healthy
        except Exception as e:
            logger.error(f"Prometheus健康检查失败: {str(e)}")
            return False

    def get_metric_labels(self, metric: str) -> List[str]:
        """获取指定指标的标签列表（简化实现）。"""
        try:
            result = self.query(metric)
            if isinstance(result, dict) and result.get("status") == "success":
                data = result.get("data")
                if isinstance(data, list):
                    return data
            return []
        except Exception:
            return []

    # 指标元数据接口暂未被使用，删除以减少冗余
