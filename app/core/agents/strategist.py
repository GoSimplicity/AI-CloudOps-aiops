#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
K8s修复策略Agent - 专门负责制定修复策略
Author: AI Assistant
License: Apache 2.0
Description: 基于检测结果制定最优修复策略的Agent
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
from app.services.llm import LLMService

logger = logging.getLogger("aiops.strategist")


class K8sStrategistAgent:
    """Kubernetes修复策略Agent"""

    def __init__(self):
        self.llm_service = LLMService()
        self.strategy_templates = self._load_strategy_templates()

    def _load_strategy_templates(self) -> Dict[str, Any]:
        """加载策略模板"""
        return {
            "crash_loop_fix": {
                "description": "修复CrashLoopBackOff问题",
                "steps": [
                    "检查容器日志",
                    "分析探针配置",
                    "调整健康检查参数",
                    "重启部署验证",
                ],
                "priority": "high",
                "rollback_plan": "恢复探针配置到之前状态",
            },
            "resource_adjustment": {
                "description": "调整资源配置",
                "steps": [
                    "分析当前资源使用情况",
                    "计算合理资源需求",
                    "修改资源请求和限制",
                    "监控调整效果",
                ],
                "priority": "medium",
                "rollback_plan": "恢复原始资源配置",
            },
            "probe_fix": {
                "description": "修复健康检查配置",
                "steps": [
                    "检查当前探针配置",
                    "分析失败原因",
                    "调整探针参数",
                    "验证探针有效性",
                ],
                "priority": "medium",
                "rollback_plan": "恢复原始探针配置",
            },
            "scaling_fix": {
                "description": "调整副本数量",
                "steps": [
                    "分析当前负载",
                    "计算合理副本数",
                    "调整副本配置",
                    "监控扩展效果",
                ],
                "priority": "low",
                "rollback_plan": "恢复原始副本数",
            },
        }

    async def analyze_issues(self, issues: Dict[str, Any]) -> Dict[str, Any]:
        """分析问题并制定修复策略"""
        try:
            if "error" in issues:
                return {"error": issues["error"]}

            strategy = {
                "timestamp": datetime.now().isoformat(),
                "summary": {
                    "total_issues": issues["summary"]["total_issues"],
                    "fixable_issues": 0,
                    "risk_level": "low",
                },
                "strategies": [],
                "execution_order": [],
                "warnings": [],
            }

            # 分析每个问题
            for issue in issues["details"]:
                issue_strategy = await self._create_issue_strategy(issue)
                if issue_strategy:
                    strategy["strategies"].append(issue_strategy)
                    if issue["auto_fix"]:
                        strategy["summary"]["fixable_issues"] += 1

            # 确定执行顺序
            strategy["execution_order"] = self._determine_execution_order(
                strategy["strategies"]
            )

            # 评估风险等级
            strategy["summary"]["risk_level"] = self._assess_risk_level(
                strategy["strategies"]
            )

            # 生成警告信息
            strategy["warnings"] = self._generate_warnings(strategy["strategies"])

            logger.info(
                f"制定策略完成: {len(strategy['strategies'])} 个策略, 风险等级: {strategy['summary']['risk_level']}"
            )
            return strategy

        except Exception as e:
            logger.error(f"制定策略失败: {str(e)}")
            return {"error": str(e)}

    async def _create_issue_strategy(
        self, issue: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """为单个问题创建策略"""
        try:
            strategy_type = self._determine_strategy_type(issue)
            if not strategy_type:
                return None

            template = self.strategy_templates.get(strategy_type)
            if not template:
                return None

            # 基于问题详情定制策略
            customized_strategy = {
                "id": f"{issue['resource_name']}_{issue['sub_type']}",
                "type": strategy_type,
                "target": {
                    "resource_type": issue["type"],
                    "name": issue["resource_name"],
                    "namespace": issue["namespace"],
                },
                "description": template["description"],
                "steps": self._customize_steps(template["steps"], issue),
                "priority": template["priority"],
                "severity": issue["severity"],
                "auto_fix": issue["auto_fix"],
                "estimated_time": self._estimate_time(strategy_type, issue),
                "rollback_plan": template["rollback_plan"],
                "pre_conditions": self._get_pre_conditions(strategy_type, issue),
                "post_conditions": self._get_post_conditions(strategy_type, issue),
                "monitoring_points": self._get_monitoring_points(strategy_type, issue),
            }

            return customized_strategy

        except Exception as e:
            logger.error(f"创建问题策略失败: {str(e)}")
            return None

    def _determine_strategy_type(self, issue: Dict[str, Any]) -> Optional[str]:
        """确定策略类型"""
        issue_type = issue["type"]
        sub_type = issue["sub_type"]

        if issue_type == "pod_issue":
            if sub_type == "crash_loop":
                return "crash_loop_fix"
            elif sub_type == "resource_pressure":
                return "resource_adjustment"
            elif sub_type == "pending_timeout":
                return "resource_adjustment"
        elif issue_type == "deployment_issue":
            if sub_type == "replica_mismatch":
                return "scaling_fix"
            elif sub_type == "unavailable_replicas":
                return "probe_fix"
        elif issue_type == "service_issue":
            return "probe_fix"

        return None

    def _customize_steps(self, steps: List[str], issue: Dict[str, Any]) -> List[str]:
        """根据问题定制步骤"""
        customized = steps.copy()

        # 根据问题类型添加具体信息
        if issue["sub_type"] == "crash_loop":
            customized[1] = f"检查探针配置 - 资源: {issue['resource_name']}"
        elif issue["sub_type"] == "resource_pressure":
            customized[1] = (
                f"分析资源需求 - 当前: {issue.get('details', {}).get('current', 'N/A')}"
            )

        return customized

    def _estimate_time(self, strategy_type: str, issue: Dict[str, Any]) -> int:
        """估算执行时间（分钟）"""
        estimates = {
            "crash_loop_fix": 5,
            "resource_adjustment": 3,
            "probe_fix": 3,
            "scaling_fix": 2,
        }

        base_time = estimates.get(strategy_type, 5)

        # 根据严重程度调整
        if issue["severity"] == "critical":
            base_time += 2
        elif issue["severity"] == "high":
            base_time += 1

        return base_time

    def _get_pre_conditions(
        self, strategy_type: str, issue: Dict[str, Any]
    ) -> List[str]:
        """获取前置条件"""
        pre_conditions = {
            "crash_loop_fix": [
                "集群连接正常",
                "有足够的权限修改资源",
                "部署处于可修改状态",
            ],
            "resource_adjustment": [
                "集群资源充足",
                "节点有可用资源",
                "应用支持资源限制",
            ],
            "probe_fix": ["应用有健康检查端点", "网络连通性正常", "服务已正确暴露"],
            "scaling_fix": ["集群资源充足", "应用支持水平扩展", "负载均衡配置正确"],
        }

        return pre_conditions.get(strategy_type, ["集群连接正常"])

    def _get_post_conditions(
        self, strategy_type: str, issue: Dict[str, Any]
    ) -> List[str]:
        """获取后置条件"""
        post_conditions = {
            "crash_loop_fix": [
                "Pod状态变为Running",
                "重启次数不再增加",
                "应用响应正常",
            ],
            "resource_adjustment": [
                "Pod成功调度",
                "资源使用在限制范围内",
                "应用性能稳定",
            ],
            "probe_fix": ["健康检查通过", "服务状态正常", "流量路由正确"],
            "scaling_fix": ["副本数达到目标", "负载均衡生效", "性能指标改善"],
        }

        return post_conditions.get(strategy_type, ["问题已解决"])

    def _get_monitoring_points(
        self, strategy_type: str, issue: Dict[str, Any]
    ) -> List[str]:
        """获取监控点"""
        monitoring_points = {
            "crash_loop_fix": ["Pod重启次数", "应用日志", "服务响应时间", "内存使用量"],
            "resource_adjustment": [
                "CPU使用率",
                "内存使用率",
                "Pod调度状态",
                "节点资源使用",
            ],
            "probe_fix": ["健康检查状态", "服务可用性", "错误率", "响应时间"],
            "scaling_fix": ["副本数", "负载分布", "响应时间", "错误率"],
        }

        return monitoring_points.get(strategy_type, ["Pod状态"])

    def _determine_execution_order(self, strategies: List[Dict[str, Any]]) -> List[str]:
        """确定执行顺序"""
        # 按优先级排序
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}

        sorted_strategies = sorted(
            strategies,
            key=lambda x: (
                priority_order.get(x["severity"], 4),
                x["estimated_time"],
                x["target"]["resource_type"],
            ),
        )

        return [s["id"] for s in sorted_strategies]

    def _assess_risk_level(self, strategies: List[Dict[str, Any]]) -> str:
        """评估风险等级"""
        critical_count = sum(1 for s in strategies if s["severity"] == "critical")
        high_count = sum(1 for s in strategies if s["severity"] == "high")

        if critical_count > 0:
            return "high"
        elif high_count > 2:
            return "medium"
        elif high_count > 0:
            return "low"
        else:
            return "minimal"

    def _generate_warnings(self, strategies: List[Dict[str, Any]]) -> List[str]:
        """生成警告信息"""
        warnings = []

        # 检查是否有不可自动修复的问题
        non_fixable = [s for s in strategies if not s["auto_fix"]]
        if non_fixable:
            warnings.append(f"有 {len(non_fixable)} 个问题需要手动处理")

        # 检查高风险操作
        critical_strategies = [s for s in strategies if s["severity"] == "critical"]
        if critical_strategies:
            warnings.append(f"有 {len(critical_strategies)} 个高风险操作，请谨慎执行")

        # 检查资源影响
        resource_strategies = [
            s for s in strategies if s["type"] == "resource_adjustment"
        ]
        if resource_strategies:
            warnings.append("资源调整可能影响应用性能，请监控调整效果")

        return warnings

    async def validate_strategy(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        """验证策略可行性"""
        try:
            validation = {
                "valid": True,
                "warnings": [],
                "errors": [],
                "suggestions": [],
            }

            # 检查前置条件
            for condition in strategy.get("pre_conditions", []):
                # 这里可以添加实际的检查逻辑
                if "权限" in condition:
                    validation["warnings"].append("请确认有足够的集群权限")
                elif "资源" in condition:
                    validation["warnings"].append("请确认集群资源充足")

            # 检查策略合理性
            if strategy["type"] == "resource_adjustment":
                # 检查资源调整幅度
                if "128Mi" in str(strategy):
                    validation["suggestions"].append("考虑逐步调整资源，避免大幅度变化")

            # 检查执行时间
            if strategy["estimated_time"] > 10:
                validation["warnings"].append("策略执行时间较长，建议在低峰期执行")

            return validation

        except Exception as e:
            logger.error(f"验证策略失败: {str(e)}")
            return {"valid": False, "errors": [str(e)]}

    def get_strategy_summary(self, strategy: Dict[str, Any]) -> str:
        """获取策略摘要"""
        return f"""
策略摘要:
- 目标: {strategy["target"]["resource_type"]} {strategy["target"]["name"]}
- 类型: {strategy["type"]}
- 严重程度: {strategy["severity"]}
- 预计时间: {strategy["estimated_time"]}分钟
- 步骤: {", ".join(strategy["steps"])}
"""
