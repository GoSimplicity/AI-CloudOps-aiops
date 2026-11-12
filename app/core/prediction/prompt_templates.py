#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps智能预测系统提示词模板管理器 - 可配置的提示词系统
"""

from dataclasses import dataclass
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.models import PredictionType

logger = logging.getLogger("aiops.core.prediction.prompts")


@dataclass
class PromptTemplate:
    """提示词模板数据类"""

    name: str
    template: str
    variables: List[str]
    description: str
    category: str
    version: str = "1.0"

    def format(self, **kwargs) -> str:
        """格式化模板"""
        try:
            return self.template.format(**kwargs)
        except KeyError:
            raise ValueError(f"模板 {self.name} 缺少必要变量")


class PromptTemplateManager:
    """提示词模板管理器 - 支持动态加载和配置化管理"""

    def __init__(self):
        self.templates: Dict[str, PromptTemplate] = {}
        self._load_default_templates()

    def _load_default_templates(self) -> None:
        """加载默认提示词模板"""

        # 预测分析模板
        self._register_template(
            PromptTemplate(
                name="prediction_analysis",
                category="analysis",
                description="分析历史数据和预测上下文",
                variables=[
                    "prediction_type",
                    "current_value",
                    "historical_data",
                    "time_context",
                ],
                template="""你是一个专业的云平台资源分析专家。请分析以下{prediction_type}预测的上下文信息：

当前指标值: {current_value}
历史数据: {historical_data}
时间上下文: {time_context}

请从以下几个维度进行专业分析：
1. **数据质量评估**: 历史数据的完整性和可靠性如何？
2. **模式识别**: 识别出哪些时间模式和周期性变化？
3. **影响因素**: 可能影响{prediction_type}变化的关键因素有哪些？
4. **预测难点**: 这种类型的预测可能面临哪些挑战？
5. **建议关注点**: 在预测过程中应该重点关注哪些方面？

请用专业、简洁的语言提供分析结果，每个维度控制在2-3句话内。""",
            )
        )

        # 预测结果解读模板
        self._register_template(
            PromptTemplate(
                name="prediction_interpretation",
                category="interpretation",
                description="解读和分析预测结果",
                variables=[
                    "prediction_type",
                    "predictions_summary",
                    "confidence_stats",
                    "anomaly_info",
                    "trend_analysis",
                ],
                template="""作为云平台运维专家，请解读以下{prediction_type}预测结果：

预测摘要: {predictions_summary}
置信度统计: {confidence_stats}
异常信息: {anomaly_info}
趋势分析: {trend_analysis}

请提供专业的结果解读：
1. **预测质量评估**: 根据置信度和趋势，这次预测的可靠性如何？
2. **关键发现**: 预测结果中最重要的发现是什么？
3. **风险识别**: 预测结果显示了哪些潜在风险？
4. **时间节点**: 需要重点关注的时间点有哪些？
5. **资源影响**: 对其他资源可能产生的连锁影响？

每个方面用1-2句简洁的专业语言描述。""",
            )
        )

        # 综合报告模板
        self._register_template(
            PromptTemplate(
                name="comprehensive_report",
                category="report",
                description="生成综合预测分析报告",
                variables=[
                    "prediction_type",
                    "analysis_context",
                    "prediction_results",
                    "scaling_recommendations",
                    "cost_analysis",
                    "insights",
                ],
                template="""作为AI运维专家，基于以下信息生成{prediction_type}预测的综合分析报告：

分析背景: {analysis_context}
预测结果: {prediction_results}
扩缩容建议: {scaling_recommendations}
成本分析: {cost_analysis}
系统洞察: {insights}

请生成一份专业的综合报告，包含：

## 📊 预测概览
简要总结预测的核心发现和整体趋势

## ⚠️ 关键警示
标识需要立即或近期关注的风险点

## 📈 趋势解读
深度分析预测趋势的业务含义和技术影响

