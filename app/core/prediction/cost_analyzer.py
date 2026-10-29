#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps成本分析器 - 分析和优化云资源成本
"""

from datetime import datetime
import logging
from typing import Any, Dict, List, Optional

import numpy as np

from app.models import (
    CostAnalysis,
    PredictionDataPoint,
    ResourceConstraints,
    ScalingRecommendation,
)

logger = logging.getLogger("aiops.core.cost_analyzer")


class CostAnalyzer:
    """成本分析器"""

    def __init__(self):
        from app.config.settings import config

        # 从配置文件获取定价信息
        self.default_pricing = self._init_pricing()

        # 从配置获取折扣策略
        cost_config = config.prediction.cost_analysis_config
        self.discount_tiers = cost_config.get(
            "discount_tiers",
            [
                {"hours": 730, "discount": 0.1},
                {"hours": 2190, "discount": 0.2},
                {"hours": 8760, "discount": 0.3},
            ],
        )

    def _init_pricing(self) -> Dict[str, Any]:
        """初始化定价配置"""
        from app.config.settings import config

        # 从配置文件获取成本分析配置
        cost_config = config.prediction.cost_analysis_config

        # 使用配置中的定价，如果不存在则使用默认值
        return cost_config.get(
            "default_pricing",
            {
                "instance": {
                    "small": 0.02,
                    "medium": 0.04,
                    "large": 0.08,
                    "xlarge": 0.16,
                },
                "cpu_per_core": 0.02,
                "memory_per_gb": 0.005,
                "disk_per_gb": 0.0001,
                "bandwidth_per_gb": 0.01,
            },
        )

    async def analyze_cost(
        self,
        predictions: List[PredictionDataPoint],
        scaling_recommendations: List[ScalingRecommendation],
        constraints: ResourceConstraints,
    ) -> CostAnalysis:
        """分析成本"""

        try:
            # 计算当前成本
            current_cost = self._calculate_current_cost(constraints)

            # 计算预测成本
            predicted_cost = self._calculate_predicted_cost(
                predictions, scaling_recommendations, constraints
            )

            # 计算节省潜力
            savings_potential = self._calculate_savings_potential(
                current_cost, predicted_cost, scaling_recommendations
            )

            # 趋势分析
            trend_analysis = self._analyze_cost_trend(
                predictions, scaling_recommendations, current_cost
            )

            return CostAnalysis(
                current_hourly_cost=current_cost,
                predicted_hourly_cost=predicted_cost,
                cost_savings_potential=savings_potential,
                cost_trend_analysis=trend_analysis,
            )

        except Exception as e:
            logger.error("成本分析失败: %s", type(e).__name__)
            raise

    def _calculate_current_cost(self, constraints: ResourceConstraints) -> float:
        """计算当前成本"""

        if constraints and constraints.cost_per_hour:
            return constraints.cost_per_hour

        # 基于资源配置估算
        cost = 0.0

        if constraints:
            if constraints.cpu_cores:
                cost += constraints.cpu_cores * self.default_pricing["cpu_per_core"]
            if constraints.memory_gb:
                cost += constraints.memory_gb * self.default_pricing["memory_per_gb"]
            if constraints.disk_gb:
                cost += constraints.disk_gb * self.default_pricing["disk_per_gb"]
            if constraints.min_instances:
                # 假设使用medium实例
                cost += (
                    constraints.min_instances
                    * self.default_pricing["instance"]["medium"]
                )

        return cost if cost > 0 else 0.1  # 默认最小成本

    def _calculate_predicted_cost(
        self,
        predictions: List[PredictionDataPoint],
        scaling_recommendations: List[ScalingRecommendation],
        constraints: ResourceConstraints,
    ) -> float:
        """计算预测成本"""

        if not predictions:
            return self._calculate_current_cost(constraints)

        # 基于扩缩容建议计算平均成本
        total_cost = 0.0
        cost_points = []

        current_cost = self._calculate_current_cost(constraints)

        for prediction in predictions:
            # 查找对应时间的扩缩容建议
            recommendation = self._find_recommendation_for_time(
                prediction.timestamp, scaling_recommendations
            )

            if recommendation:
                # 根据建议调整成本
                adjusted_cost = self._adjust_cost_by_recommendation(
                    current_cost, recommendation
                )
                cost_points.append(adjusted_cost)
            else:
                cost_points.append(current_cost)

        # 计算平均成本
        if cost_points:
            total_cost = np.mean(cost_points)
        else:
            total_cost = current_cost

        return total_cost

    def _calculate_savings_potential(
        self,
        current_cost: float,
        predicted_cost: float,
        scaling_recommendations: List[ScalingRecommendation],
    ) -> float:
        """计算节省潜力百分比"""

        if current_cost == 0:
            return 0.0

        # 计算基础节省
        base_savings = ((current_cost - predicted_cost) / current_cost) * 100

        # 考虑缩容建议的额外节省
        scale_down_count = sum(
            1 for r in scaling_recommendations if r.action.value == "scale_down"
        )

        if scale_down_count > 0:
            # 每个缩容建议可能节省5-10%
            additional_savings = min(scale_down_count * 5, 30)
            base_savings += additional_savings

        # 限制在合理范围内
        return max(-50, min(50, base_savings))

    def _analyze_cost_trend(
        self,
        predictions: List[PredictionDataPoint],
        scaling_recommendations: List[ScalingRecommendation],
        current_cost: float,
    ) -> Dict[str, Any]:
        """分析成本趋势"""

        trend_data = {
            "trend_direction": "stable",
            "peak_cost_time": None,
            "lowest_cost_time": None,
            "cost_volatility": 0.0,
            "optimization_opportunities": [],
            "hourly_breakdown": [],
        }

        if not predictions:
            return trend_data

        # 计算每个时间点的成本
        hourly_costs = []
        for prediction in predictions:
            recommendation = self._find_recommendation_for_time(
                prediction.timestamp, scaling_recommendations
            )

            if recommendation:
                cost = self._adjust_cost_by_recommendation(current_cost, recommendation)
            else:
                cost = current_cost

            hourly_costs.append(
                {
                    "timestamp": prediction.timestamp.isoformat(),
                    "cost": cost,
                    "has_recommendation": recommendation is not None,
                }
            )

        trend_data["hourly_breakdown"] = hourly_costs

        # 分析趋势
        costs = [h["cost"] for h in hourly_costs]
        if costs:
            # 峰值和谷值
            max_idx = np.argmax(costs)
            min_idx = np.argmin(costs)
            trend_data["peak_cost_time"] = predictions[max_idx].timestamp.isoformat()
            trend_data["lowest_cost_time"] = predictions[min_idx].timestamp.isoformat()

            # 波动性
            trend_data["cost_volatility"] = float(np.std(costs) / np.mean(costs))

            # 趋势方向
            first_half_avg = np.mean(costs[: len(costs) // 2])
            second_half_avg = np.mean(costs[len(costs) // 2 :])

            if second_half_avg > first_half_avg * 1.1:
                trend_data["trend_direction"] = "increasing"
            elif second_half_avg < first_half_avg * 0.9:
                trend_data["trend_direction"] = "decreasing"
            else:
                trend_data["trend_direction"] = "stable"

        # 优化机会
        trend_data["optimization_opportunities"] = (
            self._identify_optimization_opportunities(
                predictions, scaling_recommendations
            )
        )

        return trend_data

    def _find_recommendation_for_time(
        self, timestamp: datetime, recommendations: List[ScalingRecommendation]
    ) -> Optional[ScalingRecommendation]:
        """查找特定时间的扩缩容建议"""

        for rec in recommendations:
            # 如果时间差在1小时内，认为是对应的建议
            time_diff = abs((rec.trigger_time - timestamp).total_seconds())
            if time_diff <= 3600:
                return rec

        return None

    def _adjust_cost_by_recommendation(
        self, base_cost: float, recommendation: ScalingRecommendation
    ) -> float:
        """根据建议调整成本"""

        if recommendation.estimated_cost_change is not None:
            # 使用建议中的成本变化估算
            return base_cost * (1 + recommendation.estimated_cost_change / 100)

        # 根据动作类型估算
        if recommendation.action.value == "scale_up":
            # 扩容增加成本
            if recommendation.target_instances:
                return base_cost * (1 + 0.2 * recommendation.target_instances)
            return base_cost * 1.3
        elif recommendation.action.value == "scale_down":
            # 缩容减少成本
            if recommendation.target_instances:
                return base_cost * (0.7 + 0.05 * recommendation.target_instances)
            return base_cost * 0.7

        return base_cost

    def _identify_optimization_opportunities(
        self,
        predictions: List[PredictionDataPoint],
        scaling_recommendations: List[ScalingRecommendation],
    ) -> List[str]:
        """识别优化机会"""

        opportunities = []

        # 检查是否有频繁的扩缩容
        if len(scaling_recommendations) > len(predictions) * 0.3:
            opportunities.append("频繁扩缩容导致成本波动，建议优化扩缩容策略")

        # 检查是否有连续的缩容机会
        consecutive_scale_down = 0
        for rec in scaling_recommendations:
            if rec.action.value == "scale_down":
                consecutive_scale_down += 1
            else:
                consecutive_scale_down = 0

            if consecutive_scale_down >= 3:
                opportunities.append("存在持续的资源过剩，建议调整基础配置")
                break

        # 检查预测值的稳定性
        if predictions:
            values = [p.predicted_value for p in predictions]
            cv = np.std(values) / np.mean(values) if np.mean(values) > 0 else 0

            if cv < 0.1:
                opportunities.append("负载相对稳定，可以考虑使用预留实例节省成本")
            elif cv > 0.5:
                opportunities.append("负载波动较大，建议使用弹性伸缩和按需实例")

        # 检查是否可以使用竞价实例
        scale_down_ratio = sum(
            1 for r in scaling_recommendations if r.action.value == "scale_down"
        ) / max(1, len(scaling_recommendations))
        if scale_down_ratio > 0.3:
            opportunities.append("部分负载可以使用竞价实例降低成本")

        # 时间段优化
        if self._has_time_pattern(predictions):
            opportunities.append("检测到明显的时间模式，可以使用定时扩缩容策略")

        return opportunities

    def _has_time_pattern(self, predictions: List[PredictionDataPoint]) -> bool:
        """检查是否有时间模式"""

        if len(predictions) < 24:
            return False

        # 简单检查：工作时间和非工作时间的差异
        work_hours = []
        off_hours = []

        for pred in predictions:
            hour = pred.timestamp.hour
            if 9 <= hour <= 17:
                work_hours.append(pred.predicted_value)
            else:
                off_hours.append(pred.predicted_value)

        if work_hours and off_hours:
            work_avg = np.mean(work_hours)
            off_avg = np.mean(off_hours)

            # 如果工作时间和非工作时间差异超过30%，认为有时间模式
            if abs(work_avg - off_avg) / max(work_avg, off_avg) > 0.3:
                return True

        return False

    def calculate_roi(
        self, investment: float, savings: float, time_period_hours: int
    ) -> float:
        """计算投资回报率"""

        if investment == 0:
            return 0.0

        # ROI = (收益 - 投资) / 投资 * 100
        total_savings = savings * time_period_hours
        roi = ((total_savings - investment) / investment) * 100

        return roi

    def recommend_instance_type(
        self, cpu_requirement: float, memory_requirement: float
    ) -> Dict[str, Any]:
        """推荐实例类型"""

        # 简单的实例选择逻辑
        if cpu_requirement <= 1 and memory_requirement <= 2:
            return {
                "type": "small",
                "cpu": 1,
                "memory": 2,
                "cost_per_hour": self.default_pricing["instance"]["small"],
            }
        elif cpu_requirement <= 2 and memory_requirement <= 4:
            return {
                "type": "medium",
                "cpu": 2,
                "memory": 4,
                "cost_per_hour": self.default_pricing["instance"]["medium"],
            }
        elif cpu_requirement <= 4 and memory_requirement <= 8:
            return {
                "type": "large",
                "cpu": 4,
                "memory": 8,
                "cost_per_hour": self.default_pricing["instance"]["large"],
            }
        else:
            return {
                "type": "xlarge",
                "cpu": 8,
                "memory": 16,
                "cost_per_hour": self.default_pricing["instance"]["xlarge"],
            }
