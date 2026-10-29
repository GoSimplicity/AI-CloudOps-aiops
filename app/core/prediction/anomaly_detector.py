#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps异常检测器 - 检测预测数据中的异常
"""

import logging
from typing import Any, Dict, List, Tuple

import numpy as np
from scipy import stats

from app.models import AnomalyPrediction, PredictionDataPoint

logger = logging.getLogger("aiops.core.anomaly_detector")


class AnomalyDetector:
    """异常检测器"""

    def __init__(self):
        self.methods = {
            "zscore": self._zscore_detection,
            "iqr": self._iqr_detection,
            "isolation": self._isolation_forest_detection,
            "mad": self._mad_detection,
        }

    async def detect_anomalies(
        self,
        predictions: List[PredictionDataPoint],
        sensitivity: float = None,
        method: str = None,
    ) -> List[AnomalyPrediction]:
        """检测预测数据中的异常"""

        if not predictions:
            return []

        try:
            # 从配置文件获取默认值
            from app.config.settings import config

            anomaly_config = getattr(config.prediction, "anomaly_detection", {})

            # 使用传入值或配置文件默认值
            if sensitivity is None:
                sensitivity = anomaly_config.get("default_sensitivity", 0.8)
            if method is None:
                method = anomaly_config.get("default_method", "zscore")

            # 提取预测值
            values = np.array([p.predicted_value for p in predictions])
            timestamps = [p.timestamp for p in predictions]

            # 选择检测方法
            detection_method = self.methods.get(method, self._zscore_detection)

            # 执行异常检测
            anomaly_indices, anomaly_scores = detection_method(values, sensitivity)

            # 构建异常预测结果
            anomalies = []
            for idx in anomaly_indices:
                # 计算期望值（使用邻近点的平均值）
                expected_value = self._calculate_expected_value(values, idx)

                # 评估影响等级
                impact_level = self._assess_impact_level(
                    values[idx], expected_value, anomaly_scores[idx]
                )

                # 确定异常类型
                anomaly_type = self._determine_anomaly_type(
                    values[idx], expected_value, idx, values
                )

                # 确保异常分数在0-1范围内
                normalized_score = min(1.0, max(0.0, float(anomaly_scores[idx]) / 5.0))

                anomaly = AnomalyPrediction(
                    timestamp=timestamps[idx],
                    anomaly_score=normalized_score,
                    anomaly_type=anomaly_type,
                    impact_level=impact_level,
                    predicted_value=float(values[idx]),
                    expected_value=float(expected_value),
                )

                anomalies.append(anomaly)

            return anomalies

        except Exception as e:
            logger.error("异常检测失败: %s", type(e).__name__)
            raise

    def _zscore_detection(
        self, values: np.ndarray, sensitivity: float
    ) -> Tuple[List[int], np.ndarray]:
        """Z-score异常检测"""

        # 检查数据标准差，避免precision loss警告
        std_dev = np.std(values)
        if std_dev < 1e-10:  # 数据几乎相同
            # 使用MAD检测作为替代
            logger.warning("数据标准差过小，使用MAD检测替代Z-score检测")
            return self._mad_detection(values, sensitivity)

        # 计算Z-score，使用ddof=1避免精度问题
        try:
            z_scores = np.abs(stats.zscore(values, ddof=1))
        except RuntimeWarning:
            # 如果仍然有警告，使用自定义计算
            mean_val = np.mean(values)
            z_scores = np.abs((values - mean_val) / (std_dev + 1e-10))

        # 根据敏感度设置阈值
        threshold = 3.0 * (1.5 - sensitivity)  # sensitivity越高，阈值越低

        # 找出异常点
        anomaly_indices = np.where(z_scores > threshold)[0].tolist()

        return anomaly_indices, z_scores

    def _iqr_detection(
        self, values: np.ndarray, sensitivity: float
    ) -> Tuple[List[int], np.ndarray]:
        """IQR（四分位距）异常检测"""

        q1 = np.percentile(values, 25)
        q3 = np.percentile(values, 75)
        iqr = q3 - q1

        # 根据敏感度调整系数
        k = 1.5 * (2 - sensitivity)

        lower_bound = q1 - k * iqr
        upper_bound = q3 + k * iqr

        # 计算异常分数
        scores = np.zeros(len(values))
        for i, v in enumerate(values):
            if v < lower_bound:
                scores[i] = abs(v - lower_bound) / iqr
            elif v > upper_bound:
                scores[i] = abs(v - upper_bound) / iqr

        # 找出异常点
        anomaly_indices = np.where((values < lower_bound) | (values > upper_bound))[
            0
        ].tolist()

        return anomaly_indices, scores

    def _isolation_forest_detection(
        self, values: np.ndarray, sensitivity: float
    ) -> Tuple[List[int], np.ndarray]:
        """简化版Isolation Forest检测"""

        # 这是一个简化实现
        # 实际应用中应使用sklearn的IsolationForest

        # 使用MAD作为简化方案
        return self._mad_detection(values, sensitivity)

    def _mad_detection(
        self, values: np.ndarray, sensitivity: float
    ) -> Tuple[List[int], np.ndarray]:
        """MAD（中位数绝对偏差）异常检测"""

        median = np.median(values)
        mad = np.median(np.abs(values - median))

        # 避免除零
        if mad == 0:
            mad = 1.0

        # 计算修正的Z-score
        modified_z_scores = 0.6745 * (values - median) / mad

        # 根据敏感度设置阈值
        threshold = 3.5 * (1.5 - sensitivity)

        # 异常分数
        scores = np.abs(modified_z_scores)

        # 找出异常点
        anomaly_indices = np.where(scores > threshold)[0].tolist()

        return anomaly_indices, scores

    def _calculate_expected_value(self, values: np.ndarray, idx: int) -> float:
        """计算期望值"""

        # 使用邻近点的中位数作为期望值
        window_size = 5
        start = max(0, idx - window_size)
        end = min(len(values), idx + window_size + 1)

        # 排除当前点
        neighbor_values = np.concatenate([values[start:idx], values[idx + 1 : end]])

        if len(neighbor_values) > 0:
            return np.median(neighbor_values)
        else:
            return np.median(values)

    def _assess_impact_level(
        self, anomaly_value: float, expected_value: float, anomaly_score: float
    ) -> str:
        """评估异常影响等级"""

        # 计算偏离程度
        if expected_value != 0:
            deviation_ratio = abs(anomaly_value - expected_value) / expected_value
        else:
            deviation_ratio = abs(anomaly_value)

        # 综合考虑异常分数和偏离程度
        combined_score = (anomaly_score + deviation_ratio * 10) / 2

        if combined_score > 5:
            return "critical"
        elif combined_score > 3:
            return "high"
        elif combined_score > 1.5:
            return "medium"
        else:
            return "low"

    def _determine_anomaly_type(
        self, anomaly_value: float, expected_value: float, idx: int, values: np.ndarray
    ) -> str:
        """确定异常类型"""

        # 判断是峰值还是谷值
        if anomaly_value > expected_value:
            base_type = "spike"
        else:
            base_type = "dip"

        # 检查是否为持续异常
        if self._is_sustained_anomaly(idx, values):
            return f"sustained_{base_type}"

        # 检查是否为突变
        if self._is_sudden_change(idx, values):
            return f"sudden_{base_type}"

        # 检查是否为渐变
        if self._is_gradual_change(idx, values):
            return f"gradual_{base_type}"

        return base_type

    def _is_sustained_anomaly(self, idx: int, values: np.ndarray) -> bool:
        """检查是否为持续异常"""

        if idx < 2 or idx >= len(values) - 2:
            return False

        # 检查前后的值是否也异常
        threshold = np.std(values) * 2
        mean_val = np.mean(values)

        sustained = True
        for i in range(max(0, idx - 2), min(len(values), idx + 3)):
            if abs(values[i] - mean_val) < threshold:
                sustained = False
                break

        return sustained

    def _is_sudden_change(self, idx: int, values: np.ndarray) -> bool:
        """检查是否为突变"""

        if idx < 1 or idx >= len(values) - 1:
            return False

        # 计算变化率
        prev_change = abs(values[idx] - values[idx - 1])
        next_change = abs(values[idx + 1] - values[idx])

        avg_change = np.mean(np.abs(np.diff(values)))

        # 如果变化率远大于平均值，则为突变
        return prev_change > avg_change * 3 or next_change > avg_change * 3

    def _is_gradual_change(self, idx: int, values: np.ndarray) -> bool:
        """检查是否为渐变"""

        if idx < 3 or idx >= len(values) - 3:
            return False

        # 检查趋势的单调性
        window = values[idx - 3 : idx + 4]
        diffs = np.diff(window)

        # 如果差值符号一致，说明是渐变
        return np.all(diffs > 0) or np.all(diffs < 0)

    def analyze_anomaly_patterns(
        self, anomalies: List[AnomalyPrediction]
    ) -> Dict[str, Any]:
        """分析异常模式"""

        if not anomalies:
            return {"total_anomalies": 0, "patterns": []}

        # 统计各类型异常
        type_counts = {}
        impact_counts = {"low": 0, "medium": 0, "high": 0, "critical": 0}

        for anomaly in anomalies:
            type_counts[anomaly.anomaly_type] = (
                type_counts.get(anomaly.anomaly_type, 0) + 1
            )
            impact_counts[anomaly.impact_level] += 1

        # 检测异常簇
        clusters = self._detect_anomaly_clusters(anomalies)

        return {
            "total_anomalies": len(anomalies),
            "type_distribution": type_counts,
            "impact_distribution": impact_counts,
            "clusters": clusters,
            "severity_score": self._calculate_severity_score(anomalies),
            "patterns": self._identify_patterns(anomalies),
        }

    def _detect_anomaly_clusters(
        self, anomalies: List[AnomalyPrediction]
    ) -> List[Dict[str, Any]]:
        """检测异常簇"""

        if not anomalies:
            return []

        # 按时间排序
        sorted_anomalies = sorted(anomalies, key=lambda x: x.timestamp)

        clusters = []
        current_cluster = [sorted_anomalies[0]]

        for i in range(1, len(sorted_anomalies)):
            # 如果时间间隔小于1小时，认为是同一簇
            time_diff = (
                sorted_anomalies[i].timestamp - current_cluster[-1].timestamp
            ).total_seconds() / 3600

            if time_diff <= 1:
                current_cluster.append(sorted_anomalies[i])
            else:
                if len(current_cluster) >= 2:
                    clusters.append(
                        {
                            "start_time": current_cluster[0].timestamp,
                            "end_time": current_cluster[-1].timestamp,
                            "anomaly_count": len(current_cluster),
                            "max_severity": max(
                                a.anomaly_score for a in current_cluster
                            ),
                        }
                    )
                current_cluster = [sorted_anomalies[i]]

        # 处理最后一个簇
        if len(current_cluster) >= 2:
            clusters.append(
                {
                    "start_time": current_cluster[0].timestamp,
                    "end_time": current_cluster[-1].timestamp,
                    "anomaly_count": len(current_cluster),
                    "max_severity": max(a.anomaly_score for a in current_cluster),
                }
            )

        return clusters

    def _calculate_severity_score(self, anomalies: List[AnomalyPrediction]) -> float:
        """计算整体严重程度分数"""

        if not anomalies:
            return 0.0

        impact_weights = {"low": 0.25, "medium": 0.5, "high": 0.75, "critical": 1.0}

        total_score = sum(
            impact_weights.get(a.impact_level, 0.5) * a.anomaly_score for a in anomalies
        )

        # 归一化到0-1范围
        return min(1.0, total_score / (len(anomalies) * 3))

    def _identify_patterns(self, anomalies: List[AnomalyPrediction]) -> List[str]:
        """识别异常模式"""

        patterns = []

        if not anomalies:
            return patterns

        # 检查是否有周期性异常
        timestamps = [a.timestamp for a in anomalies]
        hours = [t.hour for t in timestamps]

        # 检查特定时间段的异常
        if len([h for h in hours if 9 <= h <= 17]) > len(anomalies) * 0.7:
            patterns.append("business_hours_concentration")
        elif len([h for h in hours if h < 6 or h > 22]) > len(anomalies) * 0.7:
            patterns.append("off_hours_concentration")

        # 检查异常类型模式
        types = [a.anomaly_type for a in anomalies]
        if types.count("spike") > len(types) * 0.7:
            patterns.append("predominantly_spikes")
        elif types.count("dip") > len(types) * 0.7:
            patterns.append("predominantly_dips")

        # 检查严重程度趋势
        critical_count = len(
            [a for a in anomalies if a.impact_level in ["high", "critical"]]
        )
        if critical_count > len(anomalies) * 0.5:
            patterns.append("high_severity_concentration")

        return patterns
