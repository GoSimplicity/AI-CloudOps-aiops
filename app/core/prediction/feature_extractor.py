#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps特征提取器 - 从时间序列数据中提取预测特征
"""

from datetime import datetime
import logging
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from app.models import PredictionType

logger = logging.getLogger("aiops.core.feature_extractor")


class FeatureExtractor:
    """特征提取器"""

    def __init__(self):
        self.feature_config = self._init_feature_config()

    def _init_feature_config(self) -> Dict[PredictionType, List[str]]:
        """初始化各预测类型的特征配置"""

        base_features = [
            "sin_time",
            "cos_time",
            "sin_day",
            "cos_day",
            "is_business_hour",
            "is_weekend",
            "is_holiday",
        ]

        return {
            PredictionType.QPS: base_features
            + [
                "QPS",
                "QPS_1h_ago",
                "QPS_1d_ago",
                "QPS_1w_ago",
                "QPS_change",
                "QPS_avg_6h",
                "QPS_std_6h",
            ],
            PredictionType.CPU: base_features
            + [
                "CPU",
                "CPU_1h_ago",
                "CPU_1d_ago",
                "CPU_1w_ago",
                "CPU_change",
                "CPU_avg_6h",
                "CPU_std_6h",
                "CPU_max_6h",
            ],
            PredictionType.MEMORY: base_features
            + [
                "MEMORY",
                "MEMORY_1h_ago",
                "MEMORY_1d_ago",
                "MEMORY_1w_ago",
                "MEMORY_change",
                "MEMORY_avg_6h",
                "MEMORY_trend",
                "MEMORY_min_6h",
            ],
            PredictionType.DISK: base_features
            + [
                "DISK",
                "DISK_1h_ago",
                "DISK_1d_ago",
                "DISK_1w_ago",
                "DISK_change",
                "DISK_avg_24h",
                "DISK_growth_rate",
                "DISK_max_24h",
            ],
        }

    async def extract_features(
        self,
        timestamp: datetime,
        current_value: float,
        historical_data: List[Dict[str, Any]],
        prediction_type: PredictionType,
    ) -> pd.DataFrame:
        """提取特征"""

        try:
            # 提取时间特征
            time_features = self._extract_time_features(timestamp)

            # 提取历史特征
            historical_features = self._extract_historical_features(
                current_value, historical_data, prediction_type
            )

            # 提取统计特征
            statistical_features = self._extract_statistical_features(
                current_value, historical_data, prediction_type
            )

            # 合并所有特征
            features = {**time_features, **historical_features, **statistical_features}

            # 根据预测类型添加主要特征
            metric_name = prediction_type.value.upper()
            features[metric_name] = current_value

            # 获取该类型所需的特征列表
            required_features = self.feature_config[prediction_type]

            # 确保所有必需的特征都存在
            for feature in required_features:
                if feature not in features:
                    # 对缺失特征使用默认值
                    features[feature] = self._get_default_value(feature, current_value)

            # 创建DataFrame并按照要求的顺序排列特征
            df = pd.DataFrame([features])[required_features]

            return df

        except Exception as e:
            logger.error("特征提取失败，异常类型: %s", type(e).__name__)
            # 返回默认特征
            return self._get_default_features(prediction_type, current_value)

    def _extract_time_features(self, timestamp: datetime) -> Dict[str, float]:
        """提取时间相关特征"""

        hour = timestamp.hour
        day_of_week = timestamp.weekday()
        day_of_month = timestamp.day
        month = timestamp.month

        # 周期性编码
        features = {
            "sin_time": np.sin(2 * np.pi * hour / 24),
            "cos_time": np.cos(2 * np.pi * hour / 24),
            "sin_day": np.sin(2 * np.pi * day_of_week / 7),
            "cos_day": np.cos(2 * np.pi * day_of_week / 7),
            "hour": hour,
            "day_of_week": day_of_week,
            "day_of_month": day_of_month,
            "month": month,
            "is_weekend": int(day_of_week >= 5),
            "is_business_hour": int(9 <= hour <= 17 and day_of_week < 5),
            "is_holiday": int(self._is_holiday(timestamp)),
        }

        return features

    def _extract_historical_features(
        self,
        current_value: float,
        historical_data: List[Dict[str, Any]],
        prediction_type: PredictionType,
    ) -> Dict[str, float]:
        """提取历史特征"""

        metric_name = prediction_type.value.upper()
        features = {}

        if not historical_data:
            # 如果没有历史数据，使用当前值估算
            features[f"{metric_name}_1h_ago"] = current_value * 0.95
            features[f"{metric_name}_1d_ago"] = current_value * 0.9
            features[f"{metric_name}_1w_ago"] = current_value * 0.85
            features[f"{metric_name}_change"] = 0.05
        else:
            # 从历史数据中提取
            df = pd.DataFrame(historical_data)
            if "value" in df.columns:
                values = df["value"].values

                # 1小时前（假设数据点间隔为1小时）
                features[f"{metric_name}_1h_ago"] = (
                    values[-1] if len(values) >= 1 else current_value
                )

                # 1天前
                features[f"{metric_name}_1d_ago"] = (
                    values[-24] if len(values) >= 24 else values[0]
                )

                # 1周前
                features[f"{metric_name}_1w_ago"] = (
                    values[-168] if len(values) >= 168 else values[0]
                )

                # 变化率
                if len(values) >= 2:
                    features[f"{metric_name}_change"] = (
                        current_value - values[-1]
                    ) / max(values[-1], 1)
                else:
                    features[f"{metric_name}_change"] = 0
            else:
                # 使用默认值
                features[f"{metric_name}_1h_ago"] = current_value * 0.95
                features[f"{metric_name}_1d_ago"] = current_value * 0.9
                features[f"{metric_name}_1w_ago"] = current_value * 0.85
                features[f"{metric_name}_change"] = 0.05

        return features

    def _extract_statistical_features(
        self,
        current_value: float,
        historical_data: List[Dict[str, Any]],
        prediction_type: PredictionType,
    ) -> Dict[str, float]:
        """提取统计特征"""

        metric_name = prediction_type.value.upper()
        features = {}

        if not historical_data:
            # 使用当前值作为基准
            features[f"{metric_name}_avg_6h"] = current_value
            features[f"{metric_name}_avg_24h"] = current_value
            features[f"{metric_name}_std_6h"] = current_value * 0.1
            features[f"{metric_name}_max_6h"] = current_value
            features[f"{metric_name}_min_6h"] = current_value
            features[f"{metric_name}_trend"] = 0
            features[f"{metric_name}_growth_rate"] = 0
            features[f"{metric_name}_max_24h"] = current_value
        else:
            df = pd.DataFrame(historical_data)
            if "value" in df.columns:
                values = df["value"].values

                # 6小时平均
                features[f"{metric_name}_avg_6h"] = (
                    np.mean(values[-6:]) if len(values) >= 6 else np.mean(values)
                )

                # 24小时平均
                features[f"{metric_name}_avg_24h"] = (
                    np.mean(values[-24:]) if len(values) >= 24 else np.mean(values)
                )

                # 6小时标准差
                features[f"{metric_name}_std_6h"] = (
                    np.std(values[-6:]) if len(values) >= 6 else 0
                )
                # 6小时最大/最小
                features[f"{metric_name}_max_6h"] = (
                    np.max(values[-6:]) if len(values) >= 6 else np.max(values)
                )
                features[f"{metric_name}_min_6h"] = (
                    np.min(values[-6:]) if len(values) >= 6 else np.min(values)
                )

                # 趋势（线性回归斜率）
                if len(values) >= 2:
                    x = np.arange(len(values))
                    z = np.polyfit(x, values, 1)
                    features[f"{metric_name}_trend"] = z[0]
                else:
                    features[f"{metric_name}_trend"] = 0

                # 增长率
                if len(values) >= 24:
                    old_avg = np.mean(values[:12])
                    new_avg = np.mean(values[-12:])
                    features[f"{metric_name}_growth_rate"] = (new_avg - old_avg) / max(
                        old_avg, 1
                    )
                    features[f"{metric_name}_max_24h"] = np.max(values[-24:])
                else:
                    features[f"{metric_name}_growth_rate"] = 0
                    features[f"{metric_name}_max_24h"] = np.max(values)
            else:
                # 使用默认值
                features[f"{metric_name}_avg_6h"] = current_value
                features[f"{metric_name}_avg_24h"] = current_value
                features[f"{metric_name}_std_6h"] = current_value * 0.1
                features[f"{metric_name}_max_6h"] = current_value
                features[f"{metric_name}_min_6h"] = current_value
                features[f"{metric_name}_trend"] = 0
                features[f"{metric_name}_growth_rate"] = 0
                features[f"{metric_name}_max_24h"] = current_value

        return features

    def _is_holiday(self, timestamp: datetime) -> bool:
        """判断是否为节假日"""

        # 这里可以接入节假日API或使用预定义的节假日列表
        # 简单实现：周六日视为节假日
        return timestamp.weekday() >= 5

    def _get_default_value(self, feature_name: str, current_value: float) -> float:
        """获取特征的默认值"""

        if "ago" in feature_name:
            return current_value * 0.9
        elif "avg" in feature_name:
            return current_value
        elif "std" in feature_name:
            return current_value * 0.1
        elif (
            "change" in feature_name
            or "trend" in feature_name
            or "growth" in feature_name
        ):
            return 0
        elif feature_name in ["is_weekend", "is_business_hour", "is_holiday"]:
            return 0
        elif "sin" in feature_name or "cos" in feature_name:
            return 0
        else:
            return 0

    def _get_default_features(
        self, prediction_type: PredictionType, current_value: float
    ) -> pd.DataFrame:
        """获取默认特征DataFrame"""

        required_features = self.feature_config[prediction_type]
        features = {}

        for feature in required_features:
            features[feature] = self._get_default_value(feature, current_value)

        # 设置主要特征值
        metric_name = prediction_type.value.upper()
        if metric_name in features:
            features[metric_name] = current_value

        return pd.DataFrame([features])[required_features]

    def get_feature_importance(
        self, prediction_type: PredictionType
    ) -> Dict[str, float]:
        """获取特征重要性"""

        # 这里可以从训练好的模型中获取真实的特征重要性
        # 暂时返回预定义的重要性

        importance_map = {
            PredictionType.QPS: {
                "QPS": 0.3,
                "QPS_avg_6h": 0.2,
                "QPS_1h_ago": 0.15,
                "is_business_hour": 0.1,
                "sin_time": 0.08,
                "cos_time": 0.08,
                "QPS_change": 0.05,
                "is_weekend": 0.04,
            },
            PredictionType.CPU: {
                "CPU": 0.35,
                "CPU_avg_6h": 0.2,
                "CPU_1h_ago": 0.15,
                "CPU_std_6h": 0.1,
                "is_business_hour": 0.08,
                "CPU_trend": 0.07,
                "sin_time": 0.05,
            },
            PredictionType.MEMORY: {
                "MEMORY": 0.4,
                "MEMORY_avg_6h": 0.25,
                "MEMORY_trend": 0.15,
                "MEMORY_1h_ago": 0.1,
                "MEMORY_change": 0.05,
                "is_business_hour": 0.05,
            },
            PredictionType.DISK: {
                "DISK": 0.35,
                "DISK_growth_rate": 0.25,
                "DISK_avg_24h": 0.2,
                "DISK_1d_ago": 0.1,
                "DISK_trend": 0.1,
            },
        }

        return importance_map.get(prediction_type, {})

    def _calculate_std_6h(
        self, historical_data: List[Dict[str, Any]], current_value: float
    ) -> float:
        """计算6小时标准差"""
        if not historical_data or len(historical_data) < 6:
            return current_value * 0.1  # 默认标准差为当前值的10%

        values = [d.get("value", current_value) for d in historical_data[-6:]]
        import numpy as np

        return float(np.std(values)) if len(values) > 1 else current_value * 0.1

    def _calculate_max_6h(
        self, historical_data: List[Dict[str, Any]], current_value: float
    ) -> float:
        """计算6小时最大值"""
        if not historical_data:
            return current_value * 1.1

        values = [d.get("value", current_value) for d in historical_data[-6:]]
        return max(values + [current_value])

    def _calculate_min_6h(
        self, historical_data: List[Dict[str, Any]], current_value: float
    ) -> float:
        """计算6小时最小值"""
        if not historical_data:
            return current_value * 0.9

        values = [d.get("value", current_value) for d in historical_data[-6:]]
        return min(values + [current_value])

    def _calculate_trend(
        self, historical_data: List[Dict[str, Any]], current_value: float
    ) -> float:
        """计算趋势（斜率）"""
        if not historical_data or len(historical_data) < 2:
            return 0.0

        values = [
            d.get("value", current_value) for d in historical_data[-12:]
        ]  # 12小时趋势
        if len(values) < 2:
            return 0.0

        import numpy as np

        try:
            # 计算线性回归斜率
            x = np.arange(len(values))
            y = np.array(values)
            slope = np.polyfit(x, y, 1)[0]
            return float(np.clip(slope, -10, 10))  # 限制趋势范围
        except Exception:
            return 0.0

    def _calculate_growth_rate(
        self, historical_data: List[Dict[str, Any]], current_value: float
    ) -> float:
        """计算增长率"""
        if not historical_data or len(historical_data) < 24:
            return 0.001  # 默认增长率

        prev_day_value = historical_data[-24].get("value", current_value)
        if prev_day_value <= 0:
            return 0.001

        growth_rate = (current_value - prev_day_value) / prev_day_value
        return float(np.clip(growth_rate, -0.5, 0.5))  # 限制增长率范围

    def _calculate_max_24h(
        self, historical_data: List[Dict[str, Any]], current_value: float
    ) -> float:
        """计算24小时最大值"""
        if not historical_data:
            return current_value * 1.1

        values = [d.get("value", current_value) for d in historical_data[-24:]]
        return max(values + [current_value])
