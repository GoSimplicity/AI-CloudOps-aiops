#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps智能预测分析器 - 使用大模型解读和分析预测结果
"""

from datetime import datetime
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.core.interfaces.llm_client import LLMClient, NullLLMClient
from app.core.prediction.prompt_templates import prompt_builder
from app.models import PredictionType

logger = logging.getLogger("aiops.core.prediction.analyzer")


class PredictionAnalyzer:
    """预测分析器 - 结合外部分析的结果解读"""

    def __init__(self, llm_client: Optional[LLMClient] = None):
        # 默认使用空实现，服务层可注入真实实现
        self.llm_service: LLMClient = llm_client or NullLLMClient()

    async def analyze_historical_context(
        self,
        prediction_type: PredictionType,
        current_value: float,
        historical_data: List[Dict[str, Any]],
        additional_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """分析历史数据上下文"""
        try:
            # 构建分析提示
            prompt = prompt_builder.build_analysis_prompt(
                prediction_type=prediction_type,
                current_value=current_value,
                historical_data=historical_data,
                additional_context=additional_context,
            )

            # 调用分析接口
            analysis_result = await self.llm_service.generate_response(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=800,
                use_task_model=False,  # 复杂操作：数据分析，使用主模型
            )

            if analysis_result:
                # 解析分析结果
                parsed_analysis = self._parse_context_analysis(analysis_result)

                return {
                    "status": "success",
                    "analysis": parsed_analysis,
                    "raw_response": analysis_result,
                    "timestamp": datetime.now(),
                }
            else:
                return self._get_fallback_context_analysis(prediction_type)

        except Exception as e:
            logger.error("历史数据上下文分析失败，异常类型: %s", type(e).__name__)
            return self._get_fallback_context_analysis(prediction_type)

    async def interpret_prediction_results(
        self,
        prediction_type: PredictionType,
        prediction_results: Dict[str, Any],
        analysis_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """解读预测结果"""
        try:
            # 构建解读提示
            prompt = prompt_builder.build_interpretation_prompt(
                prediction_type=prediction_type,
                prediction_results=prediction_results,
                additional_analysis=analysis_context,
            )

            # 调用解读接口
            interpretation = await self.llm_service.generate_response(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=1000,
                use_task_model=False,  # 复杂操作：趋势解读，使用主模型
            )

            if interpretation:
                # 解析解读结果
                parsed_interpretation = self._parse_interpretation_result(
                    interpretation
                )

                # 计算量化指标
                quantitative_metrics = self._calculate_quantitative_metrics(
                    prediction_results
                )

                return {
                    "status": "success",
                    "interpretation": parsed_interpretation,
                    "quantitative_metrics": quantitative_metrics,
                    "raw_response": interpretation,
                    "timestamp": datetime.now(),
                }
            else:
                return self._get_fallback_interpretation(prediction_type)

        except Exception as e:
            logger.error("预测结果解读失败，异常类型: %s", type(e).__name__)
            return self._get_fallback_interpretation(prediction_type)

    async def generate_insights(
        self,
        prediction_type: PredictionType,
        prediction_results: Dict[str, Any],
        context_analysis: Dict[str, Any],
        interpretation: Dict[str, Any],
    ) -> List[str]:
        """生成洞察"""
        try:
            # 综合所有分析信息
            combined_context = {
                "prediction_type": prediction_type.value,
                "prediction_summary": prediction_results.get("prediction_summary", {}),
                "context_insights": context_analysis.get("analysis", {}),
                "interpretation_insights": interpretation.get("interpretation", {}),
                "anomalies": prediction_results.get("anomaly_predictions", []),
                "trends": prediction_results.get("trend_insights", []),
            }

            # 构建洞察生成提示
            insights_prompt = f"""基于以下综合分析信息，为{prediction_type.value}预测生成5-7个关键洞察：

分析信息：
{json.dumps(combined_context, ensure_ascii=False, indent=2)}

请生成具体的、可操作的洞察，每个洞察应该：
1. 基于数据分析得出
2. 对运维决策有实际价值
3. 语言简洁专业
4. 突出关键风险或机会

