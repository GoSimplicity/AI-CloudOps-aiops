#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps智能报告生成器 - 基于大模型生成综合预测分析报告
"""

from dataclasses import dataclass
from datetime import datetime
import json
import logging
from typing import Any, Dict, List, Optional

from app.core.interfaces.llm_client import LLMClient, NullLLMClient
from app.core.prediction.prompt_templates import prompt_builder
from app.models import PredictionType

logger = logging.getLogger("aiops.core.prediction.report_generator")


@dataclass
class ReportContext:
    """报告生成上下文"""

    prediction_type: PredictionType
    analysis_context: Dict[str, Any]
    prediction_results: Dict[str, Any]
    interpretation: Dict[str, Any]
    insights: List[str]
    scaling_recommendations: List[Dict[str, Any]]
    cost_analysis: Optional[Dict[str, Any]]
    quantitative_metrics: Dict[str, Any]


class IntelligentReportGenerator:
    """报告生成器 - 结合外部分析生成多样化报告"""

    def __init__(self, llm_client: Optional[LLMClient] = None):
        # 默认使用空实现，服务层可注入真实实现
        self.llm_service: LLMClient = llm_client or NullLLMClient()

    def _safe_get_dict(
        self, data: Any, default: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """安全获取字典数据"""
        if isinstance(data, dict):
            return data
        return default if default is not None else {}

    def _safe_get_value(self, data: Any, key: str, default: Any = None) -> Any:
        """安全获取字典值"""
        if isinstance(data, dict):
            return data.get(key, default)
        return default

    async def generate_comprehensive_report(
        self,
        report_context: ReportContext,
        report_style: str = "professional",
        include_charts_desc: bool = True,
    ) -> Dict[str, Any]:
        """生成综合分析报告"""
        try:
            # 准备报告数据
            report_data = self._prepare_report_data(report_context)

            # 根据风格选择模板和参数
            template_params = self._get_template_params(report_style)

            # 构建报告生成提示
            prompt = await self._build_comprehensive_prompt(
                report_data, template_params, include_charts_desc
            )

            # 生成报告
            report_content = await self.llm_service.generate_response(
                messages=[{"role": "user", "content": prompt}],
                temperature=template_params.get("temperature", 0.3),
                max_tokens=template_params.get("max_tokens", 1500),
                use_task_model=False,  # 复杂操作：生成综合报告，使用主模型
            )

            if report_content:
                # 生成执行摘要
                executive_summary = await self._generate_executive_summary(
                    report_context, report_content
                )

                # 生成关键指标摘要
                key_metrics = self._extract_key_metrics(report_context)

                return {
                    "status": "success",
                    "report": {
                        "executive_summary": executive_summary,
                        "full_content": report_content,
                        "key_metrics": key_metrics,
                        "report_metadata": {
                            "generated_at": datetime.now(),
                            "prediction_type": report_context.prediction_type.value,
                            "report_style": report_style,
                            "data_quality_score": self._assess_data_quality(
                                report_context
                            ),
                        },
                    },
                }
            else:
                return self._generate_fallback_report(report_context)

        except Exception as e:
            logger.error(f"生成综合报告失败: {str(e)}")
            return self._generate_fallback_report(report_context)

    async def generate_executive_summary(
        self, report_context: ReportContext, max_length: int = 300
    ) -> Dict[str, Any]:
        """生成执行摘要 - 简短的高层决策概要"""
        try:
            summary_prompt = f"""基于以下{report_context.prediction_type.value}预测分析，生成一份简洁的执行摘要（{max_length}字以内）：

关键指标：
- 预测趋势: {self._get_trend_summary(report_context)}
- 风险等级: {self._assess_risk_level(report_context)}
- 资源建议: {len(report_context.scaling_recommendations)}项建议
- 成本影响: {self._get_cost_impact_summary(report_context)}

洞察要点：
{chr(10).join(report_context.insights[:3])}