## 💡 优化建议
基于预测结果提出的具体优化建议

## 🎯 行动计划
短期（24小时内）和中期（一周内）的推荐行动

报告要求：
- 语言简洁专业，避免技术术语过度复杂
- 突出可操作的建议
- 每个部分控制在3-4句话
- 使用数据支撑观点""",
            )
        )

        # 异常预警模板
        self._register_template(
            PromptTemplate(
                name="anomaly_alert",
                category="alert",
                description="异常情况预警分析",
                variables=[
                    "prediction_type",
                    "anomaly_details",
                    "impact_assessment",
                    "historical_comparison",
                ],
                template="""检测到{prediction_type}预测中存在异常情况，请提供专业预警分析：

异常详情: {anomaly_details}
影响评估: {impact_assessment}
历史对比: {historical_comparison}

请生成预警分析：

🚨 **异常等级**: 基于影响范围和严重程度判断异常等级

🔍 **根因分析**: 分析可能导致异常的原因

⏰ **时间窗口**: 预计异常影响的时间范围

🛠️ **应对策略**: 建议的处理和缓解措施

📊 **监控重点**: 需要加强监控的指标和阈值

保持简洁专业，重点突出可执行的建议。""",
            )
        )

        # 扩缩容决策模板
        self._register_template(
            PromptTemplate(
                name="scaling_decision",
                category="scaling",
                description="扩缩容决策分析",
                variables=[
                    "prediction_type",
                    "current_resources",
                    "predicted_load",
                    "scaling_options",
                    "cost_considerations",
                ],
                template="""需要为{prediction_type}制定扩缩容策略，请分析以下信息：

当前资源配置: {current_resources}
预测负载: {predicted_load}
扩缩容选项: {scaling_options}
成本考量: {cost_considerations}

请提供扩缩容决策建议：

## 🎯 推荐方案
基于预测结果推荐的最佳扩缩容策略

## ⚖️ 方案权衡
不同方案的优缺点对比分析

## 💰 成本效益
方案的成本效益分析和ROI预估

## ⏱️ 执行时机
建议的执行时间和分阶段实施计划

## 🔄 回滚策略
如果方案效果不理想的备选方案

每个部分用简洁专业的语言，突出决策依据。""",
            )
        )

        # 多维度预测对比模板
        self._register_template(
            PromptTemplate(
                name="multi_dimension_comparison",
                category="comparison",
                description="多维度预测结果对比分析",
                variables=[
                    "prediction_results",
                    "correlation_analysis",
                    "resource_interaction",
                ],
                template="""请分析多个资源维度的预测结果及其相互关系：

预测结果: {prediction_results}
关联性分析: {correlation_analysis}
资源交互: {resource_interaction}

提供多维度分析：

## 🔗 资源关联性
分析不同资源指标之间的关联和相互影响

## ⚠️ 瓶颈识别
识别可能成为系统瓶颈的资源

## 🎨 优化策略
基于多维度分析的整体优化建议

## 📊 协调配置
各资源维度的协调配置建议

