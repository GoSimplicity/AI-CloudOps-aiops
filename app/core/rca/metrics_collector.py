#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps指标数据收集器
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from scipy import stats

from app.config.settings import CONFIG, config
from app.core.interfaces.prometheus_client import (
    NullPrometheusClient,
    PrometheusClient,
)
from app.models.rca_models import MetricData

from .base_collector import BaseDataCollector, CollectionBatch


class MetricsCollector(BaseDataCollector):
    """优化的Prometheus指标数据收集器"""

    def __init__(
        self,
        config_dict: Optional[Dict[str, Any]] = None,
        prometheus_client: Optional[PrometheusClient] = None,
    ):
        super().__init__("metrics", config_dict)
        self.prometheus: PrometheusClient = prometheus_client or NullPrometheusClient()

        # 从配置文件读取RCA指标配置
        self.rca_config = config.rca
        # 从原始配置字典获取详细配置
        self.metrics_config = CONFIG.get("rca", {}).get("metrics", {})

        # 缓存配置
        cache_config = self.metrics_config
        self._query_cache = {}
        self._anomaly_cache = {}
        self.cache_size = cache_config.get("cache_size", 100)
        self.cache_ttl = cache_config.get("cache_ttl", 60)
        self.anomaly_cache_size = cache_config.get("anomaly_cache_size", 500)

        # 指标配置
        self.default_metrics = (
            config_dict.get("default_metrics")
            if config_dict
            else self.metrics_config.get("default_metrics", [])
        )
        self.step_interval = (
            config_dict.get("step_interval")
            if config_dict
            else self.metrics_config.get("step_interval", "1m")
        )
        self.concurrent_limit = self.metrics_config.get("concurrent_limit", 3)

        # 阈值配置
        self.thresholds = self.metrics_config.get("thresholds", {})

    async def _do_initialize(self) -> None:
        """初始化Prometheus客户端连接（通过依赖注入）"""
        try:
            # 如果是空实现，直接返回，让上层服务感知健康状态
            if isinstance(self.prometheus, NullPrometheusClient):
                self.logger.warning("未注入Prometheus客户端，将以降级模式运行")
                return

            # 重试健康检查
            for attempt in range(3):
                try:
                    if await self.prometheus.health_check():
                        self.logger.info("Prometheus客户端健康检查通过")
                        return
                    else:
                        self.logger.warning(
                            f"Prometheus健康检查失败，尝试 {attempt + 1}/3"
                        )
                        if attempt < 2:
                            await asyncio.sleep(2**attempt)
                except Exception as e:
                    self.logger.warning(
                        f"Prometheus客户端连接尝试 {attempt + 1}/3 失败: {type(e).__name__}"
                    )
                    if attempt < 2:
                        await asyncio.sleep(2**attempt)

            raise RuntimeError("无法连接到Prometheus客户端，已尝试3次")

        except Exception as e:
            self.logger.error("初始化Prometheus客户端时发生错误: %s", type(e).__name__)
            raise

    async def collect(
        self, namespace: str, start_time: datetime, end_time: datetime, **kwargs
    ) -> List[MetricData]:
        """
        收集指标数据（修复版本）

        Args:
            namespace: Kubernetes命名空间
            start_time: 开始时间
            end_time: 结束时间
            **kwargs: 其他参数

        Returns:
            List[MetricData]: 指标数据列表
        """
        try:
            await self.initialize()
            self._ensure_initialized()
            if isinstance(self.prometheus, NullPrometheusClient):
                raise RuntimeError("Prometheus client unavailable")

            # 确保时间有时区
            start_time = self._ensure_timezone(start_time)
            end_time = self._ensure_timezone(end_time)

            metrics_to_collect = kwargs.get("metrics")
            if not metrics_to_collect:
                metrics_to_collect = self.default_metrics

            # 安全检查指标列表
            if not metrics_to_collect:
                self.logger.debug("没有指定要收集的指标，返回空列表")
                return []

            # 并发收集所有指标
            collected_metrics = []

            # 限制并发数
            semaphore = asyncio.Semaphore(self.concurrent_limit)

            async def collect_with_limit(metric_name):
                async with semaphore:
                    return await self._collect_single_metric(
                        metric_name, namespace, start_time, end_time
                    )

            # 创建任务
            tasks = [collect_with_limit(metric) for metric in metrics_to_collect]

            # 执行任务
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 处理结果
            failed = 0
            for result in results:
                if isinstance(result, list):
                    collected_metrics.extend(result)
                elif isinstance(result, Exception):
                    failed += 1
                    self.logger.warning("指标采集失败: %s", type(result).__name__)

            self.logger.info(f"成功收集 {len(collected_metrics)} 个指标数据")
            return CollectionBatch(collected_metrics, succeeded=len(results) - failed, failed=failed)

        except Exception as e:
            self.logger.error("指标收集失败: %s", type(e).__name__)
            raise

    async def _collect_single_metric(
        self, metric_name: str, namespace: str, start_time: datetime, end_time: datetime
    ) -> List[MetricData]:
        """收集单个指标"""
        try:
            # 检查缓存
            cache_key = f"{metric_name}:{namespace}:{start_time}:{end_time}"
            if cache_key in self._query_cache:
                cache_entry = self._query_cache[cache_key]
                if (datetime.now() - cache_entry["timestamp"]).seconds < self.cache_ttl:
                    return cache_entry["data"]

            # 构建查询
            query = self._build_optimized_query(metric_name, namespace)

            # 执行查询
            self.logger.info(f"执行Prometheus查询: {query}")
            data = await self.prometheus.query_range(
                query=query,
                start_time=start_time,
                end_time=end_time,
                step=self.step_interval,
            )

            self.logger.info(f"查询 {metric_name} 返回数据类型: {type(data)}")
            if data is not None:
                self.logger.info(
                    f"查询 {metric_name} 返回数据形状: {data.shape if hasattr(data, 'shape') else 'N/A'}"
                )

            if data is None:
                return []

            if hasattr(data, "empty") and data.empty:
                self.logger.warning(f"Prometheus查询返回空数据: {metric_name}")
                return []

            # 处理数据
            metric_data_list = self._process_metric_data_optimized(metric_name, data)

            # 只缓存非空结果
            if metric_data_list:
                self._query_cache[cache_key] = {
                    "data": metric_data_list,
                    "timestamp": datetime.now(),
                }

            # 限制缓存大小
            if len(self._query_cache) > self.cache_size:
                # 删除最老的缓存项
                oldest_key = min(
                    self._query_cache.keys(),
                    key=lambda k: self._query_cache[k]["timestamp"],
                )
                del self._query_cache[oldest_key]

            return metric_data_list

        except Exception as e:
            self.logger.warning("指标采集失败: %s", type(e).__name__)
            raise

    def _build_optimized_query(self, metric_name: str, namespace: str) -> str:
        """构建优化的Prometheus查询"""
        # 基础查询
        base_query = metric_name

        # 构建标签过滤器
        filters = []

        # 更广泛的命名空间过滤策略
        namespace_metrics = [
            "container_",
            "kube_",
            "apiserver_",
            "etcd_",
            "kubelet_",
            "nginx_",
            "mysql_",
            "redis_",
            "postgres_",
        ]

        # 检查是否需要命名空间过滤
        needs_namespace = any(prefix in metric_name for prefix in namespace_metrics)

        # 特殊处理某些指标
        if needs_namespace:
            if "apiserver_" in metric_name:
                # API服务器指标可能没有namespace标签，尝试其他过滤方式
                pass  # 保持原始查询，不添加namespace过滤
            elif "kube_" in metric_name:
                filters.append(f'namespace="{namespace}"')
            elif "container_" in metric_name:
                filters.append(f'namespace="{namespace}"')

        # 组合查询
        if filters:
            query = f"{base_query}{{{','.join(filters)}}}"
        else:
            query = base_query

        # 添加聚合（如果需要）
        if "container_" in metric_name:
            # 按Pod聚合
            query = f"sum by (pod, container) ({query})"
        elif "node_" in metric_name:
            # 按节点聚合
            query = f"sum by (instance) ({query})"
        elif "apiserver_" in metric_name:
            # API服务器指标按请求类型聚合
            query = f"sum by (verb, resource) ({query})"

        self.logger.debug(f"构建查询: {metric_name} -> {query}")
        return query

    def _process_metric_data_optimized(
        self, metric_name: str, data: pd.DataFrame
    ) -> List[MetricData]:
        """优化的指标数据处理"""
        metric_data_list = []

        try:
            # 安全检查
            if data is None:
                return []
            if hasattr(data, "empty") and data.empty:
                return []

            # 无效样本不进入响应和统计，保留真实零值及采样缺口
            numeric_values = pd.to_numeric(data["value"], errors="coerce").to_numpy()
            finite = np.isfinite(numeric_values)
            data = data.iloc[np.flatnonzero(finite)].copy()
            data["value"] = numeric_values[finite]
            if data.empty:
                return []

            # 智能分组
            group_cols = self._determine_grouping_columns(data.columns)

            if group_cols:
                # 按标签分组处理
                for group_values, group_data in data.groupby(group_cols):
                    if len(group_data) > 0:
                        labels = dict(zip(group_cols, group_values))
                        metric_data = self._create_metric_data_fast(
                            f"{metric_name}|{self._format_labels(labels)}",
                            group_data,
                            labels,
                        )
                        metric_data_list.append(metric_data)
            else:
                # 整体处理
                metric_data = self._create_metric_data_fast(metric_name, data, {})
                metric_data_list.append(metric_data)

        except Exception as e:
            self.logger.error("处理指标数据失败: %s", type(e).__name__)
            raise

        return metric_data_list

    def _determine_grouping_columns(self, columns: List[str]) -> List[str]:
        """确定分组列"""
        # 优先级顺序的标签
        priority_labels = [
            "label_pod",
            "label_container",
            "label_instance",
            "label_node",
        ]

        group_cols = []
        for label in priority_labels:
            if label in columns:
                group_cols.append(label)
                if len(group_cols) >= 2:  # 最多两个分组维度
                    break

        return group_cols

    def _create_metric_data_fast(
        self, name: str, data: pd.DataFrame, labels: Dict[str, str]
    ) -> MetricData:
        """快速创建MetricData对象"""
        # 查询结果使用 DatetimeIndex，兼容已存在的显式 timestamp 列
        observed_times = data["timestamp"] if "timestamp" in data.columns else data.index
        timestamps = pd.DatetimeIndex(pd.to_datetime(observed_times, utc=True)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        values = [{"timestamp": ts, "value": float(val)} for ts, val in zip(timestamps, data["value"].values)]
        value_series = data["value"]

        # 异常分数（简化计算）
        anomaly_score = 0.0
        if len(value_series) >= 10:
            anomaly_score = self._calculate_anomaly_score_fast(value_series)

        # 趋势分析（简化）
        trend = self._analyze_trend_fast(value_series)

        return MetricData(
            name=name,
            values=values,
            labels=labels,
            anomaly_score=anomaly_score,
            trend=trend,
        )

    def _calculate_anomaly_score_fast(self, values: pd.Series) -> float:
        """快速计算异常分数"""
        try:
            # 使用缓存的异常分数
            values_hash = hash(tuple(values.head(20)))  # 使用前20个值的哈希
            if values_hash in self._anomaly_cache:
                return self._anomaly_cache[values_hash]

            scores = []

            # 1. 快速Z-Score检测 - 降低阈值
            z_scores = np.abs(stats.zscore(values))
            max_z = np.max(z_scores)
            if max_z > 2:  # 从3降低到2
                scores.append(min(max_z / 3.0, 1.0))  # 调整标准化系数

            # 2. 快速IQR检测
            q1, q3 = np.percentile(values, [25, 75])
            iqr = q3 - q1
            if iqr > 0:
                outlier_ratio = np.sum(
                    (values < q1 - 1.5 * iqr) | (values > q3 + 1.5 * iqr)
                ) / len(values)
                scores.append(min(outlier_ratio * 2, 1.0))  # 增加权重

            # 3. 变化率检测
            if len(values) > 1:
                diff = np.diff(values)
                if len(diff) > 0:
                    change_rate = np.std(diff) / (np.mean(np.abs(values)) + 1e-10)
                    scores.append(min(change_rate * 1.5, 1.0))  # 增加敏感度

            # 4. 添加移动平均异常检测
            if len(values) > 10:
                ma = values.rolling(window=5, min_periods=1).mean()
                deviation = np.abs(values - ma) / (ma + 1e-10)
                max_deviation = deviation.max()
                if max_deviation > 0.5:
                    scores.append(min(max_deviation, 1.0))

            # 计算最终分数
            if scores:
                # 使用加权平均，给更高的分数更大权重
                final_score = max(scores) * 0.6 + np.mean(scores) * 0.4
            else:
                final_score = 0.0

            # 缓存结果
            if len(self._anomaly_cache) < self.anomaly_cache_size:
                self._anomaly_cache[values_hash] = final_score

            return float(min(max(final_score, 0.0), 1.0))

        except Exception:
            return 0.0

    def _analyze_trend_fast(self, values: pd.Series) -> str:
        """快速趋势分析"""
        try:
            if len(values) < 5:
                return "stable"

            # 使用简单的线性回归
            x = np.arange(len(values))
            slope, _, r_value, _, _ = stats.linregress(x, values)

            # 计算相对变化
            mean_val = np.mean(values)
            if mean_val != 0:
                relative_change = abs(slope) / abs(mean_val)

                # 判断趋势
                if abs(r_value) > 0.5 and relative_change > 0.1:
                    return "increasing" if slope > 0 else "decreasing"

            return "stable"

        except Exception:
            return "stable"

    def _format_labels(self, labels: Dict[str, str]) -> str:
        """格式化标签为字符串"""
        return "|".join([f"{k}:{v}" for k, v in labels.items()])

    def _ensure_timezone(self, dt: datetime) -> datetime:
        """确保datetime有时区信息"""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    async def health_check(self) -> bool:
        """健康检查"""
        try:
            if not self.prometheus:
                return False
            return await self.prometheus.health_check()
        except Exception as e:
            self.logger.error(f"指标收集器健康检查失败: {e}")
            return False
