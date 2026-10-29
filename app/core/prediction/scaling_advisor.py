#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps扩缩容建议器 - 生成智能扩缩容建议
"""

from datetime import datetime
import logging
from typing import Dict, List, Optional

import numpy as np

from app.models import (
    PredictionDataPoint,
    PredictionType,
    ResourceConstraints,
    ScalingAction,
    ScalingRecommendation,
)

logger = logging.getLogger("aiops.core.scaling_advisor")


class ScalingAdvisor:
    """扩缩容建议器"""

    def __init__(self):
        pass

        # 从配置文件获取扩缩容阈值
        self.thresholds = self._init_thresholds()

        # 从配置文件获取冷却时间
        self.cooldown_periods = self._init_cooldown_periods()

    def _init_thresholds(self) -> Dict[PredictionType, Dict[str, float]]:
        """初始化扩缩容阈值"""
        from app.config.settings import config

        config_thresholds = config.prediction.scaling_thresholds
        default_thresholds = {
            PredictionType.QPS: {
                "scale_up": 1000,  # QPS超过1000需要扩容
                "scale_down": 200,  # QPS低于200可以缩容
                "per_instance": 500,  # 每个实例处理500 QPS
            },
            PredictionType.CPU: {
                "scale_up": 80,  # CPU超过80%需要扩容
                "scale_down": 30,  # CPU低于30%可以缩容
                "optimal": 60,  # 最优CPU利用率
            },
            PredictionType.MEMORY: {
                "scale_up": 85,  # 内存超过85%需要扩容
                "scale_down": 40,  # 内存低于40%可以缩容
                "optimal": 70,  # 最优内存利用率
            },
            PredictionType.DISK: {
                "scale_up": 90,  # 磁盘超过90%需要扩容
                "scale_down": 50,  # 磁盘低于50%可以缩容
                "optimal": 75,  # 最优磁盘利用率
            },
        }

        # 合并配置文件和默认值
        result = {}
        for pred_type in PredictionType:
            pred_type_key = pred_type.value.lower()
            if pred_type_key in config_thresholds:
                result[pred_type] = config_thresholds[pred_type_key]
            else:
                result[pred_type] = default_thresholds[pred_type]

        return result

    def _init_cooldown_periods(self) -> Dict[str, int]:
        """初始化冷却时间"""
        from app.config.settings import config

        config_cooldown = config.prediction.cooldown_periods
        default_cooldown = {"scale_up": 5, "scale_down": 15}

        return config_cooldown if config_cooldown else default_cooldown

    async def generate_recommendations(
        self,
        predictions: List[PredictionDataPoint],
        prediction_type: PredictionType,
        target_utilization: float = 0.7,
        constraints: Optional[ResourceConstraints] = None,
    ) -> List[ScalingRecommendation]:
        """生成扩缩容建议"""

        if not predictions:
            return []

        try:
            recommendations = []
            last_action_time = None

            for i, prediction in enumerate(predictions):
                # 检查冷却期
                if last_action_time and self._in_cooldown(
                    prediction.timestamp, last_action_time, "scale_up"
                ):
                    continue

                # 评估是否需要扩缩容
                action = self._evaluate_scaling_need(
                    prediction.predicted_value, prediction_type, target_utilization
                )

                if action != ScalingAction.MAINTAIN:
                    # 生成建议
                    recommendation = self._create_recommendation(
                        action=action,
                        timestamp=prediction.timestamp,
                        prediction=prediction,
                        prediction_type=prediction_type,
                        target_utilization=target_utilization,
                        constraints=constraints,
                    )

                    if recommendation:
                        recommendations.append(recommendation)
                        last_action_time = prediction.timestamp

            # 优化建议（合并相近的建议）
            recommendations = self._optimize_recommendations(recommendations)

            return recommendations

        except Exception as e:
            logger.error("生成扩缩容建议失败: %s", type(e).__name__)
            raise

    def _evaluate_scaling_need(
        self,
        predicted_value: float,
        prediction_type: PredictionType,
        target_utilization: float,
    ) -> ScalingAction:
        """评估扩缩容需求"""

        thresholds = self.thresholds.get(prediction_type, {})

        if prediction_type == PredictionType.QPS:
            # QPS基于绝对值判断
            if predicted_value > thresholds.get("scale_up", 1000):
                return ScalingAction.SCALE_UP
            elif predicted_value < thresholds.get("scale_down", 200):
                return ScalingAction.SCALE_DOWN
        else:
            # 其他资源基于利用率判断
            scale_up_threshold = target_utilization * 100 + 10  # 目标利用率+10%
            scale_down_threshold = target_utilization * 100 - 20  # 目标利用率-20%

            if predicted_value > scale_up_threshold:
                return ScalingAction.SCALE_UP
            elif predicted_value < scale_down_threshold:
                return ScalingAction.SCALE_DOWN

        return ScalingAction.MAINTAIN

    def _create_recommendation(
        self,
        action: ScalingAction,
        timestamp: datetime,
        prediction: PredictionDataPoint,
        prediction_type: PredictionType,
        target_utilization: float,
        constraints: Optional[ResourceConstraints],
    ) -> Optional[ScalingRecommendation]:
        """创建扩缩容建议"""

        # 计算目标资源配置
        target_config = self._calculate_target_resources(
            current_value=prediction.predicted_value,
            action=action,
            prediction_type=prediction_type,
            target_utilization=target_utilization,
            constraints=constraints,
        )

        if not target_config:
            return None

        # 生成建议原因
        reason = self._generate_reason(
            action=action,
            prediction_type=prediction_type,
            current_value=prediction.predicted_value,
            target_config=target_config,
        )

        # 计算置信度
        confidence = self._calculate_recommendation_confidence(
            prediction=prediction, action=action
        )

        # 估算成本变化
        cost_change = self._estimate_cost_change(
            action=action, target_config=target_config, constraints=constraints
        )

        return ScalingRecommendation(
            action=action,
            trigger_time=timestamp,
            confidence=confidence,
            reason=reason,
            target_instances=target_config.get("instances"),
            target_cpu_cores=target_config.get("cpu_cores"),
            target_memory_gb=target_config.get("memory_gb"),
            target_disk_gb=target_config.get("disk_gb"),
            estimated_cost_change=cost_change,
        )

    def _calculate_target_resources(
        self,
        current_value: float,
        action: ScalingAction,
        prediction_type: PredictionType,
        target_utilization: float,
        constraints: Optional[ResourceConstraints],
    ) -> Optional[Dict[str, float]]:
        """计算目标资源配置"""

        config = {}

        if prediction_type == PredictionType.QPS:
            # 基于QPS计算实例数
            per_instance_qps = self.thresholds[PredictionType.QPS]["per_instance"]

            # 按目标利用率折算可承载QPS：更保守 -> 单实例可用容量 = per_instance_qps * target_utilization
            effective_capacity = max(1.0, per_instance_qps * max(0.1, min(0.9, target_utilization)))

            base_instances = int(np.ceil(current_value / effective_capacity))

            # 根据动作引入缓冲系数，扩容更保守，缩容更谨慎
            if action == ScalingAction.SCALE_UP:
                target_instances = int(np.ceil(base_instances * 1.2))
            elif action == ScalingAction.SCALE_DOWN:
                target_instances = int(np.ceil(max(1, base_instances * 0.8)))
            else:
                target_instances = base_instances

            # 应用请求级约束
            if constraints:
                if constraints.min_instances:
                    target_instances = max(target_instances, constraints.min_instances)
                if constraints.max_instances:
                    target_instances = min(target_instances, constraints.max_instances)

            # 应用全局实例数上下限
            try:
                from app.config.settings import config as _config

                global_min = int(getattr(_config.prediction, "min_instances", 1))
                global_max = int(getattr(_config.prediction, "max_instances", 20))
                target_instances = max(global_min, min(target_instances, global_max))
            except Exception:
                pass

            config["instances"] = max(1, target_instances)

        elif prediction_type == PredictionType.CPU:
            # 基于CPU计算核数
            if action == ScalingAction.SCALE_UP:
                target_cores = self._calculate_cpu_cores(current_value * 1.5)
            elif action == ScalingAction.SCALE_DOWN:
                target_cores = self._calculate_cpu_cores(current_value * 0.7)
            else:
                target_cores = self._calculate_cpu_cores(current_value)

            if constraints and constraints.cpu_cores:
                target_cores = min(target_cores, constraints.cpu_cores)

            config["cpu_cores"] = target_cores

        elif prediction_type == PredictionType.MEMORY:
            # 基于内存计算GB数
            if action == ScalingAction.SCALE_UP:
                target_memory = self._calculate_memory_gb(current_value * 1.5)
            elif action == ScalingAction.SCALE_DOWN:
                target_memory = self._calculate_memory_gb(current_value * 0.7)
            else:
                target_memory = self._calculate_memory_gb(current_value)

            if constraints and constraints.memory_gb:
                target_memory = min(target_memory, constraints.memory_gb)

            config["memory_gb"] = target_memory

        elif prediction_type == PredictionType.DISK:
            # 基于磁盘计算GB数
            if action == ScalingAction.SCALE_UP:
                target_disk = self._calculate_disk_gb(current_value * 1.5)
            elif action == ScalingAction.SCALE_DOWN:
                target_disk = self._calculate_disk_gb(current_value * 0.8)
            else:
                target_disk = self._calculate_disk_gb(current_value)

            if constraints and constraints.disk_gb:
                target_disk = min(target_disk, constraints.disk_gb)

            config["disk_gb"] = target_disk

        return config if config else None

    def _calculate_cpu_cores(self, utilization: float) -> float:
        """根据利用率计算CPU核数"""
        # 假设当前是4核，根据利用率计算需要的核数
        current_cores = 4
        if utilization > 80:
            return current_cores * (utilization / 60)  # 目标60%利用率
        elif utilization < 30:
            return max(1, current_cores * (utilization / 60))
        return current_cores

    def _calculate_memory_gb(self, utilization: float) -> float:
        """根据利用率计算内存GB"""
        # 假设当前是8GB，根据利用率计算需要的内存
        current_memory = 8
        if utilization > 85:
            return current_memory * (utilization / 70)  # 目标70%利用率
        elif utilization < 40:
            return max(2, current_memory * (utilization / 70))
        return current_memory

    def _calculate_disk_gb(self, utilization: float) -> float:
        """根据利用率计算磁盘GB"""
        # 假设当前是100GB，根据利用率计算需要的磁盘
        current_disk = 100
        if utilization > 90:
            return current_disk * (utilization / 75)  # 目标75%利用率
        elif utilization < 50:
            return max(50, current_disk * (utilization / 75))
        return current_disk

    def _generate_reason(
        self,
        action: ScalingAction,
        prediction_type: PredictionType,
        current_value: float,
        target_config: Dict[str, float],
    ) -> str:
        """生成建议原因"""

        type_name = prediction_type.value.upper()

        if action == ScalingAction.SCALE_UP:
            if prediction_type == PredictionType.QPS:
                return f"{type_name}预测值({current_value:.0f})超过阈值，建议扩容至{target_config.get('instances', 1)}个实例"
            else:
                return (
                    f"{type_name}利用率预测值({current_value:.1f}%)过高，建议增加资源"
                )
        elif action == ScalingAction.SCALE_DOWN:
            if prediction_type == PredictionType.QPS:
                return f"{type_name}预测值({current_value:.0f})低于阈值，可以缩容至{target_config.get('instances', 1)}个实例"
            else:
                return f"{type_name}利用率预测值({current_value:.1f}%)较低，可以减少资源节省成本"
        else:
            return f"{type_name}预测值({current_value:.1f})在正常范围内，维持当前配置"

    def _calculate_recommendation_confidence(
        self, prediction: PredictionDataPoint, action: ScalingAction
    ) -> Optional[float]:
        """没有经过建议级别的回测时，不推断建议置信度。"""
        return None

    def _estimate_cost_change(
        self,
        action: ScalingAction,
        target_config: Dict[str, float],
        constraints: Optional[ResourceConstraints],
    ) -> Optional[float]:
        """估算成本变化百分比"""

        if not constraints or not constraints.cost_per_hour:
            return None

        # 简单估算：扩容增加20-50%成本，缩容减少10-30%成本
        if action == ScalingAction.SCALE_UP:
            if "instances" in target_config:
                return 20.0 + (target_config["instances"] - 1) * 5
            return 30.0
        elif action == ScalingAction.SCALE_DOWN:
            if "instances" in target_config:
                return -10.0 - (1 / max(1, target_config["instances"])) * 10
            return -20.0

        return 0.0

    def _in_cooldown(
        self, current_time: datetime, last_action_time: datetime, action_type: str
    ) -> bool:
        """检查是否在冷却期内"""

        cooldown_minutes = self.cooldown_periods.get(action_type, 5)
        time_diff = (current_time - last_action_time).total_seconds() / 60

        return time_diff < cooldown_minutes

    def _optimize_recommendations(
        self, recommendations: List[ScalingRecommendation]
    ) -> List[ScalingRecommendation]:
        """优化建议列表"""

        if len(recommendations) <= 1:
            return recommendations

        optimized = []
        i = 0

        while i < len(recommendations):
            current = recommendations[i]

            # 查找相近时间的相同类型建议
            j = i + 1
            while j < len(recommendations):
                next_rec = recommendations[j]
                time_diff = (
                    next_rec.trigger_time - current.trigger_time
                ).total_seconds() / 3600

                # 如果在1小时内且是相同动作，合并
                if time_diff <= 1 and current.action == next_rec.action:
                    # 更新当前建议（取最大值）
                    if current.target_instances and next_rec.target_instances:
                        current.target_instances = max(
                            current.target_instances, next_rec.target_instances
                        )
                    j += 1
                else:
                    break

            optimized.append(current)
            i = j

        return optimized