请生成包含以下要素的执行摘要：
1. 一句话概述当前状况
2. 最关键的1-2个发现
3. 最重要的建议行动
4. 预期时间框架

要求：语言简洁、决策导向、突出行动要点。"""

            summary_response = await self.llm_service.generate_response(
                messages=[{"role": "user", "content": summary_prompt}],
                temperature=0.2,
                max_tokens=400,
                use_task_model=False,  # 复杂操作：生成执行摘要，使用主模型
            )

            if summary_response:
                return {
                    "status": "success",
                    "summary": summary_response,
                    "word_count": len(summary_response),
                    "generated_at": datetime.now(),
                }
            else:
                return self._generate_fallback_summary(report_context)

        except Exception as e:
            logger.error(f"生成执行摘要失败: {str(e)}")
            return self._generate_fallback_summary(report_context)

    async def generate_action_plan(
        self, report_context: ReportContext, time_horizon: str = "weekly"
    ) -> Dict[str, Any]:
        """生成行动计划"""
        try:
            time_configs = {
                "daily": {"steps": 3, "detail_level": "具体操作"},
                "weekly": {"steps": 5, "detail_level": "阶段性任务"},
                "monthly": {"steps": 4, "detail_level": "战略性目标"},
            }

            config = time_configs.get(time_horizon, time_configs["weekly"])

            action_prompt = f"""基于{report_context.prediction_type.value}预测分析，制定{time_horizon}行动计划：

预测概况：
{self._format_prediction_overview(report_context)}

扩缩容建议：
{self._format_scaling_recommendations(report_context)}

请生成{config["steps"]}步的行动计划，每步包含：
- 行动项目（{config["detail_level"]}）
- 执行时间
- 负责团队建议
- 预期结果
- 风险控制

要求：可执行、有时间节点、考虑依赖关系。"""

            plan_response = await self.llm_service.generate_response(
                messages=[{"role": "user", "content": action_prompt}],
                temperature=0.3,
                max_tokens=800,
                use_task_model=False,  # 复杂操作：生成行动计划，使用主模型
            )

            if plan_response:
                parsed_plan = self._parse_action_plan(plan_response)

                return {
                    "status": "success",
                    "action_plan": {
                        "time_horizon": time_horizon,
                        "plan_content": plan_response,
                        "parsed_actions": parsed_plan,
                        "priority_actions": self._identify_priority_actions(
                            parsed_plan
                        ),
                        "generated_at": datetime.now(),
                    },
                }
            else:
                return self._generate_fallback_action_plan(report_context, time_horizon)

        except Exception as e:
            logger.error(f"生成行动计划失败: {str(e)}")
            return self._generate_fallback_action_plan(report_context, time_horizon)

    async def generate_risk_assessment_report(
        self, report_context: ReportContext
    ) -> Dict[str, Any]:
        """生成风险评估报告"""
        try:
            risk_data = self._analyze_risks(report_context)

            risk_prompt = f"""基于{report_context.prediction_type.value}预测，进行风险评估分析：

检测到的风险点：
{json.dumps(risk_data, ensure_ascii=False, indent=2)}

请从以下维度进行风险评估：

## 🚨 风险识别
识别主要风险类型和触发条件

## 📊 影响分析  
评估风险对业务和系统的潜在影响

## ⚡ 紧急程度
根据时间紧迫性对风险进行优先级排序

## 🛡️ 缓解策略
针对每类风险提出具体的应对措施

## 📈 监控建议
建议需要加强监控的关键指标

