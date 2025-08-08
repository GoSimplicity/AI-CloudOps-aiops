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
import logging
from typing import Any, Dict, List

import numpy as np
from app.constants import DAY_FACTORS, HOUR_FACTORS, LOW_QPS_THRESHOLD
from app.core.prediction.model_loader import ModelLoader
from app.services.prometheus import PrometheusService
from app.utils.error_handlers import ErrorHandler

logger = logging.getLogger("aiops.predictor")


class PredictionService:
    """负载预测服务"""

    def __init__(self):
        """初始化负载预测服务"""
        self.prometheus = PrometheusService()
        self.model_loader = ModelLoader()
        self.model_loaded = False
        self.scaler_loaded = False
        self.error_handler = ErrorHandler(logger)
        
        logger.info("负载预测服务初始化完成")

    def _initialize(self):
        """初始化模型"""
        try:
            self.model_loaded = self.model_loader.load_model()
            self.scaler_loaded = self.model_loader.load_scaler()
            
            if self.model_loaded and self.scaler_loaded:
                logger.info("预测模型加载成功")
            else:
                logger.warning("预测模型加载失败，将使用基础预测方法")
                
        except Exception as e:
            logger.error(f"模型初始化失败: {e}")
            self.model_loaded = False
            self.scaler_loaded = False

    async def predict(
        self, 
        service_name: str, 
        namespace: str = "default", 
        duration_minutes: int = 60
    ) -> Dict[str, Any]:
        """预测服务负载"""
        try:
            logger.info(f"开始预测服务 {service_name} 的负载")

            # 获取当前QPS
            current_qps = await self._get_current_qps(service_name, namespace)
            
            # 获取历史数据
            historical_data = await self._get_historical_qps(service_name, namespace)
            
            # 执行预测
            if self.model_loaded and len(historical_data) >= 10:
                prediction = await self._ml_predict(historical_data, duration_minutes)
            else:
                prediction = await self._simple_predict(current_qps, duration_minutes)

            # 构建预测结果
            result = {
                "service_name": service_name,
                "namespace": namespace,
                "current_qps": current_qps,
                "predicted_qps": prediction["predicted_qps"],
                "prediction_time": duration_minutes,
                "confidence": prediction.get("confidence", 0.5),
                "prediction_method": prediction.get("method", "simple"),
                "timestamp": datetime.datetime.now().isoformat(),
                "recommendation": self._generate_recommendation(
                    current_qps, prediction["predicted_qps"]
                )
            }

            logger.info(f"预测完成: 当前QPS={current_qps:.2f}, 预测QPS={prediction['predicted_qps']:.2f}")
            return result

        except Exception as e:
            logger.error(f"预测失败: {e}")
            return {
                "service_name": service_name,
                "namespace": namespace,
                "error": str(e),
                "current_qps": 0.0,
                "predicted_qps": 0.0,
                "confidence": 0.0
            }

    async def _get_current_qps(self, service_name: str, namespace: str = "default") -> float:
        """获取当前QPS"""
        try:
            query = f'rate(http_requests_total{{service="{service_name}",namespace="{namespace}"}}[5m])'
            result = await self.prometheus.query(query)
            
            if result and "data" in result:
                data_points = result["data"].get("result", [])
                if data_points:
                    return float(data_points[0]["value"][1])
            
            return 0.0
            
        except Exception as e:
            logger.error(f"获取当前QPS失败: {e}")
            return 0.0

    async def _get_historical_qps(
        self, 
        service_name: str, 
        namespace: str = "default", 
        hours: int = 24
    ) -> List[float]:
        """获取历史QPS数据"""
        try:
            end_time = datetime.datetime.now()
            start_time = end_time - datetime.timedelta(hours=hours)
            
            query = f'rate(http_requests_total{{service="{service_name}",namespace="{namespace}"}}[5m])'
            result = await self.prometheus.query_range(
                query, 
                start_time.timestamp(), 
                end_time.timestamp(), 
                step=300  # 5分钟间隔
            )
            
            qps_values = []
            if result and "data" in result:
                data_points = result["data"].get("result", [])
                if data_points:
                    values = data_points[0].get("values", [])
                    qps_values = [float(value[1]) for value in values]
            
            return qps_values
            
        except Exception as e:
            logger.error(f"获取历史QPS失败: {e}")
            return []

    async def _ml_predict(self, historical_data: List[float], duration_minutes: int) -> Dict[str, Any]:
        """机器学习预测"""
        try:
            if not self.model_loaded:
                return await self._simple_predict(historical_data[-1], duration_minutes)

            # 准备特征数据
            features = self._prepare_features(historical_data)
            
            # 执行预测
            prediction = self.model_loader.predict(features)
            
            # 计算置信度
            confidence = self._calculate_ml_confidence(historical_data, prediction)
            
            return {
                "predicted_qps": max(0.0, float(prediction)),
                "confidence": confidence,
                "method": "machine_learning"
            }
            
        except Exception as e:
            logger.error(f"机器学习预测失败: {e}")
            return await self._simple_predict(historical_data[-1] if historical_data else 0.0, duration_minutes)

    async def _simple_predict(self, current_qps: float, duration_minutes: int) -> Dict[str, Any]:
        """简单预测方法"""
        try:
            now = datetime.datetime.now()
            future_time = now + datetime.timedelta(minutes=duration_minutes)
            
            # 基于时间因子的预测
            current_hour_factor = self._get_hour_factor(now.hour)
            future_hour_factor = self._get_hour_factor(future_time.hour)
            
            current_day_factor = self._get_day_factor(now.weekday())
            future_day_factor = self._get_day_factor(future_time.weekday())
            
            # 计算预测值
            if current_hour_factor > 0:
                base_qps = current_qps / current_hour_factor / current_day_factor
                predicted_qps = base_qps * future_hour_factor * future_day_factor
            else:
                predicted_qps = current_qps  # 如果当前因子为0，保持当前值
            
            return {
                "predicted_qps": max(0.0, predicted_qps),
                "confidence": 0.6 if current_qps > LOW_QPS_THRESHOLD else 0.3,
                "method": "time_factor"
            }
            
        except Exception as e:
            logger.error(f"简单预测失败: {e}")
            return {
                "predicted_qps": current_qps,
                "confidence": 0.1,
                "method": "fallback"
            }

    def _prepare_features(self, historical_data: List[float]) -> List[float]:
        """准备机器学习特征"""
        try:
            if len(historical_data) < 5:
                return [0.0] * 10  # 默认特征
            
            recent_data = historical_data[-10:]  # 取最近10个数据点
            
            features = [
                np.mean(recent_data),  # 平均值
                np.std(recent_data),   # 标准差
                max(recent_data),      # 最大值
                min(recent_data),      # 最小值
                recent_data[-1],       # 最新值
            ]
            
            # 补充特征到固定长度
            while len(features) < 10:
                features.append(0.0)
                
            return features[:10]
            
        except Exception as e:
            logger.error(f"特征准备失败: {e}")
            return [0.0] * 10

    def _calculate_ml_confidence(self, historical_data: List[float], prediction: float) -> float:
        """计算机器学习预测的置信度"""
        try:
            if len(historical_data) < 2:
                return 0.3
            
            # 基于历史数据的方差计算置信度
            variance = np.var(historical_data[-10:])
            stability = 1.0 / (1.0 + variance)
            
            # 基于数据量的置信度调整
            data_confidence = min(len(historical_data) / 100.0, 1.0)
            
            return min(0.9, stability * 0.7 + data_confidence * 0.3)
            
        except Exception:
            return 0.5

    def _generate_recommendation(self, current_qps: float, predicted_qps: float) -> Dict[str, Any]:
        """生成资源调整建议"""
        try:
            change_ratio = (predicted_qps - current_qps) / max(current_qps, 0.1)
            
            if change_ratio > 0.5:
                return {
                    "action": "scale_up",
                    "reason": f"预计负载将增加{change_ratio:.1%}",
                    "suggested_replicas": "+2"
                }
            elif change_ratio < -0.3:
                return {
                    "action": "scale_down", 
                    "reason": f"预计负载将减少{abs(change_ratio):.1%}",
                    "suggested_replicas": "-1"
                }
            else:
                return {
                    "action": "maintain",
                    "reason": "负载变化不大，维持当前配置",
                    "suggested_replicas": "0"
                }
                
        except Exception as e:
            logger.error(f"生成建议失败: {e}")
            return {"action": "maintain", "reason": "无法分析负载变化"}

    def _get_hour_factor(self, hour: int) -> float:
        """获取小时因子"""
        return HOUR_FACTORS.get(hour, 1.0)

    def _get_day_factor(self, day_of_week: int) -> float:
        """获取星期因子"""
        return DAY_FACTORS.get(day_of_week, 1.0)

    async def predict_trend(self, service_name: str, namespace: str = "default") -> Dict[str, Any]:
        """预测趋势"""
        try:
            # 获取多个时间点的预测
            predictions = []
            for minutes in [15, 30, 60, 120]:
                pred = await self.predict(service_name, namespace, minutes)
                predictions.append({
                    "time_minutes": minutes,
                    "predicted_qps": pred.get("predicted_qps", 0.0)
                })
            
            return {
                "service_name": service_name,
                "namespace": namespace,
                "trend_predictions": predictions,
                "trend_analysis": self._analyze_trend(predictions)
            }
            
        except Exception as e:
            logger.error(f"趋势预测失败: {e}")
            return {"error": str(e)}

    def _analyze_trend(self, predictions: List[Dict]) -> str:
        """分析趋势"""
        if len(predictions) < 2:
            return "数据不足"
        
        values = [p["predicted_qps"] for p in predictions]
        
        if values[-1] > values[0] * 1.2:
            return "上升趋势"
        elif values[-1] < values[0] * 0.8:
            return "下降趋势"
        else:
            return "平稳趋势"

    def is_healthy(self) -> bool:
        """检查服务健康状态"""
        return self.prometheus.is_healthy()

    def get_service_info(self) -> Dict[str, Any]:
        """获取服务信息"""
        return {
            "service_name": "PredictionService",
            "model_loaded": self.model_loaded,
            "scaler_loaded": self.scaler_loaded,
            "prometheus_healthy": self.prometheus.is_healthy(),
            "status": "healthy" if self.is_healthy() else "unhealthy"
        }

    async def reload_models(self) -> bool:
        """重新加载模型"""
        try:
            self._initialize()
            return self.model_loaded and self.scaler_loaded
        except Exception as e:
            logger.error(f"重新加载模型失败: {e}")
            return False