请以清单形式返回洞察，每个洞察一行。"""

            insights_response = await self.llm_service.generate_response(
                messages=[{"role": "user", "content": insights_prompt}],
                temperature=0.4,
                max_tokens=600,
                use_task_model=False,  # 复杂操作：生成洞察，使用主模型
            )

            if insights_response:
                # 解析洞察列表
                insights = self._parse_insights_response(insights_response)
                return insights
            else:
                return self._get_fallback_insights(prediction_type, prediction_results)

        except Exception as e:
            logger.error("生成智能洞察失败，异常类型: %s", type(e).__name__)
            return self._get_fallback_insights(prediction_type, prediction_results)

    async def analyze_multi_dimension_correlation(
        self, prediction_results: Dict[PredictionType, Dict[str, Any]]
    ) -> Dict[str, Any]:
        """分析多维度预测结果的关联性"""
        try:
            if len(prediction_results) < 2:
                return {"correlation_analysis": "需要至少两个维度的预测数据"}

            # 构建多维度分析数据
            analysis_data = {}
            for pred_type, results in prediction_results.items():
                analysis_data[pred_type.value] = {
                    "trend": results.get("prediction_summary", {}).get(
                        "trend", "unknown"
                    ),
                    "max_value": results.get("prediction_summary", {}).get(
                        "max_value", 0
                    ),
                    "min_value": results.get("prediction_summary", {}).get(
                        "min_value", 0
                    ),
                    "anomaly_count": len(results.get("anomaly_predictions", [])),
                    "scaling_recommendations": len(
                        results.get("scaling_recommendations", [])
                    ),
                }

            # 构建多维度分析提示词
            prompt = prompt_builder.template_manager.format_template(
                "multi_dimension_comparison",
                prediction_results=json.dumps(
                    analysis_data, ensure_ascii=False, indent=2
                ),
                correlation_analysis="多维度预测关联性分析",
                resource_interaction="资源维度交互影响评估",
            )

            correlation_analysis = await self.llm_service.generate_response(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=800,
                use_task_model=False,  # 复杂操作：关联分析，使用主模型
            )

            if correlation_analysis:
                return {
                    "status": "success",
                    "correlation_analysis": correlation_analysis,
                    "analyzed_dimensions": list(prediction_results.keys()),
                    "timestamp": datetime.now(),
                }
            else:
                return {
                    "status": "fallback",
                    "correlation_analysis": "多维度分析暂时不可用",
                }

        except Exception as e:
            logger.error("多维度关联分析失败，异常类型: %s", type(e).__name__)
            return {"status": "error", "correlation_analysis": "分析失败"}

    def _parse_context_analysis(self, analysis_text: str) -> Dict[str, Any]:
        """解析上下文分析结果"""
        try:
            # 尝试提取结构化信息
            sections = {
                "data_quality": self._extract_section(analysis_text, "数据质量"),
                "pattern_recognition": self._extract_section(analysis_text, "模式识别"),
                "influence_factors": self._extract_section(analysis_text, "影响因素"),
                "prediction_challenges": self._extract_section(
                    analysis_text, "预测难点"
                ),
                "focus_points": self._extract_section(analysis_text, "关注点"),
            }

            # 过滤空内容
            sections = {k: v for k, v in sections.items() if v}

            return {
                "structured_analysis": sections,
                "full_text": analysis_text,
                "summary": self._generate_analysis_summary(sections),
            }

        except Exception as e:
            logger.warning("解析上下文分析失败，异常类型: %s", type(e).__name__)
            return {
                "structured_analysis": {},
                "full_text": analysis_text,
                "summary": "分析结果解析出现问题，请查看完整文本",
            }

    def _parse_interpretation_result(self, interpretation_text: str) -> Dict[str, Any]:
        """解析预测解读结果"""
        try:
            sections = {
                "quality_assessment": self._extract_section(
                    interpretation_text, "预测质量评估"
                ),
                "key_findings": self._extract_section(interpretation_text, "关键发现"),
                "risk_identification": self._extract_section(
                    interpretation_text, "风险识别"
                ),
                "key_timepoints": self._extract_section(
                    interpretation_text, "时间节点"
                ),
                "resource_impact": self._extract_section(
                    interpretation_text, "资源影响"
                ),
            }

            # 过滤空内容
            sections = {k: v for k, v in sections.items() if v}

            return {
                "structured_interpretation": sections,
                "full_text": interpretation_text,
                "interpretation_summary": self._generate_interpretation_summary(
                    sections
                ),
            }

        except Exception as e:
            logger.warning("解析预测解读失败，异常类型: %s", type(e).__name__)
            return {
                "structured_interpretation": {},
                "full_text": interpretation_text,
                "interpretation_summary": "解读结果解析出现问题，请查看完整文本",
            }

    def _extract_section(self, text: str, section_name: str) -> str:
        """从文本中提取指定章节内容"""
        import re

        # 匹配章节标题的正则模式
        patterns = [
            rf"\*\*{section_name}[^*]*?\*\*[：:]\s*([^*\n]+(?:\n[^*\n#]+)*)",
            rf"{section_name}[：:]\s*([^\n]+(?:\n[^#\n]+)*)",
            rf"\d+\.\s*\*\*{section_name}[^*]*?\*\*[：:]\s*([^*\n]+(?:\n[^*\n#]+)*)",
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
            if match:
                content = match.group(1).strip()
                # 清理内容
                content = re.sub(r"\n+", " ", content)
                content = re.sub(r"\s+", " ", content)
                return content

        return ""

    def _parse_insights_response(self, insights_text: str) -> List[str]:
        """解析洞察响应"""
        try:
            insights = []
            lines = insights_text.strip().split("\n")

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                # 移除列表标记
                line = re.sub(r"^[-*•]\s*", "", line)
                line = re.sub(r"^\d+\.\s*", "", line)

                if len(line) > 10:
                    insights.append(line)

            return insights[:7]

        except Exception as e:
            logger.warning("解析洞察响应失败，异常类型: %s", type(e).__name__)
            return ["洞察解析失败，请查看原始响应"]

    def _calculate_quantitative_metrics(
        self, prediction_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """计算量化指标"""
        try:
            predictions = prediction_results.get("predicted_data", [])
            if not predictions:
                return {}

            # 提取预测值
            values = [p.get("predicted_value", 0) for p in predictions]
            confidences = [
                p.get("confidence_level", 0)
                for p in predictions
                if p.get("confidence_level")
            ]

            # 计算基础统计
            metrics = {
                "prediction_count": len(predictions),
                "value_range": (
                    {"min": min(values), "max": max(values)} if values else {}
                ),
                "value_statistics": {
                    "mean": sum(values) / len(values) if values else 0,
                    "median": sorted(values)[len(values) // 2] if values else 0,
                },
                "confidence_statistics": (
                    {
                        "mean_confidence": (
                            sum(confidences) / len(confidences) if confidences else 0
                        ),
                        "min_confidence": min(confidences) if confidences else 0,
                        "confidence_trend": self._analyze_confidence_trend(confidences),
                    }
                    if confidences
                    else {}
                ),
            }

            # 计算变化趋势
            if len(values) >= 2:
                first_half = values[: len(values) // 2]
                second_half = values[len(values) // 2 :]

                first_avg = sum(first_half) / len(first_half)
                second_avg = sum(second_half) / len(second_half)

                metrics["trend_analysis"] = {
                    "direction": (
                        "increasing"
                        if second_avg > first_avg
                        else "decreasing"
                        if second_avg < first_avg
                        else "stable"
                    ),
                    "change_percentage": (
                        ((second_avg - first_avg) / first_avg * 100)
                        if first_avg > 0
                        else 0
                    ),
                }

            return metrics

        except Exception as e:
            logger.error("计算量化指标失败，异常类型: %s", type(e).__name__)
            return {}

    def _analyze_confidence_trend(self, confidences: List[float]) -> str:
        """分析置信度趋势"""
        if len(confidences) < 2:
            return "insufficient_data"

        first_half_avg = sum(confidences[: len(confidences) // 2]) / (
            len(confidences) // 2
        )
        second_half_avg = sum(confidences[len(confidences) // 2 :]) / (
            len(confidences) - len(confidences) // 2
        )

        if second_half_avg > first_half_avg + 0.05:
            return "increasing"
        elif second_half_avg < first_half_avg - 0.05:
            return "decreasing"
        else:
            return "stable"

    def _generate_analysis_summary(self, sections: Dict[str, str]) -> str:
        """生成分析摘要"""
        if not sections:
            return "分析结果为空"

        summary_parts = []
        for key, value in sections.items():
            if value:
                summary_parts.append(f"{key}: {value[:50]}...")

        return "; ".join(summary_parts) if summary_parts else "分析摘要生成失败"

    def _generate_interpretation_summary(self, sections: Dict[str, str]) -> str:
        """生成解读摘要"""
        if not sections:
            return "解读结果为空"

        key_points = []
        for key, value in sections.items():
            if value and len(value) > 20:
                key_points.append(value[:80])

        return " | ".join(key_points) if key_points else "解读摘要生成失败"

    # 降级方案
    def _get_fallback_context_analysis(
        self, prediction_type: PredictionType
    ) -> Dict[str, Any]:
        """获取降级上下文分析"""
        return {
            "status": "fallback",
            "analysis": {
                "structured_analysis": {
                    "data_quality": f"{prediction_type.value}历史数据质量评估需要更多信息",
                    "pattern_recognition": f"{prediction_type.value}模式识别功能暂时不可用",
                    "influence_factors": f"{prediction_type.value}影响因素分析需要人工介入",
                },
                "full_text": "上下文分析服务暂时不可用，请稍后重试",
                "summary": "降级分析模式",
            },
            "timestamp": datetime.now(),
        }

    def _get_fallback_interpretation(
        self, prediction_type: PredictionType
    ) -> Dict[str, Any]:
        """获取降级解读结果"""
        return {
            "status": "fallback",
            "interpretation": {
                "structured_interpretation": {
                    "quality_assessment": f"{prediction_type.value}预测质量评估需要手动分析",
                    "key_findings": f"{prediction_type.value}关键发现识别功能暂不可用",
                },
                "full_text": "预测解读服务暂时不可用，请参考量化指标",
                "interpretation_summary": "降级解读模式",
            },
            "quantitative_metrics": {},
            "timestamp": datetime.now(),
        }

    def _get_fallback_insights(
        self, prediction_type: PredictionType, prediction_results: Dict[str, Any]
    ) -> List[str]:
        """获取降级洞察"""
        fallback_insights = [
            f"{prediction_type.value}预测基于当前数据模式生成",
            "建议结合业务场景评估预测合理性",
            "关注预测趋势变化和异常点",
            "建议监控实际值与预测值的偏差",
        ]

        # 基于预测结果添加简单洞察
        if prediction_results.get("anomaly_predictions"):
            fallback_insights.append("检测到异常预测点，建议重点关注")

        if prediction_results.get("scaling_recommendations"):
            fallback_insights.append("系统建议进行资源扩缩容调整")

        return fallback_insights