要求：客观分析、量化影响、可操作建议。"""

            risk_response = await self.llm_service.generate_response(
                messages=[{"role": "user", "content": risk_prompt}],
                temperature=0.2,
                max_tokens=1000,
                use_task_model=False,  # 复杂操作：风险评估，使用主模型
            )

            if risk_response:
                return {
                    "status": "success",
                    "risk_assessment": {
                        "assessment_content": risk_response,
                        "risk_score": self._calculate_risk_score(report_context),
                        "critical_risks": risk_data.get("critical_risks", []),
                        "risk_timeline": self._generate_risk_timeline(report_context),
                        "generated_at": datetime.now(),
                    },
                }
            else:
                return self._generate_fallback_risk_assessment(report_context)

        except Exception as e:
            logger.error(f"生成风险评估报告失败: {str(e)}")
            return self._generate_fallback_risk_assessment(report_context)

    async def generate_cost_optimization_report(
        self, report_context: ReportContext
    ) -> Optional[Dict[str, Any]]:
        """生成成本优化报告"""
        if not report_context.cost_analysis:
            return None

        try:
            cost_data = self._safe_get_dict(report_context.cost_analysis)

            cost_prompt = f"""基于{report_context.prediction_type.value}预测和成本分析，生成成本优化报告：

成本分析数据：
{json.dumps(cost_data, ensure_ascii=False, indent=2)}

扩缩容建议：
{self._format_scaling_recommendations(report_context)}

请生成成本优化分析：

## 💰 成本现状
当前成本结构和主要开销项目

## 📈 成本预测
基于预测结果的成本变化趋势

## 💡 优化机会
识别的成本节省机会和优化点

## 🎯 优化建议
具体的成本优化措施和预期效果

## ⚖️ 风险权衡
成本优化可能带来的性能或可靠性风险

要求：量化分析、ROI评估、平衡建议。"""

            cost_response = await self.llm_service.generate_response(
                messages=[{"role": "user", "content": cost_prompt}],
                temperature=0.3,
                max_tokens=900,
                use_task_model=False,  # 复杂操作：成本优化报告，使用主模型
            )

            if cost_response:
                return {
                    "status": "success",
                    "cost_optimization": {
                        "report_content": cost_response,
                        "potential_savings": cost_data.get("cost_savings_potential", 0),
                        "optimization_priority": self._assess_cost_optimization_priority(
                            cost_data
                        ),
                        "generated_at": datetime.now(),
                    },
                }
            else:
                return {"status": "fallback", "message": "成本优化报告生成失败"}

        except Exception as e:
            logger.error(f"生成成本优化报告失败: {str(e)}")
            return {"status": "error", "message": str(e)}

    def _prepare_report_data(self, context: ReportContext) -> Dict[str, Any]:
        """准备报告数据"""
        try:
            # 安全获取各种数据，确保都是字典类型
            analysis_context = self._safe_get_dict(context.analysis_context)
            prediction_results = self._safe_get_dict(context.prediction_results)
            interpretation = self._safe_get_dict(context.interpretation)
            quantitative_metrics = self._safe_get_dict(context.quantitative_metrics)

            # 提取分析摘要
            if isinstance(context.analysis_context, str):
                analysis_summary = context.analysis_context
            else:
                analysis_dict = self._safe_get_dict(
                    self._safe_get_value(analysis_context, "analysis", {})
                )
                analysis_summary = self._safe_get_value(analysis_dict, "summary", "")

            # 提取解释摘要
            if isinstance(context.interpretation, str):
                interpretation_summary = context.interpretation
            else:
                interpretation_summary = self._safe_get_value(
                    interpretation, "interpretation_summary", ""
                )

            return {
                "prediction_type": context.prediction_type.value,
                "analysis_summary": analysis_summary,
                "interpretation_summary": interpretation_summary,
                "key_insights": context.insights,
                "quantitative_metrics": quantitative_metrics,
                "scaling_count": len(
                    context.scaling_recommendations
                    if context.scaling_recommendations
                    else []
                ),
                "anomaly_count": len(
                    self._safe_get_value(prediction_results, "anomaly_predictions", [])
                ),
                "prediction_summary": self._safe_get_value(
                    prediction_results, "prediction_summary", {}
                ),
            }
        except Exception as e:
            logger.error(f"准备报告数据失败: {str(e)}")
            # 返回基本的安全数据
            return {
                "prediction_type": context.prediction_type.value,
                "analysis_summary": "数据准备失败",
                "interpretation_summary": "解释数据不可用",
                "key_insights": context.insights or [],
                "quantitative_metrics": {},
                "scaling_count": 0,
                "anomaly_count": 0,
                "prediction_summary": {},
            }

    def _get_template_params(self, style: str) -> Dict[str, Any]:
        """获取模板参数"""
        style_configs = {
            "professional": {"temperature": 0.2, "max_tokens": 1500, "tone": "formal"},
            "executive": {"temperature": 0.1, "max_tokens": 1200, "tone": "business"},
            "technical": {"temperature": 0.3, "max_tokens": 1800, "tone": "detailed"},
            "concise": {"temperature": 0.2, "max_tokens": 800, "tone": "brief"},
        }
        return style_configs.get(style, style_configs["professional"])

    async def _build_comprehensive_prompt(
        self,
        report_data: Dict[str, Any],
        template_params: Dict[str, Any],
        include_charts_desc: bool,
    ) -> str:
        """构建综合报告提示"""
        base_prompt = prompt_builder.build_comprehensive_report_prompt(
            prediction_type=PredictionType(report_data["prediction_type"]),
            analysis_context=report_data["analysis_summary"],
            prediction_results=report_data["prediction_summary"],
            scaling_recommendations=[f"建议{report_data['scaling_count']}项扩缩容操作"],
            cost_analysis="成本分析数据",
            insights=report_data["key_insights"],
        )

        if include_charts_desc:
            charts_addition = """

