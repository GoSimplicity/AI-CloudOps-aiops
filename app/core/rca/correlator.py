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
from typing import Any, Dict, List, Tuple, Optional

import numpy as np
import pandas as pd
from scipy.stats import pearsonr

from app.config.settings import config

logger = logging.getLogger("aiops.correlator")


class CorrelationAnalyzer:
    def __init__(self, correlation_threshold: float = None):
        self.correlation_threshold = (
            correlation_threshold or config.rca.correlation_threshold
        )
        logger.info(f"相关性分析器初始化完成, 阈值: {self.correlation_threshold}")

    async def analyze_correlations(
        self, metrics_data: Dict[str, pd.DataFrame]
    ) -> Dict[str, List[Tuple[str, float]]]:
        """分析指标间的相关性"""
        try:
            if len(metrics_data) < 2:
                logger.warning("指标数量少于2个，无法进行相关性分析")
                return {}

            # 准备数据
            combined_df = self._prepare_correlation_data(metrics_data)
            if combined_df.empty:
                logger.warning("准备相关性分析数据失败")
                return {}

            logger.info(f"准备了 {len(combined_df.columns)} 个指标进行相关性分析")

            # 计算相关性矩阵
            correlation_matrix = self._calculate_correlation_matrix(combined_df)

            # 提取显著相关性
            significant_correlations = self._extract_significant_correlations(
                correlation_matrix
            )

            logger.info(f"发现 {len(significant_correlations)} 组显著相关性")
            return significant_correlations

        except Exception as e:
            logger.error(f"相关性分析失败: {str(e)}")
            return {}

    async def analyze_correlations_with_cross_lag(
        self, metrics_data: Dict[str, pd.DataFrame], max_lags: int = 10
    ) -> Dict[str, Any]:
        """返回同时包含零时滞相关与跨时滞最优相关的结果。"""
        base = await self.analyze_correlations(metrics_data)
        cross: Dict[str, List[Tuple[str, int, float]]] = {}
        try:
            combined_df = self._prepare_correlation_data(metrics_data)
            metrics = list(combined_df.columns)
            for i, m1 in enumerate(metrics):
                values_for_m1: List[Tuple[str, int, float]] = []
                for j, m2 in enumerate(metrics):
                    if i == j:
                        continue
                    cc = await self.calculate_cross_correlation(
                        combined_df[m1], combined_df[m2], max_lags=max_lags
                    )
                    if cc:
                        best_lag = max(cc.keys(), key=lambda k: abs(cc[k]))
                        best_corr = float(cc[best_lag])
                        if abs(best_corr) >= self.correlation_threshold:
                            values_for_m1.append((m2, int(best_lag), round(best_corr, 3)))
                if values_for_m1:
                    values_for_m1.sort(key=lambda x: abs(x[2]), reverse=True)
                    cross[m1] = values_for_m1[:5]
        except Exception as e:
            logger.error(f"跨时滞相关分析失败: {str(e)}")
            cross = {}

        return {"correlations": base, "cross_correlations": cross}

    # ---------------------------- 新增：面向基础指标的聚合管线 ---------------------------- #

    @staticmethod
    def _is_counter_metric(metric_base_name: str) -> bool:
        base = str(metric_base_name).split("|")[0]
        return (
            base.endswith("_total")
            or base.endswith("_count")
            or base.endswith("_seconds_total")
        )

    def _aggregate_views(
        self,
        metrics_data: Dict[str, pd.DataFrame],
        *,
        group_by: Optional[str] = None,
        group_value: Optional[str] = None,
    ) -> pd.DataFrame:
        """将多条标签序列按“基础指标名”聚合为若干列视图。

        - group_by: None/global 或 "namespace"
        - 按基础指标名聚合，单个基础指标下的多条序列以按行均值合并
        - 对计数器型指标应用差分以近似 rate
        - 移除方差极小/为0的列
        """
        per_metric_series: Dict[str, List[pd.Series]] = {}

        for series_name, df in (metrics_data or {}).items():
            if not isinstance(df, pd.DataFrame) or df.empty or "value" not in df.columns:
                continue
            base_name = str(series_name).split("|")[0]

            # 命名空间过滤
            if group_by == "namespace":
                try:
                    if "label_namespace" not in df.columns:
                        # 该序列没有命名空间标签，跳过命名空间视图
                        continue
                    # 标签在整列恒定，取首值即可
                    ns_value = None
                    try:
                        ns_value = df["label_namespace"].iloc[0]
                    except Exception:
                        ns_value = None
                    if group_value is None or str(ns_value) != str(group_value):
                        continue
                except Exception:
                    continue

            # 取值序列并确保数值型
            series = pd.to_numeric(df["value"], errors="coerce").astype(float)
            # 统一为等间隔时间索引（如已处理则保持不变）
            if not isinstance(series.index, pd.DatetimeIndex):
                try:
                    series.index = pd.date_range(
                        start=pd.Timestamp.now(tz="UTC")
                        - pd.Timedelta(minutes=len(series) - 1),
                        periods=len(series),
                        freq="1min",
                        tz="UTC",
                    )
                except Exception:
                    pass
            series = series.resample("1min").mean()

            # 计数器 -> 增量
            try:
                if self._is_counter_metric(base_name):
                    series = series.diff().clip(lower=0.0)
            except Exception:
                pass

            if base_name not in per_metric_series:
                per_metric_series[base_name] = []
            per_metric_series[base_name].append(series)

        # 聚合为每个基础指标一列（按行均值）
        aggregated_columns: Dict[str, pd.Series] = {}
        for base_name, parts in per_metric_series.items():
            try:
                if not parts:
                    continue
                if len(parts) == 1:
                    aggregated = parts[0]
                else:
                    aggregated = pd.concat(parts, axis=1).mean(axis=1, skipna=True)
                # 清理：全空则丢弃
                if aggregated.isna().all():
                    continue
                # 清理：方差过小丢弃
                try:
                    if float(aggregated.var()) <= 1e-12:
                        logger.warning(f"移除方差为0的基础指标: {base_name}")
                        continue
                except Exception:
                    pass
                aggregated_columns[base_name] = aggregated
            except Exception:
                continue

        if not aggregated_columns:
            return pd.DataFrame()

        df = pd.DataFrame(aggregated_columns)
        # 归一化（z-score）
        try:
            for col in list(df.columns):
                s = pd.to_numeric(df[col], errors="coerce")
                std = float(s.std()) if s.std() is not None else 0.0
                if std > 0:
                    df[col] = (s - float(s.mean())) / std
                else:
                    # 标准差为0时，保留原值（后续相关性会被过滤）
                    df[col] = s
        except Exception:
            pass

        # 最终再移除全空列
        for col in list(df.columns):
            try:
                if pd.isna(df[col]).all():
                    df = df.drop(columns=[col])
            except Exception:
                continue
        return df

    def _extract_significant_correlations_base(
        self, correlation_matrix: pd.DataFrame, threshold: float
    ) -> Dict[str, List[Tuple[str, float]]]:
        result: Dict[str, List[Tuple[str, float]]] = {}
        try:
            for metric in correlation_matrix.columns:
                pairs: List[Tuple[str, float]] = []
                for other in correlation_matrix.columns:
                    if metric == other:
                        continue
                    value = float(correlation_matrix.loc[metric, other])
                    if not np.isnan(value) and abs(value) >= float(threshold):
                        pairs.append((other, round(value, 3)))
                if pairs:
                    pairs.sort(key=lambda x: abs(x[1]), reverse=True)
                    result[metric] = pairs[:10]
        except Exception:
            return {}
        return result

    def analyze_target_with_views(
        self,
        metrics_data: Dict[str, pd.DataFrame],
        *,
        target_metric: str,
        namespace: Optional[str],
        thresholds: Optional[List[float]] = None,
    ) -> Dict[str, List[Tuple[str, float]]]:
        """智能相关性：按视图（namespace->global）与阈值序列逐步兜底，返回目标基础指标的Top对。

        返回形如：{ target_base_metric: [(other_base, corr), ...] }
        """
        if not target_metric:
            return {}
        target_base = str(target_metric).split("|")[0]
        thresholds = thresholds or [
            float(self.correlation_threshold or 0.7),
            0.5,
            0.3,
            0.0,
        ]

        view_plan: List[Tuple[Optional[str], Optional[str]]] = []
        if namespace:
            view_plan.append(("namespace", str(namespace)))
        view_plan.append((None, None))  # global 视图

        for group_by, group_value in view_plan:
            try:
                df_view = self._aggregate_views(
                    metrics_data, group_by=group_by, group_value=group_value
                )
                if df_view.empty or target_base not in df_view.columns:
                    continue
                corr = df_view.corr(method="pearson").fillna(0)
                for thr in thresholds:
                    out = self._extract_significant_correlations_base(corr, thr)
                    candidates = out.get(target_base) or []
                    if candidates:
                        return {target_base: candidates[:10]}
            except Exception:
                continue

        # 全部失败，返回显式空
        return {target_base: []}

    def _prepare_correlation_data(
        self, metrics_data: Dict[str, pd.DataFrame]
    ) -> pd.DataFrame:
        """准备相关性分析数据"""
        try:
            series_list: List[pd.Series] = []

            for metric_name, df in metrics_data.items():
                if "value" in df.columns and not df.empty:
                    clean_series = df["value"].dropna()
                    if len(clean_series) > 5:
                        if not isinstance(clean_series.index, pd.DatetimeIndex):
                            clean_series.index = pd.date_range(
                                start=pd.Timestamp.now(tz="UTC")
                                - pd.Timedelta(minutes=len(clean_series) - 1),
                                periods=len(clean_series),
                                freq="1min",
                                tz="UTC",
                            )
                        clean_series.name = metric_name
                        series_list.append(clean_series)

            if not series_list:
                return pd.DataFrame()

            combined_df = pd.concat(series_list, axis=1, join="outer")

            if not combined_df.empty and isinstance(combined_df.index, pd.DatetimeIndex):
                combined_df = combined_df.resample("1min").mean()

            # 将疑似计数器型指标转换为每分钟增量（近似 rate），提升相关性可解释性
            try:
                def _is_counter(col_name: str) -> bool:
                    base = str(col_name).split("|")[0]
                    return base.endswith("_total") or base.endswith("_count") or base.endswith("_seconds_total")

                for col in list(combined_df.columns):
                    if _is_counter(col):
                        numeric = pd.to_numeric(combined_df[col], errors="coerce")
                        diff_vals = numeric.diff().clip(lower=0.0)
                        combined_df[col] = diff_vals
            except Exception:
                pass

            # 放宽缺失过滤：仅当行全空时才剔除，避免因稀疏导致全部被丢弃
            combined_df = combined_df.dropna(how="all")

            # 保守移除方差为0：允许极小波动通过（方差阈值>0且极小）
            for col in list(combined_df.columns):
                try:
                    if pd.isna(combined_df[col]).all():
                        combined_df = combined_df.drop(columns=[col])
                        continue
                    variance = float(combined_df[col].var())
                    if variance <= 1e-12:
                        combined_df = combined_df.drop(columns=[col])
                        logger.warning(f"移除方差为0的指标: {col}")
                except Exception:
                    continue

            logger.info(f"相关性分析数据准备完成: {combined_df.shape}")
            return combined_df

        except Exception as e:
            logger.error(f"准备相关性数据失败: {str(e)}")
            return pd.DataFrame()

    def _calculate_correlation_matrix(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算相关性矩阵"""
        try:
            correlation_matrix = df.corr(method="pearson")
            correlation_matrix = correlation_matrix.fillna(0)
            logger.debug(f"相关性矩阵计算完成: {correlation_matrix.shape}")
            return correlation_matrix
        except Exception as e:
            logger.error(f"计算相关性矩阵失败: {str(e)}")
            return pd.DataFrame()

    def _extract_significant_correlations(
        self, correlation_matrix: pd.DataFrame
    ) -> Dict[str, List[Tuple[str, float]]]:
        """提取显著相关性"""
        significant_correlations: Dict[str, List[Tuple[str, float]]] = {}
        try:
            for metric in correlation_matrix.columns:
                correlations: List[Tuple[str, float]] = []
                for other_metric in correlation_matrix.columns:
                    if metric != other_metric:
                        corr_value = correlation_matrix.loc[metric, other_metric]
                        if abs(corr_value) >= self.correlation_threshold and not np.isnan(corr_value):
                            correlations.append((other_metric, round(corr_value, 3)))
                if correlations:
                    correlations.sort(key=lambda x: abs(x[1]), reverse=True)
                    significant_correlations[metric] = correlations[:5]
            return significant_correlations
        except Exception as e:
            logger.error(f"提取显著相关性失败: {str(e)}")
            return {}

    async def calculate_cross_correlation(
        self, series1: pd.Series, series2: pd.Series, max_lags: int = 10
    ) -> Dict[int, float]:
        """计算交叉相关性（考虑时间滞后）"""
        try:
            min_length = min(len(series1), len(series2))
            if min_length < max_lags * 2:
                max_lags = min_length // 2

            series1 = series1.iloc[-min_length:]
            series2 = series2.iloc[-min_length:]

            cross_correlations: Dict[int, float] = {}
            for lag in range(-max_lags, max_lags + 1):
                try:
                    if lag < 0:
                        s1 = series1.iloc[-lag:]
                        s2 = series2.iloc[:lag] if lag != 0 else series2
                    elif lag > 0:
                        s1 = series1.iloc[:-lag] if lag != 0 else series1
                        s2 = series2.iloc[lag:]
                    else:
                        s1 = series1
                        s2 = series2

                    if len(s1) > 3 and len(s2) > 3 and len(s1) == len(s2):
                        corr, p_value = pearsonr(s1, s2)
                        if not np.isnan(corr) and p_value < 0.05:
                            cross_correlations[lag] = round(corr, 3)
                except Exception:
                    continue
            return cross_correlations
        except Exception as e:
            logger.error(f"计算交叉相关性失败: {str(e)}")
            return {}

    async def detect_causal_relationships(
        self, metrics_data: Dict[str, pd.DataFrame], max_lag: int = 5
    ) -> Dict[str, List[str]]:
        """检测因果关系（基于时间滞后的简化Granger因果检验）"""
        try:
            causal_relationships: Dict[str, List[str]] = {}
            combined_df = self._prepare_correlation_data(metrics_data)
            if combined_df.shape[1] < 2:
                return {}
            metrics = list(combined_df.columns)
            for i, metric1 in enumerate(metrics):
                potential_causes: List[str] = []
                for j, metric2 in enumerate(metrics):
                    if i != j:
                        if self._test_granger_causality(
                            combined_df[metric1], combined_df[metric2], max_lag
                        ):
                            potential_causes.append(metric2)
                if potential_causes:
                    causal_relationships[metric1] = potential_causes
            logger.info(f"检测到 {len(causal_relationships)} 组潜在因果关系")
            return causal_relationships
        except Exception as e:
            logger.error(f"检测因果关系失败: {str(e)}")
            return {}

    def _test_granger_causality(
        self, target: pd.Series, predictor: pd.Series, max_lag: int
    ) -> bool:
        """简化的Granger因果检验"""
        try:
            df = pd.DataFrame({"target": target, "predictor": predictor}).dropna()
            if len(df) < max_lag * 3:
                return False
            significant_lags = 0
            for lag in range(1, max_lag + 1):
                if len(df) > lag:
                    lagged_predictor = df["predictor"].shift(lag)
                    current_target = df["target"]
                    valid_data = pd.DataFrame(
                        {"target": current_target, "predictor": lagged_predictor}
                    ).dropna()
                    if len(valid_data) > 10:
                        corr, p_value = pearsonr(valid_data["target"], valid_data["predictor"])
                        if abs(corr) > 0.3 and p_value < 0.05:
                            significant_lags += 1
            return significant_lags >= 2
        except Exception:
            return False

    async def calculate_partial_correlations(
        self, metrics_data: Dict[str, pd.DataFrame]
    ) -> Dict[str, Dict[str, float]]:
        """计算偏相关系数"""
        try:
            combined_df = self._prepare_correlation_data(metrics_data)
            if combined_df.shape[1] < 3:
                return {}
            partial_correlations: Dict[str, Dict[str, float]] = {}
            metrics = list(combined_df.columns)
            for i, metric1 in enumerate(metrics):
                partial_correlations[metric1] = {}
                for j, metric2 in enumerate(metrics):
                    if i != j:
                        control_vars = [m for m in metrics if m != metric1 and m != metric2]
                        if control_vars:
                            partial_corr = self._calculate_partial_correlation(
                                combined_df, metric1, metric2, control_vars
                            )
                            if not np.isnan(partial_corr) and abs(partial_corr) > 0.3:
                                partial_correlations[metric1][metric2] = round(partial_corr, 3)
            return partial_correlations
        except Exception as e:
            logger.error(f"计算偏相关系数失败: {str(e)}")
            return {}

    def _calculate_partial_correlation(
        self, df: pd.DataFrame, var1: str, var2: str, control_vars: List[str]
    ) -> float:
        """计算偏相关系数"""
        try:
            from sklearn.linear_model import LinearRegression
            clean_df = df[[var1, var2] + control_vars].dropna()
            if len(clean_df) < 10:
                return np.nan
            reg1 = LinearRegression()
            reg1.fit(clean_df[control_vars], clean_df[var1])
            residual1 = clean_df[var1] - reg1.predict(clean_df[control_vars])
            reg2 = LinearRegression()
            reg2.fit(clean_df[control_vars], clean_df[var2])
            residual2 = clean_df[var2] - reg2.predict(clean_df[control_vars])
            corr, _ = pearsonr(residual1, residual2)
            return corr
        except Exception:
            return np.nan


class Correlator:
    """测试兼容别名：提供与 CorrelationAnalyzer 相同的接口，便于单元测试 patch。"""

    def __init__(self, correlation_threshold: float = None):
        self._impl = CorrelationAnalyzer(correlation_threshold)

    async def analyze_correlations(
        self, metrics_data: Dict[str, pd.DataFrame]
    ) -> Dict[str, List[Tuple[str, float]]]:
        return await self._impl.analyze_correlations(metrics_data)

    async def analyze_correlations_with_cross_lag(
        self, metrics_data: Dict[str, pd.DataFrame], max_lags: int = 10
    ) -> Dict[str, Any]:
        return await self._impl.analyze_correlations_with_cross_lag(
            metrics_data, max_lags=max_lags
        )

    async def calculate_cross_correlation(
        self, series1: pd.Series, series2: pd.Series, max_lags: int = 10
    ) -> Dict[int, float]:
        return await self._impl.calculate_cross_correlation(
            series1, series2, max_lags
        )

    async def detect_causal_relationships(
        self, metrics_data: Dict[str, pd.DataFrame], max_lag: int = 5
    ) -> Dict[str, List[str]]:
        return await self._impl.detect_causal_relationships(metrics_data, max_lag)

    async def calculate_partial_correlations(
        self, metrics_data: Dict[str, pd.DataFrame]
    ) -> Dict[str, Dict[str, float]]:
        return await self._impl.calculate_partial_correlations(metrics_data)

    # 新增：智能目标相关性（命名空间/全局聚合视图 + 阈值兜底）
    def analyze_target_with_views(
        self,
        metrics_data: Dict[str, pd.DataFrame],
        *,
        target_metric: str,
        namespace: Optional[str],
        thresholds: Optional[List[float]] = None,
    ) -> Dict[str, List[Tuple[str, float]]]:
        return self._impl.analyze_target_with_views(
            metrics_data, target_metric=target_metric, namespace=namespace, thresholds=thresholds
        )
