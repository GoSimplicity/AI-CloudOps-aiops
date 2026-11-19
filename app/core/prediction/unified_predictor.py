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

from app.common.exceptions import HistoricalDataError, PredictionError, ValidationError
from app.core.prediction.measured_history import (
    ADAPTED_REASON,
    HISTORY_REASON,
    PREDICT_FAILED_REASON,
    SCHEMA_REASON,
    UNAVAILABLE_REASON,
    MeasuredHistoryError,
    fallback_code_for,
    resolve_model_schema,
)
from app.models import PredictionDataPoint, PredictionGranularity, PredictionType

logger = logging.getLogger("aiops.core.predictor")


class PredictionBatch(list):
    """单次预测及其来源，避免并发请求共用可变的最近结果。"""

    def __init__(self, points, *, method, model_version=None, fallback_reason=None, fallback_code=None, model_accuracy=None):
        super().__init__(points)
        self.method = method
        self.model_version = model_version
        self.fallback_reason = fallback_reason
        self.fallback_code = fallback_code if fallback_code is not None else fallback_code_for(fallback_reason)
        self.model_accuracy = model_accuracy


def prediction_provenance(predictions):
    return {
        "method": getattr(predictions, "method", None),
        "model_version": getattr(predictions, "model_version", None),
        "fallback_reason": getattr(predictions, "fallback_reason", None),
        "fallback_code": getattr(predictions, "fallback_code", None),
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

        except (ValidationError, HistoricalDataError):
            self._initialized = False
            raise
        except Exception as e:
            logger.error("预测器初始化失败，异常类型: %s", type(e).__name__)
            self._initialized = False
            raise PredictionError("预测器初始化失败") from None

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
            raise PredictionError("预测器未初始化")

        initial_value = current_value
        model = self.model_manager.get_model(prediction_type)
        if model is None:
            return await self._rule_based_prediction(
                prediction_type, current_value, prediction_hours, granularity,
                fallback_reason=UNAVAILABLE_REASON,
            )

        raw_metadata = getattr(self.model_manager, "metadata", {})
        metadata = raw_metadata.get(prediction_type, {}) if isinstance(raw_metadata, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        scaler = self.model_manager.get_scaler(prediction_type)
        declared_features, schema_problem = resolve_model_schema(model, metadata, scaler)
        if schema_problem or not declared_features:
            return await self._rule_based_prediction(
                prediction_type, initial_value, prediction_hours, granularity,
                fallback_reason=SCHEMA_REASON,
            )

        try:
            if granularity == PredictionGranularity.MINUTE:
                points = prediction_hours * 60
                delta = timedelta(minutes=1)
            elif granularity == PredictionGranularity.HOUR:
                points = prediction_hours
                delta = timedelta(hours=1)
            else:  # DAY
                points = prediction_hours // 24
                delta = timedelta(days=1)

            predictions = []
            current_time = datetime.now(timezone.utc)
            for i in range(points):
                future_time = current_time + delta * (i + 1)
                # 时间特征保持既有本地无时区时间。滞后和窗口锚定最新实测 T，不用预测值回填。
                features = await self.feature_extractor.extract_features(
                    timestamp=future_time.astimezone().replace(tzinfo=None),
                    current_value=current_value,
                    historical_data=historical_data,
                    prediction_type=prediction_type,
                    required_features=declared_features,
                )
                values = features[declared_features].to_numpy(dtype=float)
                if values.shape != (1, len(declared_features)) or not np.isfinite(values).all():
                    raise MeasuredHistoryError(SCHEMA_REASON)
                if scaler is not None:
                    values = scaler.transform(values)
                predicted_value = model.predict(values)[0]
                if not np.isfinite(float(predicted_value)):
                    raise PredictionError(PREDICT_FAILED_REASON)
                predicted_value = self._adjust_prediction_range(predicted_value, prediction_type)
                predictions.append(PredictionDataPoint(timestamp=future_time, predicted_value=float(predicted_value)))
                if consider_pattern:
                    current_value = predicted_value

            backtest = metadata.get("backtest") if isinstance(metadata.get("backtest"), dict) else None
            samples = backtest.get("sample_count") if isinstance(backtest, dict) else None
            accuracy = backtest.get("accuracy") if isinstance(samples, int) and not isinstance(samples, bool) and samples > 0 else None
            adapted = bool(metadata.get("adapted_for") or metadata.get("adapted_from"))
            if adapted:
                accuracy = None
            if not isinstance(accuracy, (int, float)) or isinstance(accuracy, bool) or not np.isfinite(accuracy) or not 0 <= accuracy <= 1:
                accuracy = None
            return PredictionBatch(
                predictions,
                method=f"model:{type(model).__module__}.{type(model).__qualname__}",
                model_version=metadata.get("model_version"),
                fallback_reason=ADAPTED_REASON if adapted else None,
                model_accuracy=accuracy,
            )
        except MeasuredHistoryError as error:
            reason = SCHEMA_REASON if SCHEMA_REASON in str(error) else HISTORY_REASON
            return await self._rule_based_prediction(
                prediction_type, initial_value, prediction_hours, granularity, fallback_reason=reason,
            )
        except Exception as error:
            logger.error("预测执行失败，使用规则预测: %s", type(error).__name__)
            return await self._rule_based_prediction(
                prediction_type, initial_value, prediction_hours, granularity,
                fallback_reason=PREDICT_FAILED_REASON,
            )

    async def _rule_based_prediction(
        self,
        prediction_type: PredictionType,
        current_value: float,
        prediction_hours: int,
        granularity: PredictionGranularity,
        fallback_reason: str = UNAVAILABLE_REASON,
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
            logger.warning("预测器健康检查失败，异常类型: %s", type(e).__name__)
            return False

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