## 📊 数据可视化建议
建议生成的图表和可视化内容：
- 预测趋势图
- 置信区间图  
- 异常点标注
- 资源利用率对比"""
            base_prompt += charts_addition

        return base_prompt

    async def _generate_executive_summary(
        self, context: ReportContext, full_report: str
    ) -> str:
        """生成执行摘要"""
        try:
            summary_prompt = f"""基于以下完整的{context.prediction_type.value}分析报告，提取生成200字以内的执行摘要：

完整报告：
{full_report[:800]}...

要求：
1. 突出最关键的发现
2. 明确建议的行动
3. 说明时间紧迫性
4. 语言简洁有力

请生成执行摘要："""

            summary = await self.llm_service.generate_response(
                messages=[{"role": "user", "content": summary_prompt}],
                temperature=0.1,
                max_tokens=300,
                use_task_model=False,  # 复杂操作：生成执行摘要，使用主模型
            )

            return summary if summary else self._get_fallback_executive_summary(context)

        except Exception as e:
            logger.error(f"生成执行摘要失败: {str(e)}")
            return self._get_fallback_executive_summary(context)

    def _extract_key_metrics(self, context: ReportContext) -> Dict[str, Any]:
        """提取关键指标"""
        prediction_results = self._safe_get_dict(context.prediction_results)
        prediction_summary = self._safe_get_dict(
            self._safe_get_value(prediction_results, "prediction_summary", {})
        )

        prediction_accuracy = prediction_results.get("model_accuracy")

        # 安全获取预测摘要信息
        trend_direction = self._safe_get_value(prediction_summary, "trend", "unknown")
        peak_value = self._safe_get_value(prediction_summary, "max_value", 0)

        return {
            "prediction_accuracy": prediction_accuracy,
            "trend_direction": trend_direction,
            "peak_value": peak_value,
            "risk_level": self._assess_risk_level(context),
            "optimization_potential": self._assess_optimization_potential(context),
            "time_to_action": self._assess_time_to_action(context),
        }

    def _get_trend_summary(self, context: ReportContext) -> str:
        """获取趋势摘要"""
        prediction_results = self._safe_get_dict(context.prediction_results)
        prediction_summary = self._safe_get_dict(
            self._safe_get_value(prediction_results, "prediction_summary", {})
        )
        trend = self._safe_get_value(prediction_summary, "trend", "unknown")
        trend_map = {"increasing": "上升", "decreasing": "下降", "stable": "稳定"}
        return trend_map.get(trend, "未知")

    def _assess_risk_level(self, context: ReportContext) -> str:
        """评估风险等级"""
        prediction_results = self._safe_get_dict(context.prediction_results)
        anomalies = self._safe_get_value(prediction_results, "anomaly_predictions", [])
        high_risk_anomalies = [
            a
            for a in anomalies
            if isinstance(a, dict) and a.get("impact_level") in ["high", "critical"]
        ]

        if len(high_risk_anomalies) > 2:
            return "高风险"
        elif len(high_risk_anomalies) > 0:
            return "中风险"
        else:
            return "低风险"

    def _get_cost_impact_summary(self, context: ReportContext) -> str:
        """获取成本影响摘要"""
        if not context.cost_analysis:
            return "未分析"

        cost_analysis = self._safe_get_dict(context.cost_analysis)
        savings_potential = self._safe_get_value(
            cost_analysis, "cost_savings_potential", 0
        )

        if savings_potential > 20:
            return "高节省潜力"
        elif savings_potential > 5:
            return "中等节省潜力"
        else:
            return "低节省潜力"

    def _assess_data_quality(self, context: ReportContext) -> Optional[float]:
        """缺少可验证的质量评估依据时不生成分数。"""
        return None

    def _format_prediction_overview(self, context: ReportContext) -> str:
        """格式化预测概览"""
        try:
            prediction_results = self._safe_get_dict(context.prediction_results)
            prediction_summary = self._safe_get_dict(
                self._safe_get_value(prediction_results, "prediction_summary", {})
            )
            predicted_data = self._safe_get_value(
                prediction_results, "predicted_data", []
            )

            if not predicted_data:
                return "暂无预测数据"

            overview = f"""预测类型: {context.prediction_type.value}