保持分析的系统性和实用性。""",
            )
        )

        logger.info(f"已加载 {len(self.templates)} 个默认提示词模板")

    def _register_template(self, template: PromptTemplate) -> None:
        """注册模板"""
        self.templates[template.name] = template
        logger.debug(f"注册模板: {template.name} ({template.category})")

    def get_template(self, name: str) -> Optional[PromptTemplate]:
        """获取指定名称的模板"""
        return self.templates.get(name)

    def get_templates_by_category(self, category: str) -> List[PromptTemplate]:
        """获取指定分类的所有模板"""
        return [t for t in self.templates.values() if t.category == category]

    def list_templates(self) -> Dict[str, str]:
        """列出所有模板"""
        return {name: template.description for name, template in self.templates.items()}

    def format_template(self, template_name: str, **kwargs) -> str:
        """格式化指定模板"""
        template = self.get_template(template_name)
        if not template:
            raise ValueError(f"未找到模板: {template_name}")

        return template.format(**kwargs)

    def add_custom_template(self, template: PromptTemplate) -> None:
        """添加自定义模板"""
        self._register_template(template)
        logger.info(f"添加自定义模板: {template.name}")

    def load_templates_from_file(self, file_path: str) -> None:
        """从文件加载模板"""
        try:
            path = Path(file_path)
            if not path.exists():
                logger.warning(f"模板文件不存在: {file_path}")
                return

            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for template_data in data.get("templates", []):
                template = PromptTemplate(**template_data)
                self._register_template(template)

            logger.info(f"从文件加载了 {len(data.get('templates', []))} 个模板")

        except Exception as e:
            logger.error("加载模板文件失败，异常类型: %s", type(e).__name__)

    def save_templates_to_file(self, file_path: str) -> None:
        """保存模板到文件"""
        try:
            templates_data = {
                "templates": [
                    {
                        "name": t.name,
                        "template": t.template,
                        "variables": t.variables,
                        "description": t.description,
                        "category": t.category,
                        "version": t.version,
                    }
                    for t in self.templates.values()
                ],
                "exported_at": datetime.now().isoformat(),
            }

            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            with open(path, "w", encoding="utf-8") as f:
                json.dump(templates_data, f, ensure_ascii=False, indent=2)

            logger.info(f"已保存 {len(self.templates)} 个模板到: {file_path}")

        except Exception as e:
            logger.error("保存模板文件失败，异常类型: %s", type(e).__name__)


class PredictionPromptBuilder:
    """预测提示词构建器 - 智能构建适合特定预测场景的提示词"""

    def __init__(self, template_manager: PromptTemplateManager):
        self.template_manager = template_manager

    def build_analysis_prompt(
        self,
        prediction_type: PredictionType,
        current_value: float,
        historical_data: List[Dict],
        additional_context: Optional[Dict] = None,
    ) -> str:
        """构建预测分析提示词"""

        # 处理历史数据摘要
        if historical_data:
            # 安全获取历史数据值，防止类型错误
            recent_values = []
            for d in historical_data[-24:]:  # 最近24个数据点
                if isinstance(d, dict):
                    recent_values.append(d.get("value", 0))
                else:
                    recent_values.append(0)

            if recent_values:
                historical_summary = f"最近数据点: {len(historical_data)}个, 最近24点范围: {min(recent_values):.2f}-{max(recent_values):.2f}"
            else:
                historical_summary = (
                    f"最近数据点: {len(historical_data)}个, 数据格式异常"
                )
        else:
            historical_summary = "暂无历史数据"

        # 生成时间上下文
        now = datetime.now()
        time_context = f"当前时间: {now.strftime('%Y-%m-%d %H:%M')}, 工作日: {'是' if now.weekday() < 5 else '否'}, 工作时间: {'是' if 9 <= now.hour <= 18 else '否'}"

        return self.template_manager.format_template(
            "prediction_analysis",
            prediction_type=prediction_type.value,
            current_value=current_value,
            historical_data=historical_summary,
            time_context=time_context,
        )

    def build_interpretation_prompt(
        self,
        prediction_type: PredictionType,
        prediction_results: Dict[str, Any],
        additional_analysis: Optional[Dict] = None,
    ) -> str:
        """构建预测结果解读提示词"""

        # 安全构建预测摘要，防止类型错误
        if isinstance(prediction_results, dict):
            predictions = prediction_results.get("predicted_data", [])
            if predictions:
                # 安全获取预测值
                values = []
                for p in predictions:
                    if isinstance(p, dict) and "predicted_value" in p:
                        values.append(p["predicted_value"])

                if values:
                    predictions_summary = f"预测点数: {len(predictions)}, 值范围: {min(values):.2f}-{max(values):.2f}, 平均值: {sum(values) / len(values):.2f}"
                else:
                    predictions_summary = f"预测点数: {len(predictions)}, 数据格式异常"
            else:
                predictions_summary = "暂无预测数据"
        else:
            predictions_summary = "预测结果格式异常"

        # 置信度统计
        confidence_stats = "暂无置信度信息"
        if isinstance(prediction_results, dict):
            predictions = prediction_results.get("predicted_data", [])
            if predictions:
                confidences = []
                for p in predictions:
                    if isinstance(p, dict) and p.get("confidence_level"):
                        confidences.append(p.get("confidence_level", 0))

                if confidences:
                    confidence_stats = f"平均置信度: {sum(confidences) / len(confidences):.2f}, 范围: {min(confidences):.2f}-{max(confidences):.2f}"

        # 异常信息
        anomaly_info = "未检测到异常"  # 默认值
        if isinstance(prediction_results, dict):
            anomalies = prediction_results.get("anomaly_predictions", [])
            if anomalies:
                # 安全获取高风险异常
                high_risk = []
                for a in anomalies:
                    if isinstance(a, dict) and a.get("impact_level") in [
                        "high",
                        "critical",
                    ]:
                        high_risk.append(a)
                anomaly_info = (
                    f"检测到 {len(anomalies)} 个异常点，其中 {len(high_risk)} 个高风险"
                )
            else:
                anomaly_info = "未检测到异常"

        # 趋势分析
        trend_analysis = "无趋势分析数据"  # 默认值
        if isinstance(prediction_results, dict):
            trend_info = prediction_results.get("prediction_summary", {})
            if isinstance(trend_info, dict):
                trend_analysis = f"趋势: {trend_info.get('trend', '未知')}, 峰值时间: {trend_info.get('peak_time', '未知')}"
            else:
                trend_analysis = "趋势分析数据格式异常"

        return self.template_manager.format_template(
            "prediction_interpretation",
            prediction_type=prediction_type.value,
            predictions_summary=predictions_summary,
            confidence_stats=confidence_stats,
            anomaly_info=anomaly_info,
            trend_analysis=trend_analysis,
        )

    def build_comprehensive_report_prompt(
        self,
        prediction_type: PredictionType,
        analysis_context: str,
        prediction_results: Dict[str, Any],
        scaling_recommendations: List[Dict],
        cost_analysis: Optional[Dict] = None,
        insights: Optional[List[str]] = None,
    ) -> str:
        """构建综合报告提示词"""

        # 处理扩缩容建议
        if scaling_recommendations:
            # 安全获取动作信息，防止类型错误
            actions = []
            for r in scaling_recommendations:
                if isinstance(r, dict):
                    actions.append(r.get("action", "unknown"))
                else:
                    actions.append("unknown")
            scaling_summary = (
                f"建议 {len(scaling_recommendations)} 项操作: {', '.join(set(actions))}"
            )
        else:
            scaling_summary = "暂无扩缩容建议"

        # 处理成本分析
        if cost_analysis and isinstance(cost_analysis, dict):
            cost_summary = f"成本节省潜力: {cost_analysis.get('cost_savings_potential', '未知')}%, 当前成本: {cost_analysis.get('current_hourly_cost', '未知')}/小时"
        else:
            cost_summary = "暂无成本分析"

        # 处理洞察
        insights_text = "; ".join(insights) if insights else "暂无特殊洞察"

        return self.template_manager.format_template(
            "comprehensive_report",
            prediction_type=prediction_type.value,
            analysis_context=analysis_context,
            prediction_results=str(
                prediction_results.get("prediction_summary", {})
                if isinstance(prediction_results, dict)
                else {}
            ),
            scaling_recommendations=scaling_summary,
            cost_analysis=cost_summary,
            insights=insights_text,
        )


# 全局模板管理器实例
template_manager = PromptTemplateManager()
prompt_builder = PredictionPromptBuilder(template_manager)
