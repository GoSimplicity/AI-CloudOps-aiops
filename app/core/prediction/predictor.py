#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 预测模块 - 提供负载预测和机器学习模型功能
"""

import datetime
import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from app.config.settings import config  # 系统配置
from app.constants import DAY_FACTORS, HOUR_FACTORS, LOW_QPS_THRESHOLD  # 预测相关常量
from app.core.prediction.model_loader import ModelLoader  # 模型加载器
from app.services.prometheus import PrometheusService  # Prometheus数据服务
from app.utils.error_handlers import ErrorHandler  # 错误处理工具
from app.utils.time_utils import TimeUtils  # 时间工具类

logger = logging.getLogger("aiops.predictor")


class PredictionService:
    """
    负载预测服务类 - AI-CloudOps系统的核心预测引擎
    """

    def __init__(self):
        """
        初始化负载预测服务
        """
        # 初始化Prometheus数据服务，用于获取监控指标
        self.prometheus = PrometheusService()

        # 初始化模型加载器，负责ML模型的加载和管理
        self.model_loader = ModelLoader()

        # 模型和缩放器的加载状态标志
        self.model_loaded = False
        self.scaler_loaded = False

        # 初始化错误处理器，提供统一的异常处理
        self.error_handler = ErrorHandler(logger)

        # 执行服务初始化，加载必要的模型和配置
        self._initialize()

    def _initialize(self):
        """
        初始化预测服务内部组件
        """
        try:
            # 尝试加载预训练模型和相关组件
            success = self.model_loader.load_models()

            # 验证模型的完整性和可用性
            if success and self.model_loader.validate_model():
                # 模型和缩放器都成功加载
                self.model_loaded = True
                self.scaler_loaded = True
                logger.info("预测服务初始化成功，ML模型已就绪")
            else:
                # 模型加载失败，服务将使用基于规则的预测
                logger.error("预测服务初始化失败，将使用基于规则的预测")
        except Exception as e:
            # 初始化过程中出现异常，记录错误并设置服务状态
            logger.error(f"预测服务初始化异常: {str(e)}")
            self.model_loaded = False
            self.scaler_loaded = False

    async def predict(
        self,
        current_qps: Optional[float] = None,
        timestamp: Optional[datetime.datetime] = None,
        include_features: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        执行实例数预测 - 预测服务的核心方法
        """
        try:
            # 步骤1：健康检查 - 确保预测服务处于可用状态
            if not self.is_healthy():
                logger.error("预测服务不健康，无法执行预测")
                return None

            # 步骤2：时间参数处理 - 获取预测时间点
            if timestamp is None:
                timestamp = datetime.datetime.now()

            # 步骤3：QPS数据获取 - 从Prometheus获取当前QPS或使用传入值
            if current_qps is None:
                current_qps = await self._get_current_qps()
                logger.info(f"从Prometheus获取当前QPS: {current_qps}")

            # 步骤4：QPS数据验证 - 确保QPS值合理且非负
            if current_qps < 0:
                logger.warning(f"QPS值异常: {current_qps}, 使用0")
                current_qps = 0

            # 步骤5：低流量处理 - QPS为0或极低时直接返回最小实例数
            if current_qps == 0 or current_qps < LOW_QPS_THRESHOLD:
                logger.info(
                    f"当前QPS({current_qps})低于阈值{LOW_QPS_THRESHOLD}，返回最小实例数: {config.prediction.min_instances}"
                )
                return {
                    "instances": config.prediction.min_instances,
                    "current_qps": round(current_qps, 2),
                    "timestamp": timestamp.isoformat(),
                    "confidence": 0.95,  # 低流量场景置信度很高
                    "model_version": self.model_loader.model_metadata.get(
                        "version", "1.0"
                    ),
                    "prediction_type": "threshold_based",  # 基于阈值的预测
                }

            # 步骤6：时间特征提取 - 从时间戳中提取各种时间模式特征
            time_features = TimeUtils.extract_time_features(timestamp)

            # 步骤7：历史数据收集 - 获取1小时、1天、1周前的QPS数据用于趋势分析
            qps_1h_ago = await self._get_historical_qps(
                timestamp - datetime.timedelta(hours=1)
            )
            qps_1d_ago = await self._get_historical_qps(
                timestamp - datetime.timedelta(days=1)
            )
            qps_1w_ago = await self._get_historical_qps(
                timestamp - datetime.timedelta(weeks=1)
            )

            # 步骤8：历史数据处理 - 确保历史QPS数据有效，否则使用合理的估计值
            # 如果无法获取历史数据，使用当前QPS的合理估计值
            qps_1h_ago = (
                qps_1h_ago if qps_1h_ago is not None else max(0, current_qps * 0.9)
            )
            qps_1d_ago = (
                qps_1d_ago if qps_1d_ago is not None else max(0, current_qps * 1.0)
            )
            qps_1w_ago = (
                qps_1w_ago if qps_1w_ago is not None else max(0, current_qps * 1.0)
            )

            # 步骤9：衍生特征计算 - 计算QPS变化率，反映流量趋势
            qps_change = (current_qps - qps_1h_ago) / max(
                1.0, qps_1h_ago
            )  # 避免除零错误

            # 步骤10：近期流量统计 - 计算最近6小时的平均QPS
            recent_qps_data = await self._get_recent_qps_data(timestamp, hours=6)
            if recent_qps_data and len(recent_qps_data) > 0:
                qps_avg_6h = sum(recent_qps_data) / len(recent_qps_data)
            else:
                qps_avg_6h = current_qps  # 如果无法获取历史数据，使用当前值

            # 步骤11：特征向量构建 - 构建用于机器学习模型的多维特征向量
            # 特征包括：当前QPS、时间特征、历史QPS、变化率等
            features_dict = {
                "QPS": [current_qps],  # 当前QPS值
                "sin_time": [time_features["sin_time"]],  # 时间的正弦编码
                "cos_time": [time_features["cos_time"]],  # 时间的余弦编码
                "sin_day": [time_features["sin_day"]],  # 日期的正弦编码
                "cos_day": [time_features["cos_day"]],  # 日期的余弦编码
                "is_business_hour": [
                    int(time_features["is_business_hour"])
                ],  # 是否工作时间
                "is_weekend": [int(time_features["is_weekend"])],  # 是否周末
                "QPS_1h_ago": [qps_1h_ago],  # 1小时前的QPS
                "QPS_1d_ago": [qps_1d_ago],  # 1天前的QPS
                "QPS_1w_ago": [qps_1w_ago],  # 1周前的QPS
                "QPS_change": [qps_change],  # QPS变化率
                "QPS_avg_6h": [qps_avg_6h],  # 6小时平均QPS
            }

            # 步骤12：特征DataFrame创建 - 将特征字典转换为pandas DataFrame
            try:
                features = pd.DataFrame(features_dict)

                # 步骤13：特征对齐 - 检查是否需要添加额外特征以匹配模型期望
                model_features = self.model_loader.model_metadata.get("features", [])
                for feature in model_features:
                    if feature not in features.columns:
                        logger.warning(
                            f"模型期望特征 '{feature}' 不在当前特征集中，添加默认值0"
                        )
                        features[feature] = 0.0

                # 步骤14：特征排序 - 确保特征顺序与模型训练时一致
                features = features[model_features]

            except Exception as e:
                logger.error(f"创建特征DataFrame失败: {str(e)}")
                return None

            # 步骤15：特征标准化 - 使用预训练的标准化器对特征进行缩放
            try:
                features_scaled = self.model_loader.scaler.transform(features)
            except Exception as e:
                logger.error(f"特征标准化失败: {str(e)}")
                return None

            # 步骤16：模型推理 - 使用训练好的机器学习模型进行预测
            try:
                prediction = self.model_loader.model.predict(features_scaled)[0]
            except Exception as e:
                logger.error(f"模型预测失败: {str(e)}")
                return None

            # 步骤17：结果处理 - 限制实例数范围并四舍五入（实例数应为整数）
            instances = int(
                np.clip(
                    np.round(prediction),
                    config.prediction.min_instances,
                    config.prediction.max_instances,
                )
            )

            # 步骤18：置信度计算 - 基于多个因素计算预测结果的置信度
            confidence = self._calculate_confidence(
                current_qps, time_features, prediction
            )

            logger.info(
                f"预测完成: QPS={current_qps:.2f}, 实例数={instances}, 置信度={confidence:.2f}"
            )

            # 步骤19：预测结果构建 - 构建标准化的预测结果
            result = {
                "instances": instances,  # 预测的实例数量
                "current_qps": round(current_qps, 2),  # 当前QPS（保留2位小数）
                "timestamp": timestamp.isoformat(),  # 预测时间（ISO格式）
                "confidence": confidence,  # 预测置信度
                "model_version": self.model_loader.model_metadata.get(
                    "version", "1.0"
                ),  # 模型版本
                "prediction_type": "model_based",  # 预测类型：基于模型
            }

            # 步骤20：特征详情添加 - 如果请求包含特征信息，添加详细的特征数据
            if include_features:
                result["features"] = {
                    "qps": current_qps,  # 当前QPS
                    "sin_time": time_features["sin_time"],  # 时间正弦值
                    "cos_time": time_features["cos_time"],  # 时间余弦值
                    "hour": time_features["hour"],  # 小时
                    "is_business_hour": time_features[
                        "is_business_hour"
                    ],  # 是否工作时间
                    "is_weekend": time_features["is_weekend"],  # 是否周末
                    "sin_day": features_dict["sin_day"][0],  # 日期正弦值
                    "cos_day": features_dict["cos_day"][0],  # 日期余弦值
                    "qps_1h_ago": qps_1h_ago,  # 1小时前QPS
                    "qps_1d_ago": qps_1d_ago,  # 1天前QPS
                    "qps_1w_ago": qps_1w_ago,  # 1周前QPS
                    "qps_change": qps_change,  # QPS变化率
                    "qps_avg_6h": qps_avg_6h,  # 6小时平均QPS
                }

            return result

        except Exception as e:
            # 异常处理：记录错误并返回None，调用方可以决定降级策略
            logger.error(f"预测失败: {str(e)}")
            return None

    async def _get_current_qps(self) -> float:
        """
        从Prometheus获取当前QPS - 实时数据获取方法
        """
        try:
            # 使用配置中的Prometheus查询语句
            query = config.prediction.prometheus_query

            # 执行即时查询，获取当前时间点的QPS值
            result = await self.prometheus.query_instant(query)

            # 处理查询结果
            if result and len(result) > 0:
                # 从Prometheus结果中提取QPS值
                qps = float(result[0]["value"][1])
                logger.debug(f"从Prometheus获取QPS: {qps}")
                return max(0, qps)  # 确保非负，避免异常数据
            else:
                # 查询结果为空，记录警告并返回默认值
                logger.warning(f"未能从Prometheus获取QPS，使用默认值0: {query}")
                return 0.0

        except Exception as e:
            # 异常处理：记录错误并返回安全的默认值
            logger.error(f"获取QPS失败: {str(e)}")
            return 0.0

    async def _get_historical_qps(
        self, timestamp: datetime.datetime
    ) -> Optional[float]:
        """
        获取指定时间点的历史QPS数据 - 历史数据回溯方法
        """
        try:
            # 使用配置中的Prometheus查询语句
            query = config.prediction.prometheus_query

            # 查询指定时间点的QPS - 使用时间参数的即时查询
            result = await self.prometheus.query_instant(query, timestamp)

            # 处理查询结果
            if result and len(result) > 0:
                # 从Prometheus结果中提取历史QPS值
                qps = float(result[0]["value"][1])
                logger.debug(
                    f"从Prometheus获取历史QPS ({timestamp.isoformat()}): {qps}"
                )
                return max(0, qps)  # 确保非负，避免异常数据
            else:
                # 指定时间点没有数据，这在时间跨度较大时可能发生
                logger.warning(f"未能从Prometheus获取历史QPS ({timestamp.isoformat()})")
                return None

        except Exception as e:
            # 异常处理：记录错误并返回None，调用方可以使用估算值
            logger.error(f"获取历史QPS失败 ({timestamp.isoformat()}): {str(e)}")
            return None

    async def _get_recent_qps_data(
        self, end_time: datetime.datetime, hours: int = 6
    ) -> List[float]:
        """
        获取最近几小时的QPS数据序列 - 时间序列数据获取方法
        """
        try:
            # 使用配置中的Prometheus查询语句
            query = config.prediction.prometheus_query

            # 计算开始时间
            start_time = end_time - datetime.timedelta(hours=hours)

            # 使用范围查询获取一段时间内的QPS数据
            # 30分钟间隔提供良好的数据粒度，避免查询过载
            df = await self.prometheus.query_range(
                query=query,
                start_time=start_time,
                end_time=end_time,
                step="30m",  # 每30分钟一个数据点，平衡精度和性能
            )

            # 处理查询结果
            if df is not None and not df.empty and "value" in df.columns:
                # 转换为浮点数列表，确保数据类型正确
                values = df["value"].tolist()
                logger.debug(f"获取到{len(values)}个历史QPS数据点")
                return [max(0, float(v)) for v in values]  # 确保所有值非负
            else:
                # 查询结果为空，可能是时间范围内没有数据
                logger.warning(f"未能从Prometheus获取最近{hours}小时的QPS数据")
                return []

        except Exception as e:
            # 异常处理：记录错误并返回空列表
            logger.error(f"获取最近QPS数据失败: {str(e)}")
            return []

    def _calculate_confidence(
        self, qps: float, time_features: dict, prediction: float
    ) -> float:
        """
        计算预测置信度 - 多因素置信度评估算法
        """
        try:
            # 初始化置信度因子列表，用于存储各个维度的置信度分数
            confidence_factors = []

            # 因子1：基于QPS值的稳定性评估
            # 不同流量区间的可预测性不同
            if qps <= 100:
                qps_confidence = 0.9  # 低流量场景相对稳定
            elif qps <= 500:
                qps_confidence = 0.8  # 中等流量场景
            elif qps <= 1000:
                qps_confidence = 0.7  # 中高流量场景
            else:
                qps_confidence = 0.6  # 高流量场景，影响因子复杂
            confidence_factors.append(qps_confidence)

            # 因子2：基于时间特征的稳定性评估
            # 不同时间段的流量模式可预测性不同
            hour = time_features.get("hour", 12)
            is_weekend = time_features.get("is_weekend", False)
            is_holiday = time_features.get("is_holiday", False)

            # 工作日/周末/节假日判断
            if is_holiday:
                time_confidence = 0.7  # 节假日预测较不稳定，流量模式可能变化
            elif is_weekend:
                if 10 <= hour <= 20:  # 周末白天，流量相对稳定
                    time_confidence = 0.75
                else:  # 周末夜间，流量通常较低且稳定
                    time_confidence = 0.85
            else:
                # 工作日的不同时间段
                if time_features.get("is_business_hour", False):
                    time_confidence = 0.9  # 工作时间预测相对稳定
                elif 22 <= hour or hour <= 6:
                    time_confidence = 0.85  # 深夜时间比较稳定，流量低
                else:
                    time_confidence = 0.8  # 其他时间
            confidence_factors.append(time_confidence)

            # 因子3：基于模型元数据的稳定性评估
            # 较新的模型通常有更好的性能和适应性
            model_age_days = self._get_model_age_days()
            if model_age_days <= 7:
                model_confidence = 0.95  # 新模型可信度高，数据新鲜
            elif model_age_days <= 30:
                model_confidence = 0.85  # 较新模型可信度较高
            elif model_age_days <= 90:
                model_confidence = 0.75  # 中等年龄模型
            else:
                model_confidence = 0.65  # 旧模型可信度较低，可能过时
            confidence_factors.append(model_confidence)

            # 因子4：综合置信度计算
            # 使用加权平均算法计算综合置信度
            confidence = sum(confidence_factors) / len(confidence_factors)

            # 特殊场景优化：低流量场景下有更高的置信度
            # 因为规则更简单、影响因子更少
            if qps < 5.0:
                confidence = max(confidence, 0.95)

            # 返回四舍五入到小数2位的置信度分数
            return round(confidence, 2)

        except Exception as e:
            # 异常处理：计算失败时返回中等置信度
            logger.error(f"计算置信度失败: {str(e)}")
            return 0.8  # 默认中等置信度

    def _get_model_age_days(self) -> int:
        """
        获取模型的年龄（天数） - 模型时效性评估方法
        """
        try:
            # 从模型元数据中获取创建时间
            created_at = self.model_loader.model_metadata.get("created_at")
            if not created_at:
                # 如果没有创建时间信息，假设模型很旧
                return 999  # 未知年龄，假设很旧，降低置信度

            # 解析创建时间并计算年龄
            created_date = datetime.datetime.fromisoformat(created_at)
            age_days = (datetime.datetime.now() - created_date).days

            # 返回非负的年龄天数
            return max(0, age_days)

        except Exception:
            # 解析失败，返回默认值，不影响整体置信度计算
            return 30  # 解析失败，返回默认值

    async def predict_trend(
        self, hours_ahead: int = 24, current_qps: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """
        预测未来QPS趋势和实例数需求 - 多时间点趋势预测方法
        """
        try:
            # 步骤1：服务健康检查 - 确保预测服务可用
            if not self.is_healthy():
                logger.error("预测服务不健康，无法执行趋势预测")
                return None

            # 步骤2：参数验证和范围限制 - 限制预测时长在合理范围内
            hours_ahead = min(168, max(1, hours_ahead))  # 限制在1-168小时(一周)内

            # 步骤3：基础数据获取 - 获取当前QPS和时间
            now = datetime.datetime.now()
            if current_qps is None:
                current_qps = await self._get_current_qps()

            # 步骤4：数据验证 - 验证QPS值的合理性
            if current_qps < 0:
                logger.warning(f"QPS值异常: {current_qps}, 使用0")
                current_qps = 0

            # 步骤5：历史数据收集 - 获取历史QPS数据，用于预测趋势
            # 使用24小时的历史数据来识别日周期模式
            historical_data = await self._get_recent_qps_data(now, hours=24)

            # 步骤6：趋势预测循环 - 为每个未来时间点生成预测
            # 初始化预测结果容器和初始值
            forecast = []
            predicted_qps = current_qps

            # 逐小时预测未来趋势
            for hour in range(hours_ahead):
                # 计算未来时间点
                future_time = now + datetime.timedelta(hours=hour)

                # 应用时间模式因子 - 根据小时和星期几调整预测值
                hour_factor = self._get_hour_factor(future_time.hour)
                day_factor = self._get_day_factor(future_time.weekday())

                # 根据历史数据可用性选择预测策略
                if len(historical_data) > 0:
                    # 策略A：具有历史数据时的预测算法
                    # 使用历史模式进行预测
                    base_qps = sum(historical_data) / len(historical_data)
                    time_pattern = hour_factor * day_factor
                    predicted_qps = base_qps * time_pattern

                    # 添加一些随机波动模拟真实业务变化
                    # 实际模型应该更复杂，这里使用简单的波动模拟
                    variation = 0.1  # 10%的波动系数
                    predicted_qps *= 1 + (np.random.random() - 0.5) * variation
                else:
                    # 策略B：缺乏历史数据时的预测算法
                    # 如果没有历史数据，使用当前QPS和时间模式
                    time_pattern = hour_factor * day_factor
                    predicted_qps = current_qps * time_pattern

                # 数据合理性保障 - 确保QPS非负
                predicted_qps = max(0, predicted_qps)

                # 步骤7：实例数预测 - 基于预测的QPS计算所需实例数
                # 调用主预测方法来计算对应的实例数量
                prediction_result = await self.predict(
                    current_qps=predicted_qps, timestamp=future_time
                )

                # 获取实例数预测结果，如果预测失败则使用最小实例数
                instances = (
                    prediction_result.get("instances", config.prediction.min_instances)
                    if prediction_result
                    else config.prediction.min_instances
                )

                # 步骤8：结果记录 - 将当前时间点的预测结果添加到预测列表
                forecast.append(
                    {
                        "timestamp": future_time.isoformat(),  # 时间点（ISO格式）
                        "qps": round(predicted_qps, 2),  # 预测QPS（保留2位小数）
                        "instances": instances,  # 预测实例数
                    }
                )

            # 步骤9：结果整合 - 构建完整的趋势预测结果
            return {
                "forecast": forecast,  # 详细的预测数据列表
                "current_qps": current_qps,  # 当前QPS基础值
                "hours_ahead": hours_ahead,  # 预测时长
                "timestamp": now.isoformat(),  # 预测生成时间
            }

        except Exception as e:
            # 异常处理：记录错误并返回None
            logger.error(f"趋势预测失败: {str(e)}")
            return None

    def _get_hour_factor(self, hour: int) -> float:
        """
        根据小时获取QPS乘数 - 时间模式因子获取方法
        """
        # 从常量配置中获取时间因子，如果找不到则使用默认值
        return HOUR_FACTORS.get(hour, 0.5)  # 默认为0.5，表示中等流量水平

    def _get_day_factor(self, day_of_week: int) -> float:
        """
        根据星期几获取QPS乘数 - 星期模式因子获取方法
        """
        # 从常量配置中获取星期因子，如果找不到则使用默认值
        return DAY_FACTORS.get(day_of_week, 1.0)  # 默认为1.0，表示正常流量水平

    def is_healthy(self) -> bool:
        """
        检查预测服务健康状态 - 服务可用性检查方法
        """
        # 同时检查模型和缩放器的加载状态
        return self.model_loaded and self.scaler_loaded

    def get_service_info(self) -> Dict[str, Any]:
        """
        获取服务信息 - 服务状态信息获取方法
        """
        # 获取模型加载器的详细信息
        model_info = self.model_loader.get_model_info()

        # 构建综合服务信息字典
        return {
            "healthy": self.is_healthy(),  # 整体健康状态
            "model_loaded": self.model_loaded,  # 模型加载状态
            "scaler_loaded": self.scaler_loaded,  # 缩放器加载状态
            "model_info": model_info,  # 模型详细信息
            "model_age_days": self._get_model_age_days(),  # 模型年龄
        }

    async def reload_models(self) -> bool:
        """
        重新加载模型 - 模型热更新方法
        """
        logger.info("重新加载预测模型...")
        try:
            # 调用模型加载器的重新加载功能
            success = self.model_loader.reload_models()

            if success:
                # 重新加载成功，更新服务状态
                self.model_loaded = True
                self.scaler_loaded = True
                logger.info("模型重新加载成功")
            else:
                # 重新加载失败，记录错误日志
                logger.error("模型重新加载失败")

            return success

        except Exception as e:
            # 异常处理：记录错误并设置服务为不可用状态
            logger.error(f"重新加载模型失败: {str(e)}")
            self.model_loaded = False
            self.scaler_loaded = False
            return False