当前值: {self._safe_get_value(prediction_results, "current_value", "N/A")}
预测时长: {self._safe_get_value(prediction_results, "prediction_hours", "N/A")}小时
预测点数: {len(predicted_data)}个
最大值: {self._safe_get_value(prediction_summary, "max_value", "N/A")}
最小值: {self._safe_get_value(prediction_summary, "min_value", "N/A")}
平均值: {self._safe_get_value(prediction_summary, "avg_value", "N/A")}
趋势: {self._safe_get_value(prediction_summary, "trend", "unknown")}"""

            return overview

        except Exception as e:
            logger.error(f"格式化预测概览失败: {str(e)}")
            return f"{context.prediction_type.value}预测分析"

    def _format_scaling_recommendations(self, context: ReportContext) -> str:
        """格式化扩缩容建议"""
        try:
            recommendations = context.scaling_recommendations

            if not recommendations:
                return "暂无扩缩容建议"

            formatted_recs = []
            for i, rec in enumerate(recommendations[:5], 1):
                action = rec.get("action", "unknown")
                trigger_time = rec.get("trigger_time", "N/A")
                confidence = rec.get("confidence")
                reason = rec.get("reason", "N/A")

                confidence_text = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "未知"
                rec_text = f"{i}. 动作: {action}, 时间: {trigger_time}, 置信度: {confidence_text}, 原因: {reason}"
                formatted_recs.append(rec_text)

            return "\n".join(formatted_recs)

        except Exception as e:
            logger.error(f"格式化扩缩容建议失败: {str(e)}")
            return "扩缩容建议格式化失败"

    def _parse_action_plan(self, plan_text: str) -> List[Dict[str, Any]]:
        """解析行动计划文本为结构化数据"""
        try:
            actions = []
            lines = plan_text.split("\n")
            current_action = {}

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                if line.startswith(("1.", "2.", "3.", "4.", "5.", "##", "**")):
                    if current_action:
                        actions.append(current_action)
                    current_action = {
                        "title": line,
                        "details": [],
                        "priority": "medium",
                        "estimated_time": "未指定",
                    }
                elif current_action and line.startswith(("-", "•", "*")):
                    current_action["details"].append(line[1:].strip())
                elif current_action:
                    current_action["details"].append(line)

            if current_action:
                actions.append(current_action)

            return actions[:5]

        except Exception as e:
            logger.error(f"解析行动计划失败: {str(e)}")
            return [
                {
                    "title": "行动计划",
                    "details": ["请查看完整的行动计划内容"],
                    "priority": "medium",
                    "estimated_time": "待评估",
                }
            ]

    def _identify_priority_actions(
        self, parsed_actions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """识别优先级行动项"""
        try:
            priority_actions = []
            high_priority_keywords = [
                "紧急",
                "立即",
                "critical",
                "urgent",
                "即时",
                "高风险",
            ]

            for action in parsed_actions:
                title = action.get("title", "").lower()
                details = " ".join(action.get("details", [])).lower()

                if any(
                    keyword in title or keyword in details
                    for keyword in high_priority_keywords
                ):
                    action["priority"] = "high"
                    priority_actions.append(action)
                elif len(priority_actions) < 2:
                    action["priority"] = "medium"
                    priority_actions.append(action)

            return priority_actions[:3]

        except Exception as e:
            logger.error(f"识别优先级行动失败: {str(e)}")
            return parsed_actions[:2]

    def _analyze_risks(self, context: ReportContext) -> Dict[str, Any]:
        """分析风险"""
        try:
            prediction_results = self._safe_get_dict(context.prediction_results)
            anomalies = self._safe_get_value(
                prediction_results, "anomaly_predictions", []
            )

            critical_risks = []
            for anomaly in anomalies:
                if isinstance(anomaly, dict) and anomaly.get("impact_level") in [
                    "high",
                    "critical",
                ]:
                    critical_risks.append(
                        {
                            "type": anomaly.get("anomaly_type", "unknown"),
                            "score": anomaly.get("anomaly_score", 0),
                            "timestamp": anomaly.get("timestamp", ""),
                        }
                    )

            return {
                "critical_risks": critical_risks,
                "total_anomalies": len(anomalies),
                "risk_trend": "increasing" if len(critical_risks) > 2 else "stable",
            }
        except Exception:
            return {"critical_risks": [], "total_anomalies": 0, "risk_trend": "unknown"}

    def _calculate_risk_score(self, context: ReportContext) -> float:
        """计算风险分数"""
        try:
            risk_data = self._analyze_risks(context)
            critical_count = len(risk_data.get("critical_risks", []))
            total_anomalies = risk_data.get("total_anomalies", 0)

            if total_anomalies == 0:
                return 0.0

            risk_ratio = critical_count / total_anomalies
            return min(1.0, risk_ratio * 2)
        except Exception:
            return 0.5

    def _generate_risk_timeline(self, context: ReportContext) -> List[Dict[str, Any]]:
        """生成风险时间线"""
        try:
            risk_data = self._analyze_risks(context)
            critical_risks = risk_data.get("critical_risks", [])

            timeline = []
            for risk in critical_risks:
                timeline.append(
                    {
                        "time": risk.get("timestamp", ""),
                        "risk_type": risk.get("type", "unknown"),
                        "severity": "high" if risk.get("score", 0) > 0.7 else "medium",
                    }
                )

            return sorted(timeline, key=lambda x: x.get("time", ""))
        except Exception:
            return []

    def _assess_optimization_potential(self, context: ReportContext) -> str:
        """评估优化潜力"""
        try:
            scaling_recommendations = context.scaling_recommendations or []

            if len(scaling_recommendations) > 3:
                return "高优化潜力"
            elif len(scaling_recommendations) > 1:
                return "中等优化潜力"
            else:
                return "低优化潜力"
        except Exception:
            return "未知"

    def _assess_time_to_action(self, context: ReportContext) -> str:
        """评估行动时间紧迫性"""
        try:
            risk_level = self._assess_risk_level(context)
            if risk_level == "高风险":
                return "立即行动"
            elif risk_level == "中风险":
                return "24小时内"
            else:
                return "一周内"
        except Exception:
            return "待评估"

    def _assess_cost_optimization_priority(self, cost_data: Dict[str, Any]) -> str:
        """评估成本优化优先级"""
        try:
            savings_potential = self._safe_get_value(
                cost_data, "cost_savings_potential", 0
            )
            if savings_potential > 30:
                return "高优先级"
            elif savings_potential > 10:
                return "中优先级"
            else:
                return "低优先级"
        except Exception:
            return "待评估"

    # 降级和备用方案
    def _generate_fallback_report(self, context: ReportContext) -> Dict[str, Any]:
        """生成降级报告"""
        fallback_content = f"""# {context.prediction_type.value.upper()}预测分析报告

