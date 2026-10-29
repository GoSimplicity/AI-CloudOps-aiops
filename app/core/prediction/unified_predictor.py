#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps统一预测引擎 - 支持多种资源类型预测
"""

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from app.models import PredictionDataPoint, PredictionGranularity, PredictionType

logger = logging.getLogger("aiops.core.predictor")


class PredictionBatch(list):
    """单次预测及其来源，避免并发请求共用可变的最近结果。"""

    def __init__(self, points, *, method, model_version=None, fallback_reason=None, model_accuracy=None):
        super().__init__(points)
        self.method = method
        self.model_version = model_version
        self.fallback_reason = fallback_reason
        self.model_accuracy = model_accuracy


def prediction_provenance(predictions):
    return {
        "method": getattr(predictions, "method", None),
        "model_version": getattr(predictions, "model_version", None),
        "fallback_reason": getattr(predictions, "fallback_reason", None),
        "fallback_mode": bool(getattr(predictions, "fallback_reason", None)),
        "model_accuracy": getattr(predictions, "model_accuracy", None),
    }


class UnifiedPredictor:
    """统一预测引擎 - 支持多种资源类型预测"""

    def __init__(self, model_manager, feature_extractor) -> None:
        """初始化预测器"""
        self.model_manager = model_manager
        self.feature_extractor = feature_extractor
        self._initialized = False

    async def initialize(self) -> None:
        """初始化预测器"""
        try:
            # 确保模型管理器已初始化
            if not self.model_manager.models_loaded:
                await self.model_manager.load_models()

            self._initialized = True
            logger.info("统一预测器初始化完成")

        except Exception as e:
            logger.error(f"预测器初始化失败: {str(e)}")
            self._initialized = False
            raise

    async def predict(
        self,
        prediction_type: PredictionType,
        current_value: float,
        historical_data: List[Dict[str, Any]],
        prediction_hours: int,
        granularity: PredictionGranularity,
        consider_pattern: bool = True,
    ) -> List[PredictionDataPoint]:
        """执行预测"""

        if not self._initialized:
            from app.common.exceptions import PredictionError

            raise PredictionError("预测器未初始化")

        initial_value = current_value
        try:
            # 获取对应类型的模型
            model = self.model_manager.get_model(prediction_type)
            scaler = self.model_manager.get_scaler(prediction_type)

            if model is None:
                return await self._rule_based_prediction(
                    prediction_type, current_value, prediction_hours, granularity
                )

            predictions = []
            current_time = datetime.now(timezone.utc)

            # 计算预测点数
            if granularity == PredictionGranularity.MINUTE:
                points = prediction_hours * 60
                delta = timedelta(minutes=1)
            elif granularity == PredictionGranularity.HOUR:
                points = prediction_hours
                delta = timedelta(hours=1)
            else:  # DAY
                points = prediction_hours // 24
                delta = timedelta(days=1)

            # 逐点预测
            for i in range(points):
                future_time = current_time + delta * (i + 1)

                # 提取特征
                features = await self.feature_extractor.extract_features(
                    # 特征保持既有本地无时区时间，输出时间单独使用UTC
                    timestamp=future_time.astimezone().replace(tzinfo=None),
                    current_value=current_value,
                    historical_data=historical_data,
                    prediction_type=prediction_type,
                )

                # 确保特征与模型训练时的格式一致
                model_metadata = self.model_manager.metadata.get(prediction_type, {})
                features_aligned = self._align_features_with_model(
                    features, prediction_type, model_metadata
                )

                # 标准化特征（转换为numpy数组以保持与训练时的一致性）
                if scaler is not None:
                    # 使用numpy数组进行标准化，避免特征名称不匹配警告
                    features_scaled = scaler.transform(features_aligned.values)
                else:
                    features_scaled = features_aligned.values

                # 预测
                predicted_value = model.predict(features_scaled)[0]

                # 根据预测类型调整值范围
                predicted_value = self._adjust_prediction_range(
                    predicted_value, prediction_type
                )

                # 创建预测点
                prediction_point = PredictionDataPoint(
                    timestamp=future_time,
                    predicted_value=float(predicted_value),
                )

                predictions.append(prediction_point)

                # 更新当前值用于下一次预测
                if consider_pattern:
                    current_value = predicted_value

            metadata = self.model_manager.metadata.get(prediction_type, {})
            backtest = metadata.get("backtest") if isinstance(metadata, dict) else None
            samples = backtest.get("sample_count") if isinstance(backtest, dict) else None
            accuracy = backtest.get("accuracy") if isinstance(samples, int) and not isinstance(samples, bool) and samples > 0 else None
            if metadata.get("adapted_for") or metadata.get("adapted_from"):
                accuracy = None
            if not isinstance(accuracy, (int, float)) or isinstance(accuracy, bool) or not np.isfinite(accuracy) or not 0 <= accuracy <= 1:
                accuracy = None
            return PredictionBatch(predictions, method=f"model:{type(model).__module__}.{type(model).__qualname__}",
                                   model_version=metadata.get("model_version") if not metadata.get("metadata_missing") else None,
                                   fallback_reason="使用其他指标的模型" if metadata.get("adapted_for") or metadata.get("adapted_from") else None,
                                   model_accuracy=accuracy)

        except Exception as e:
            logger.error("预测执行失败，使用规则预测: %s", type(e).__name__)
            # 降级到规则基础预测
            return await self._rule_based_prediction(
                prediction_type, initial_value, prediction_hours, granularity,
                fallback_reason="模型预测失败",
            )

    async def _rule_based_prediction(
        self,
        prediction_type: PredictionType,
        current_value: float,
        prediction_hours: int,
        granularity: PredictionGranularity,
        fallback_reason: str = "预测模型不可用",
    ) -> List[PredictionDataPoint]:
        """基于规则的预测（降级方案）"""

        predictions = []
        current_time = datetime.now(timezone.utc)

        # 计算预测点数
        if granularity == PredictionGranularity.MINUTE:
            points = min(prediction_hours * 60, 1440)  # 最多一天的分钟数
            delta = timedelta(minutes=1)
        elif granularity == PredictionGranularity.HOUR:
            points = prediction_hours
            delta = timedelta(hours=1)
        else:  # DAY
            points = prediction_hours // 24
            delta = timedelta(days=1)

        # 从配置文件或使用默认增长率
        default_growth_rates = {
            PredictionType.QPS: 0.02,
            PredictionType.CPU: 0.01,
            PredictionType.MEMORY: 0.005,
            PredictionType.DISK: 0.003,
        }
        # 可从配置文件扩展增长率设置
        growth_rates = default_growth_rates

        growth_rate = growth_rates.get(prediction_type, 0.01)

        for i in range(points):
            future_time = current_time + delta * (i + 1)

            # 简单的线性增长模型，加入时间因素
            # 工作时间因子沿用部署时区，不改变现有规则数值语义
            calendar_time = future_time.astimezone()
            hour_of_day = calendar_time.hour
            day_of_week = calendar_time.weekday()

            # 时间因子（工作时间负载更高）
            time_factor = 1.0
            if 9 <= hour_of_day <= 18 and day_of_week < 5:  # 工作时间
                time_factor = 1.2
            elif 0 <= hour_of_day < 6:  # 深夜
                time_factor = 0.6

            # 计算预测值
            base_growth = current_value * (1 + growth_rate * (i + 1))
            predicted_value = base_growth * time_factor

            # 调整值范围
            predicted_value = self._adjust_prediction_range(
                predicted_value, prediction_type
            )

            predictions.append(
                PredictionDataPoint(
                    timestamp=future_time,
                    predicted_value=float(predicted_value),
                )
            )

        return PredictionBatch(predictions, method="rule_based", fallback_reason=fallback_reason)

    def _adjust_prediction_range(
        self, value: float, prediction_type: PredictionType
    ) -> float:
        """调整预测值到合理范围"""

        if prediction_type == PredictionType.QPS:
            return np.clip(value, 0, 100000)
        elif prediction_type in [
            PredictionType.CPU,
            PredictionType.MEMORY,
            PredictionType.DISK,
        ]:
            return np.clip(value, 0, 100)
        else:
            return max(0, value)

    async def is_healthy(self) -> bool:
        """检查预测器健康状态"""
        try:
            health_checks = [
                self._initialized,
                self.model_manager is not None,
                self.model_manager.models_loaded if self.model_manager else False,
                self.feature_extractor is not None,
            ]
            return all(health_checks)
        except Exception as e:
            logger.warning(f"预测器健康检查失败: {str(e)}")
            return False

    def _align_features_with_model(
        self,
        features: pd.DataFrame,
        prediction_type: PredictionType,
        model_data: Dict[str, Any],
    ) -> pd.DataFrame:
        """确保特征与模型训练时的格式一致"""

        # 获取模型训练时的特征顺序
        model_features = model_data.get("metadata", {}).get("features", [])

        if not model_features:
            # 如果没有元数据中的特征信息，使用默认特征列表
            model_features = self._get_default_model_features(prediction_type)

        # 确保特征DataFrame包含所有必需的特征
        aligned_features = {}

        for feature in model_features:
            if feature in features.columns:
                aligned_features[feature] = features[feature].iloc[0]
            else:
                # 对缺失的特征使用默认值
                aligned_features[feature] = self._get_feature_default_value(feature)

        # 创建新的DataFrame，确保特征顺序与模型训练时一致
        result_df = pd.DataFrame([aligned_features], columns=model_features)

        return result_df

    def _get_default_model_features(self, prediction_type: PredictionType) -> List[str]:
        """获取默认的模型特征列表（与模型训练时一致）"""

        base_features = [
            "sin_time",
            "cos_time",
            "sin_day",
            "cos_day",
            "is_business_hour",
            "is_weekend",
            "is_holiday",
        ]

        if prediction_type == PredictionType.QPS:
            return (
                ["QPS"]
                + base_features
                + [
                    "QPS_1h_ago",
                    "QPS_1d_ago",
                    "QPS_1w_ago",
                    "QPS_change",
                    "QPS_avg_6h",
                    "QPS_std_6h",
                ]
            )
        elif prediction_type == PredictionType.CPU:
            return (
                ["CPU"]
                + base_features
                + [
                    "CPU_1h_ago",
                    "CPU_1d_ago",
                    "CPU_1w_ago",
                    "CPU_change",
                    "CPU_avg_6h",
                    "CPU_std_6h",
                    "CPU_max_6h",
                ]
            )
        elif prediction_type == PredictionType.MEMORY:
            return (
                ["MEMORY"]
                + base_features
                + [
                    "MEMORY_1h_ago",
                    "MEMORY_1d_ago",
                    "MEMORY_1w_ago",
                    "MEMORY_change",
                    "MEMORY_avg_6h",
                    "MEMORY_trend",
                    "MEMORY_min_6h",
                ]
            )
        else:  # DISK
            return (
                ["DISK"]
                + base_features
                + [
                    "DISK_1h_ago",
                    "DISK_1d_ago",
                    "DISK_1w_ago",
                    "DISK_change",
                    "DISK_avg_24h",
                    "DISK_growth_rate",
                    "DISK_max_24h",
                ]
            )

    def _get_feature_default_value(self, feature_name: str) -> float:
        """获取特征的默认值"""

        if feature_name.endswith("_ago"):
            return 50.0  # 历史值默认
        elif feature_name.endswith("_change"):
            return 0.0  # 变化率默认为0
        elif feature_name.startswith("is_"):
            return 0.0  # 布尔特征默认为0
        elif "sin_" in feature_name or "cos_" in feature_name:
            return 0.5  # 三角函数特征默认值
        elif feature_name.endswith("_avg_6h") or feature_name.endswith("_avg_24h"):
            return 50.0  # 平均值默认
        elif feature_name.endswith("_std_6h"):
            return 5.0  # 标准差默认
        elif feature_name.endswith("_trend"):
            return 0.0  # 趋势默认
        elif feature_name.endswith("_growth_rate"):
            return 0.001  # 增长率默认
        elif feature_name.endswith("_min_6h"):
            return 45.0  # 最小值默认
        elif feature_name.endswith("_max_6h") or feature_name.endswith("_max_24h"):
            return 55.0  # 最大值默认
        else:
            return 50.0  # 其他特征默认值

    async def get_supported_types(self) -> List[PredictionType]:
        """获取支持的预测类型"""
        return list(PredictionType)

    async def get_service_info(self) -> Dict[str, Any]:
        """获取服务信息"""
        return {
            "service_name": "unified_predictor",
            "initialized": self._initialized,
            "supported_types": [t.value for t in await self.get_supported_types()],
            "model_manager_status": (
                "loaded"
                if self.model_manager and self.model_manager.models_loaded
                else "not_loaded"
            ),
            "feature_extractor_available": self.feature_extractor is not None,
        }
