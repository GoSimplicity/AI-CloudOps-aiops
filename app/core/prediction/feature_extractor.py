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
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from app.common.exceptions import PredictionError
from app.core.prediction.measured_history import MeasuredHistoryError, measured_feature_row
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
        required_features: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """提取调用方点名的特征。未点名时使用本提取器的清单，缺测点则失败，不补常数。"""

        names = list(required_features) if required_features is not None else list(self.feature_config[prediction_type])
        try:
            time_features = self._extract_time_features(timestamp)
            row = measured_feature_row(names, current_value, historical_data, time_features)
            return pd.DataFrame([row], columns=names)
        except MeasuredHistoryError:
            raise
        except Exception as error:
            logger.error("特征提取失败，异常类型: %s", type(error).__name__)
            raise PredictionError("特征提取失败") from None

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

    def _is_holiday(self, timestamp: datetime) -> bool:
        """判断是否为节假日"""

        # 这里可以接入节假日API或使用预定义的节假日列表
        # 简单实现：周六日视为节假日
        return timestamp.weekday() >= 5

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
