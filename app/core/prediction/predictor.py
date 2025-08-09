#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 预测模块 - 提供负载预测功能
"""

import datetime
import asyncio
import logging
import math
from typing import Any, Dict, List, Optional

import pandas as pd

from app.config.settings import config
from app.core.prediction.model_loader import ModelLoader
from app.services.prometheus import PrometheusService
from app.utils.error_handlers import ErrorHandler

logger = logging.getLogger("aiops.predictor")

# 时间因子常量
HOUR_FACTORS = {
    0: 0.3, 1: 0.2, 2: 0.2, 3: 0.2, 4: 0.2, 5: 0.3,
    6: 0.5, 7: 0.7, 8: 0.9, 9: 1.0, 10: 1.0, 11: 1.0,
    12: 0.9, 13: 0.9, 14: 1.0, 15: 1.0, 16: 1.0, 17: 0.9,
    18: 0.8, 19: 0.7, 20: 0.6, 21: 0.5, 22: 0.4, 23: 0.3
}

DAY_FACTORS = {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0, 5: 0.8, 6: 0.6}
LOW_QPS_THRESHOLD = 1.0


class PredictionService:
    """负载预测服务"""

    def __init__(self):
        """初始化负载预测服务"""
        self.model_loader = ModelLoader()
        self.error_handler = ErrorHandler(logger)
        self.prometheus_service = PrometheusService()
        
        # 初始化模型
        self._initialize()
        
        logger.info("负载预测服务初始化完成")

    def _initialize(self):
        """初始化模型"""
        try:
            # 加载模型和标准化器
            self.model_loaded = self.model_loader.load_models()
            
            if self.model_loaded:
                logger.info("预测模型加载成功")
                # 验证模型
                if not self.model_loader.validate_model():
                    logger.warning("模型验证失败，将使用基础预测方法")
                    self.model_loaded = False
            else:
                logger.warning("预测模型加载失败，将使用基础预测方法")
                
        except Exception as e:
            logger.error(f"模型初始化失败: {e}")
            self.model_loaded = False

    async def predict(
        self,
        current_qps: Optional[float] = None,
        timestamp: Optional[datetime.datetime] = None,
        metric: Optional[str] = None,
        selector: Optional[str] = None,
        window: str = "1m",
    ) -> Dict[str, Any]:
        """预测实例数"""
        try:
            # 使用默认值
            if current_qps is None:
                current_qps = 50.0  # 默认QPS
            if timestamp is None:
                timestamp = datetime.datetime.now()

            logger.info(f"开始预测实例数: 当前QPS={current_qps}, 时间={timestamp}")

            # 执行预测
            if self.model_loaded:
                prediction_result = await self._ml_predict(
                    current_qps=current_qps,
                    timestamp=timestamp,
                    metric=metric,
                    selector=selector,
                    window=window,
                )
            else:
                prediction_result = self._simple_predict(current_qps, timestamp)

            # 构建预测结果
            result: Dict[str, Any] = {
                "instances": prediction_result.get("instances", 1),
                "current_qps": current_qps,
                "timestamp": timestamp.isoformat(),
                "confidence": prediction_result.get("confidence", 0.5),
                "model_version": self.model_loader.model_metadata.get("version", "1.0"),
                "prediction_type": prediction_result.get("method", "simple"),
                "features": prediction_result.get("features")
            }

            logger.info(f"预测完成: 当前QPS={current_qps:.2f}, 预测实例数={result['instances']}")
            return result

        except Exception as e:
            logger.error(f"预测失败: {e}")
            raise Exception(f"预测服务异常: {str(e)}")


    async def get_qps_from_prometheus(
        self,
        metric: str,
        selector: Optional[str] = None,
        window: str = "1m",
        timestamp: Optional[datetime.datetime] = None,
    ) -> Optional[float]:
        """从Prometheus获取当前QPS

        参数设计尽量简单，避免过度设计：
        - metric: 指标名（例如 http_requests_total）
        - selector: 标签选择器字符串，例如 'job="my-service",namespace="default"'
        - window: 速率计算窗口，默认1m
        - timestamp: 可选时间点，不传则使用当前时间
        """
        try:
            if not metric:
                return None

            # 构造PromQL：sum(rate(metric{selector}[window]))
            if selector and selector.strip():
                query = f"sum(rate({metric}{{{selector}}}[{window}]))"
            else:
                query = f"sum(rate({metric}[{window}]))"

            results = await self.prometheus_service.query_instant(query, timestamp)
            if not results:
                return None

            # 兼容多序列的情况，求和得到整体QPS
            total_qps: float = 0.0
            for series in results:
                value = series.get("value")
                if isinstance(value, list) and len(value) == 2:
                    try:
                        total_qps += float(value[1])
                    except (ValueError, TypeError):
                        continue
            return total_qps if total_qps >= 0 else 0.0
        except Exception as e:
            logger.error(f"从Prometheus获取QPS失败: {e}")
            return None

    async def _ml_predict(
        self,
        current_qps: float,
        timestamp: datetime.datetime,
        metric: Optional[str] = None,
        selector: Optional[str] = None,
        window: str = "1m",
    ) -> Dict[str, Any]:
        """机器学习预测"""
        try:
            if not self.model_loaded:
                return self._simple_predict(current_qps, timestamp)

            # 准备特征数据
            features = await self._prepare_features(
                current_qps=current_qps,
                timestamp=timestamp,
                metric=metric,
                selector=selector,
                window=window,
            )
            
            # 转换为DataFrame
            feature_df = pd.DataFrame([features])
            
            # 数据标准化
            scaled_features = self.model_loader.scaler.transform(feature_df)
            
            # 执行预测
            prediction = self.model_loader.model.predict(scaled_features)
            instances = max(1, int(round(prediction[0])))
            # 边界约束
            instances = max(config.prediction.min_instances, min(instances, config.prediction.max_instances))
            
            # 计算置信度
            confidence = self._calculate_ml_confidence(current_qps, instances)
            
            return {
                "instances": instances,
                "confidence": confidence,
                "method": "machine_learning",
                "features": features
            }
            
        except Exception as e:
            logger.error(f"机器学习预测失败: {e}")
            return self._simple_predict(current_qps, timestamp)

    def _simple_predict(self, current_qps: float, timestamp: datetime.datetime) -> Dict[str, Any]:
        """简单预测方法"""
        try:
            # 基于QPS的简单预测算法
            # 每30 QPS需要1个实例
            instances = max(1, int(math.ceil(current_qps / 30)))
            
            # 基于时间因子调整
            hour_factor = self._get_hour_factor(timestamp.hour)
            day_factor = self._get_day_factor(timestamp.weekday())
            
            # 应用时间因子
            adjusted_instances = max(1, int(round(instances * hour_factor * day_factor)))
            # 边界约束
            adjusted_instances = max(
                config.prediction.min_instances,
                min(adjusted_instances, config.prediction.max_instances),
            )
            
            return {
                "instances": adjusted_instances,
                "confidence": 0.6 if current_qps > LOW_QPS_THRESHOLD else 0.3,
                "method": "time_factor"
            }
            
        except Exception as e:
            logger.error(f"简单预测失败: {e}")
            return {
                "instances": 1,
                "confidence": 0.1,
                "method": "fallback"
            }

    async def _prepare_features(
        self,
        current_qps: float,
        timestamp: datetime.datetime,
        metric: Optional[str] = None,
        selector: Optional[str] = None,
        window: str = "1m",
    ) -> Dict[str, float]:
        """准备机器学习特征

        优先使用 Prometheus 历史真实数据来构造特征；当缺失或查询失败时回退到合理估算。
        """
        try:
            # 时间特征
            hour = timestamp.hour
            day_of_year = timestamp.timetuple().tm_yday

            sin_time = math.sin(2 * math.pi * hour / 24)
            cos_time = math.cos(2 * math.pi * hour / 24)
            sin_day = math.sin(2 * math.pi * day_of_year / 365)
            cos_day = math.cos(2 * math.pi * day_of_year / 365)

            is_business_hour = 1 if 9 <= hour <= 17 else 0
            is_weekend = 1 if timestamp.weekday() >= 5 else 0

            # 默认估算，若有 Prometheus 指标则尝试真实数据
            qps_1h_ago = current_qps * 0.9
            qps_1d_ago = current_qps * 1.1
            qps_1w_ago = current_qps * 1.05
            qps_avg_6h = current_qps * 0.95

            if metric:
                # 并发查询 1h/1d/1w 前的瞬时 QPS
                one_hour_ago = timestamp - datetime.timedelta(hours=1)
                one_day_ago = timestamp - datetime.timedelta(days=1)
                one_week_ago = timestamp - datetime.timedelta(days=7)

                async def _get(tp: datetime.datetime) -> Optional[float]:
                    return await self.get_qps_from_prometheus(
                        metric=metric, selector=selector, window=window, timestamp=tp
                    )

                inst_tasks = [
                    _get(one_hour_ago),
                    _get(one_day_ago),
                    _get(one_week_ago),
                ]

                # 6 小时区间均值
                if selector and selector.strip():
                    promql = f"sum(rate({metric}{{{selector}}}[{window}]))"
                else:
                    promql = f"sum(rate({metric}[{window}]))"

                range_task = self.prometheus_service.query_range(
                    query=promql,
                    start_time=timestamp - datetime.timedelta(hours=6),
                    end_time=timestamp,
                    step="1m",
                )

                results = await asyncio.gather(*inst_tasks, range_task, return_exceptions=True)

                # 解析结果，出现异常则保持默认估算
                try:
                    if isinstance(results[0], Exception) is False and results[0] is not None:
                        qps_1h_ago = float(results[0])
                except Exception:
                    pass
                try:
                    if isinstance(results[1], Exception) is False and results[1] is not None:
                        qps_1d_ago = float(results[1])
                except Exception:
                    pass
                try:
                    if isinstance(results[2], Exception) is False and results[2] is not None:
                        qps_1w_ago = float(results[2])
                except Exception:
                    pass
                try:
                    df = results[3]
                    if df is not None and hasattr(df, "__class__"):
                        # 期望存在 value 列
                        if "value" in df.columns:
                            qps_avg_6h = float(df["value"].mean())
                        else:
                            # 若合并后列名不同，则取数值列均值
                            numeric_cols = df.select_dtypes(include=["number"]).columns
                            if len(numeric_cols) > 0:
                                qps_avg_6h = float(df[numeric_cols].mean().mean())
                except Exception:
                    pass

            qps_change = (current_qps - qps_1h_ago) / max(qps_1h_ago, 1.0)

            return {
                "QPS": current_qps,
                "sin_time": sin_time,
                "cos_time": cos_time,
                "sin_day": sin_day,
                "cos_day": cos_day,
                "is_business_hour": is_business_hour,
                "is_weekend": is_weekend,
                "QPS_1h_ago": qps_1h_ago,
                "QPS_1d_ago": qps_1d_ago,
                "QPS_1w_ago": qps_1w_ago,
                "QPS_change": qps_change,
                "QPS_avg_6h": qps_avg_6h,
            }

        except Exception as e:
            logger.error(f"特征准备失败: {e}")
            return {
                "QPS": current_qps,
                "sin_time": 0.0,
                "cos_time": 1.0,
                "sin_day": 0.0,
                "cos_day": 1.0,
                "is_business_hour": 1,
                "is_weekend": 0,
                "QPS_1h_ago": current_qps,
                "QPS_1d_ago": current_qps,
                "QPS_1w_ago": current_qps,
                "QPS_change": 0.0,
                "QPS_avg_6h": current_qps,
            }

    def _calculate_ml_confidence(self, current_qps: float, instances: int) -> float:
        """计算机器学习预测的置信度"""
        try:
            # 基于QPS合理性的置信度
            qps_confidence = 0.8 if current_qps > 10 else 0.5
            
            # 基于实例数合理性的置信度
            instance_confidence = 0.9 if 1 <= instances <= 10 else 0.6
            
            # 组合置信度
            confidence = (qps_confidence + instance_confidence) / 2
            
            return min(0.95, confidence)
            
        except Exception:
            return 0.5

    def _get_hour_factor(self, hour: int) -> float:
        """获取小时因子"""
        return HOUR_FACTORS.get(hour, 1.0)

    def _get_day_factor(self, day_of_week: int) -> float:
        """获取星期因子"""
        return DAY_FACTORS.get(day_of_week, 1.0)

    async def predict_trend(
        self,
        hours_ahead: int = 24,
        current_qps: Optional[float] = None,
        metric: Optional[str] = None,
        selector: Optional[str] = None,
        window: str = "1m",
    ) -> Dict[str, Any]:
        """预测趋势"""
        try:
            if current_qps is None:
                current_qps = 50.0
                
            now = datetime.datetime.now()
            predictions = []
            
            # 生成未来多个时间点的预测
            for hour_offset in [1, 3, 6, 12, 24]:
                if hour_offset <= hours_ahead:
                    future_time = now + datetime.timedelta(hours=hour_offset)
                    pred_result = await self.predict(
                        current_qps=current_qps,
                        timestamp=future_time,
                        metric=metric,
                        selector=selector,
                        window=window,
                    )
                    predictions.append({
                        "hours_ahead": hour_offset,
                        "timestamp": future_time.isoformat(),
                        "predicted_instances": pred_result.get("instances", 1),
                        "confidence": pred_result.get("confidence", 0.5)
                    })
            
            return {
                "current_qps": current_qps,
                "hours_ahead": hours_ahead,
                "trend_predictions": predictions,
                "trend_analysis": self._analyze_trend(predictions),
                "timestamp": now.isoformat()
            }
            
        except Exception as e:
            logger.error(f"趋势预测失败: {e}")
            raise Exception(f"趋势预测失败: {str(e)}")

    def _analyze_trend(self, predictions: List[Dict]) -> str:
        """分析趋势"""
        if len(predictions) < 2:
            return "数据不足"
        
        values = [p["predicted_instances"] for p in predictions]
        
        if values[-1] > values[0] * 1.2:
            return "上升趋势"
        elif values[-1] < values[0] * 0.8:
            return "下降趋势"
        else:
            return "平稳趋势"

    def is_healthy(self) -> bool:
        """检查服务健康状态"""
        return self.model_loaded

    def get_model_info(self) -> Dict[str, Any]:
        """获取模型信息"""
        return {
            "service_name": "PredictionService",
            "model_loaded": self.model_loaded,
            "model_info": self.model_loader.get_model_info(),
            "status": "healthy" if self.is_healthy() else "unhealthy"
        }

    def reload_models(self) -> bool:
        """重新加载模型"""
        try:
            self._initialize()
            return self.model_loaded
        except Exception as e:
            logger.error(f"重新加载模型失败: {e}")
            return False