## 预测概览
- 预测类型: {context.prediction_type.value}
- 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}
- 分析状态: 基础模式

## 关键发现
{chr(10).join([f"- {insight}" for insight in context.insights[:5]])}

## 建议行动
- 监控预测趋势变化
- 关注异常指标变化
- 根据业务需求调整资源配置

*注：此报告为降级模式生成，建议结合专业分析。*"""

        return {
            "status": "fallback",
            "report": {
                "executive_summary": f"{context.prediction_type.value}预测分析完成，请查看详细内容。",
                "full_content": fallback_content,
                "key_metrics": self._extract_key_metrics(context),
                "report_metadata": {
                    "generated_at": datetime.now(),
                    "prediction_type": context.prediction_type.value,
                    "report_style": "fallback",
                    "data_quality_score": None,
                },
            },
        }

    def _generate_fallback_summary(self, context: ReportContext) -> Dict[str, Any]:
        """生成降级执行摘要"""
        return {
            "status": "fallback",
            "summary": f"{context.prediction_type.value}预测分析已完成。建议关注预测趋势和扩缩容建议，及时采取必要的资源调整措施。",
            "word_count": 50,
            "generated_at": datetime.now(),
        }

    def _get_fallback_executive_summary(self, context: ReportContext) -> str:
        """获取降级执行摘要"""
        return f"{context.prediction_type.value}预测显示{self._get_trend_summary(context)}趋势，风险等级{self._assess_risk_level(context)}。建议关注{len(context.scaling_recommendations)}项扩缩容建议并及时执行。"

    def _generate_fallback_action_plan(
        self, context: ReportContext, time_horizon: str
    ) -> Dict[str, Any]:
        """生成降级行动计划"""
        fallback_plan = f"""基于{context.prediction_type.value}预测结果的{time_horizon}行动计划：

1. 监控关键指标变化
   - 持续观察{context.prediction_type.value}指标趋势
   - 设置告警阈值

2. 准备扩缩容资源
   - 评估当前资源配置
   - 准备扩容方案

3. 风险评估和预案
   - 识别潜在风险点
   - 制定应急预案

*注：此为降级模式生成的基础行动计划。*"""

        return {
            "status": "fallback",
            "action_plan": {
                "time_horizon": time_horizon,
                "plan_content": fallback_plan,
                "parsed_actions": [
                    {
                        "title": "基础监控和准备",
                        "details": ["监控指标", "准备资源", "评估风险"],
                        "priority": "medium",
                        "estimated_time": time_horizon,
                    }
                ],
                "priority_actions": [],
                "generated_at": datetime.now(),
            },
        }

    def _generate_fallback_risk_assessment(
        self, context: ReportContext
    ) -> Dict[str, Any]:
        """生成降级风险评估"""
        return {
            "status": "fallback",
            "risk_assessment": {
                "assessment_content": f"{context.prediction_type.value}风险评估暂时不可用，建议人工评估。",
                "risk_score": 0.5,
                "critical_risks": [],
                "risk_timeline": [],
                "generated_at": datetime.now(),
            },
        }
