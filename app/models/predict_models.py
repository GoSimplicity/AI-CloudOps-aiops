#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 预测服务专用数据模型
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.common.constants import ServiceConstants


class PredictionType(str, Enum):
    """预测类型枚举"""

    QPS = "qps"
    CPU = "cpu"
    MEMORY = "memory"
    DISK = "disk"


class PredictionGranularity(str, Enum):
    """预测时间粒度枚举"""

    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"


class ScalingAction(str, Enum):
    """扩缩容建议动作枚举"""

    SCALE_UP = "scale_up"
    SCALE_DOWN = "scale_down"
    MAINTAIN = "maintain"


class ResourceConstraints(BaseModel):
    """资源约束配置模型"""

    cpu_cores: Optional[float] = Field(None, gt=0)
    memory_gb: Optional[float] = Field(None, gt=0)
    disk_gb: Optional[float] = Field(None, gt=0)
    max_instances: Optional[int] = Field(None, gt=0, le=1000)
    min_instances: Optional[int] = Field(None, gt=0, le=100)
    cost_per_hour: Optional[float] = Field(None, ge=0)


class BasePredictionRequest(BaseModel):
    """预测请求基础模型"""

    model_config = ConfigDict(extra="forbid")

    metric_query: Optional[str] = Field(None)
    prediction_hours: int = Field(default=24, ge=1, le=168)
    granularity: PredictionGranularity = Field(default=PredictionGranularity.HOUR)
    resource_constraints: Optional[ResourceConstraints] = Field(None)
    include_confidence: bool = Field(default=True)
    include_anomaly_detection: bool = Field(default=True)
    consider_historical_pattern: bool = Field(default=True)
    target_utilization: float = Field(default=0.7, gt=0, le=1)
    sensitivity: float = Field(default=0.8, ge=0.1, le=1.0)
    enable_ai_insights: bool = Field(default=False)
    ai_report_style: str = Field(default="professional")

    @field_validator("target_utilization")
    @classmethod
    def validate_target_utilization(cls, v: float) -> float:
        min_utilization = 0.1
        max_utilization = 0.9
        if v < min_utilization or v > max_utilization:
            raise ValueError(f"目标利用率应在{min_utilization}-{max_utilization}之间")
        return v

    @field_validator("metric_query")
    @classmethod
    def validate_metric_query(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v.strip() == "":
            raise ValueError("指标查询不能为空字符串")
        return v


class QpsPredictionRequest(BasePredictionRequest):
    """QPS预测专用请求模型"""

    current_qps: float = Field(..., gt=0)

    @field_validator("current_qps")
    @classmethod
    def validate_current_qps(cls, v: float) -> float:
        if (
            v <= ServiceConstants.PREDICTION_MIN_QPS
            or v > ServiceConstants.PREDICTION_MAX_QPS
        ):
            raise ValueError(
                f"QPS值应在{ServiceConstants.PREDICTION_MIN_QPS}-{ServiceConstants.PREDICTION_MAX_QPS}之间"
            )
        return v


class CpuPredictionRequest(BasePredictionRequest):
    """CPU预测专用请求模型"""

    current_cpu_percent: float = Field(..., ge=0, le=100)

    @field_validator("current_cpu_percent")
    @classmethod
    def validate_current_cpu_percent(cls, v: float) -> float:
        min_cpu = 0.0
        max_cpu = 100.0
        if v < min_cpu or v > max_cpu:
            raise ValueError(f"CPU利用率应在{min_cpu}-{max_cpu}%之间")
        return v


class MemoryPredictionRequest(BasePredictionRequest):
    """内存预测专用请求模型"""

    current_memory_percent: float = Field(..., ge=0, le=100)

    @field_validator("current_memory_percent")
    @classmethod
    def validate_current_memory_percent(cls, v: float) -> float:
        min_memory = 0.0
        max_memory = 100.0
        if v < min_memory or v > max_memory:
            raise ValueError(f"内存利用率应在{min_memory}-{max_memory}%之间")
        return v


class DiskPredictionRequest(BasePredictionRequest):
    """磁盘预测专用请求模型"""

    current_disk_percent: float = Field(..., ge=0, le=100)

    @field_validator("current_disk_percent")
    @classmethod
    def validate_current_disk_percent(cls, v: float) -> float:
        min_disk = 0.0
        max_disk = 100.0
        if v < min_disk or v > max_disk:
            raise ValueError(f"磁盘利用率应在{min_disk}-{max_disk}%之间")
        return v


class PredictionDataPoint(BaseModel):
    """预测数据点模型"""

    timestamp: datetime = Field(...)
    predicted_value: float = Field(...)
    confidence_lower: Optional[float] = Field(None)
    confidence_upper: Optional[float] = Field(None)
    confidence_level: Optional[float] = Field(None, ge=0, le=1)


class ResourceUtilization(BaseModel):
    """资源利用率预测模型"""

    timestamp: datetime = Field(..., description="时间点")
    cpu_utilization: Optional[float] = Field(
        None, description="CPU利用率(%)", ge=0, le=100
    )
    memory_utilization: Optional[float] = Field(
        None, description="内存利用率(%)", ge=0, le=100
    )
    disk_utilization: Optional[float] = Field(
        None, description="磁盘利用率(%)", ge=0, le=100
    )
    predicted_load: Optional[float] = Field(None, description="预测负载值")


class ScalingRecommendation(BaseModel):
    """扩缩容建议模型"""

    action: ScalingAction = Field(...)
    trigger_time: datetime = Field(...)
    confidence: Optional[float] = Field(None, ge=0, le=1)
    reason: str = Field(...)
    target_instances: Optional[int] = Field(None, gt=0)
    target_cpu_cores: Optional[float] = Field(None, gt=0)
    target_memory_gb: Optional[float] = Field(None, gt=0)
    target_disk_gb: Optional[float] = Field(None, gt=0)
    estimated_cost_change: Optional[float] = Field(None)


class CostAnalysis(BaseModel):
    """成本分析模型"""

    current_hourly_cost: Optional[float] = Field(None, ge=0)
    predicted_hourly_cost: Optional[float] = Field(None, ge=0)
    cost_savings_potential: Optional[float] = Field(None)
    cost_trend_analysis: Dict[str, Any] = Field(default_factory=dict)


class AnomalyPrediction(BaseModel):
    """异常预测模型"""

    timestamp: datetime = Field(...)
    anomaly_score: float = Field(..., ge=0, le=1)
    anomaly_type: str = Field(...)
    impact_level: str = Field(..., pattern="^(low|medium|high|critical)$")
    predicted_value: float = Field(...)
    expected_value: float = Field(...)


class PredictionResponse(BaseModel):
    """统一预测响应模型"""

    prediction_type: PredictionType = Field(..., description="预测类型")
    prediction_hours: int = Field(..., description="预测小时数")
    granularity: PredictionGranularity = Field(..., description="预测时间粒度")
    current_value: float = Field(..., description="当前指标值")
    predicted_data: List[PredictionDataPoint] = Field(..., description="预测数据点列表")
    resource_utilization: List[ResourceUtilization] = Field(
        default_factory=list, description="资源利用率预测"
    )
    scaling_recommendations: List[ScalingRecommendation] = Field(
        default_factory=list, description="扩缩容建议列表"
    )
    anomaly_predictions: List[AnomalyPrediction] = Field(
        default_factory=list, description="异常预测列表"
    )
    cost_analysis: Optional[CostAnalysis] = Field(None, description="成本分析结果")
    pattern_analysis: Dict[str, Any] = Field(
        default_factory=dict, description="模式分析结果"
    )
    trend_insights: List[str] = Field(default_factory=list, description="趋势洞察")
    model_accuracy: Optional[float] = Field(None, description="模型准确率", ge=0, le=1)
    method: Optional[str] = Field(None, description="实际预测方法")
    model_version: Optional[str] = Field(None, description="已加载模型版本；未知为 null")
    fallback_reason: Optional[str] = Field(None, description="降级原因")
    fallback_mode: Optional[bool] = Field(None, description="是否为降级模式")
    analysis_failures: List[str] = Field(default_factory=list, description="未完成的分析环节")
    ai_fallback_reason: Optional[str] = Field(None, description="AI分析降级原因")
    prediction_summary: Dict[str, Any] = Field(
        default_factory=dict, description="预测摘要信息"
    )

    # AI增强字段（可选）
    ai_enhanced: Optional[bool] = Field(None, description="是否启用了AI增强分析")
    ai_insights: Optional[List[str]] = Field(None, description="AI生成的洞察")
    ai_analysis_context: Optional[Dict[str, Any]] = Field(
        None, description="AI分析上下文"
    )
    ai_prediction_interpretation: Optional[Dict[str, Any]] = Field(
        None, description="AI预测解读"
    )
    ai_reports: Optional[Dict[str, Any]] = Field(None, description="AI生成的报告")
    analysis_id: Optional[str] = Field(None, description="分析会话ID")
    processing_time_seconds: Optional[float] = Field(None, description="处理时间(秒)")
    data_quality_assessment: Optional[Dict[str, Any]] = Field(
        None, description="数据质量评估"
    )

    timestamp: datetime = Field(
        default_factory=datetime.now, description="预测生成时间"
    )


class ModelInfo(BaseModel):
    """预测模型信息"""

    model_name: str = Field(..., description="模型名称")
    model_version: Optional[str] = Field(None, description="模型版本号，缺少元数据时为 null")
    model_type: str = Field(..., description="模型类型")
    supported_prediction_types: List[PredictionType] = Field(
        default_factory=list, description="支持的预测类型"
    )
    training_data_size: Optional[int] = Field(None, description="训练数据样本数")
    last_trained: Optional[datetime] = Field(None, description="最后训练时间")
    accuracy_metrics: Dict[str, float] = Field(
        default_factory=dict, description="模型准确性指标"
    )
    feature_importance: Dict[str, float] = Field(
        default_factory=dict, description="特征重要性分析"
    )


class PredictionServiceHealthResponse(BaseModel):
    """预测服务健康检查响应"""

    service_status: str = Field(..., description="服务整体状态")
    model_status: str = Field(..., description="模型加载状态")
    models_loaded: List[ModelInfo] = Field(
        default_factory=list, description="已加载模型列表"
    )
    supported_prediction_types: List[PredictionType] = Field(
        default_factory=list, description="支持的预测类型"
    )
    last_prediction_time: Optional[datetime] = Field(
        None, description="最后一次预测时间"
    )
    total_predictions: int = Field(default=0, description="累计预测次数")
    error_rate: float = Field(default=0.0, description="预测错误率", ge=0, le=1)
    average_response_time_ms: Optional[float] = Field(
        None, description="平均响应时间(毫秒)"
    )
    resource_usage: Dict[str, float] = Field(
        default_factory=dict, description="服务资源使用情况"
    )
    timestamp: datetime = Field(
        default_factory=datetime.now, description="健康检查时间"
    )


# AI增强预测相关响应模型


class AIAnalysisContext(BaseModel):
    """AI分析上下文模型"""

    status: str = Field(..., description="分析状态")
    analysis: Dict[str, Any] = Field(default_factory=dict, description="分析结果")
    timestamp: datetime = Field(default_factory=datetime.now, description="分析时间")


class AIPredictionInterpretation(BaseModel):
    """AI预测解读模型"""

    status: str = Field(..., description="解读状态")
    interpretation: Dict[str, Any] = Field(default_factory=dict, description="解读结果")
    quantitative_metrics: Dict[str, Any] = Field(
        default_factory=dict, description="量化指标"
    )
    timestamp: datetime = Field(default_factory=datetime.now, description="解读时间")


class AIReport(BaseModel):
    """AI报告模型"""

    comprehensive_report: Optional[Dict[str, Any]] = Field(None, description="综合报告")
    executive_summary: Optional[Dict[str, Any]] = Field(None, description="执行摘要")
    action_plan: Optional[Dict[str, Any]] = Field(None, description="行动计划")
    cost_optimization: Optional[Dict[str, Any]] = Field(
        None, description="成本优化报告"
    )


class AIEnhancedPredictionResponse(BaseModel):
    """AI增强预测响应模型"""

    # 继承基础预测响应字段
    prediction_type: PredictionType = Field(..., description="预测类型")
    prediction_hours: int = Field(..., description="预测小时数")
    granularity: PredictionGranularity = Field(..., description="预测时间粒度")
    current_value: float = Field(..., description="当前指标值")
    predicted_data: List[PredictionDataPoint] = Field(..., description="预测数据点列表")
    resource_utilization: List[ResourceUtilization] = Field(
        default_factory=list, description="资源利用率预测"
    )
    scaling_recommendations: List[ScalingRecommendation] = Field(
        default_factory=list, description="扩缩容建议列表"
    )
    anomaly_predictions: List[AnomalyPrediction] = Field(
        default_factory=list, description="异常预测列表"
    )
    cost_analysis: Optional[CostAnalysis] = Field(None, description="成本分析结果")
    pattern_analysis: Dict[str, Any] = Field(
        default_factory=dict, description="模式分析结果"
    )
    trend_insights: List[str] = Field(default_factory=list, description="趋势洞察")
    model_accuracy: Optional[float] = Field(None, description="模型准确率", ge=0, le=1)
    method: Optional[str] = Field(None, description="实际预测方法")
    model_version: Optional[str] = Field(None, description="已加载模型版本；未知为 null")
    prediction_summary: Dict[str, Any] = Field(
        default_factory=dict, description="预测摘要信息"
    )

    # AI增强特有字段
    ai_enhanced: bool = Field(default=True, description="是否AI增强")
    analysis_context: Optional[AIAnalysisContext] = Field(
        None, description="AI分析上下文"
    )
    prediction_interpretation: Optional[AIPredictionInterpretation] = Field(
        None, description="AI预测解读"
    )
    ai_insights: List[str] = Field(default_factory=list, description="AI生成的洞察")
    ai_reports: Optional[AIReport] = Field(None, description="AI生成的报告")
    analysis_id: Optional[str] = Field(None, description="分析ID")
    processing_time_seconds: Optional[float] = Field(None, description="处理时间(秒)")
    ai_processing_stages: Dict[str, str] = Field(
        default_factory=dict, description="AI处理阶段状态"
    )
    data_quality_assessment: Dict[str, Any] = Field(
        default_factory=dict, description="数据质量评估"
    )

    # 降级模式字段
    fallback_mode: Optional[bool] = Field(None, description="是否为降级模式")
    fallback_reason: Optional[str] = Field(None, description="降级原因")
    ai_fallback_reason: Optional[str] = Field(None, description="AI分析降级原因")
    analysis_failures: List[str] = Field(default_factory=list, description="未完成的分析环节")

    timestamp: datetime = Field(
        default_factory=datetime.now, description="预测生成时间"
    )


class MultiDimensionPredictionResponse(BaseModel):
    """多维度预测响应模型"""

    analysis_id: str = Field(..., description="分析ID")
    prediction_results: Dict[str, Dict[str, Any]] = Field(
        ..., description="各维度预测结果"
    )
    correlation_analysis: Optional[Dict[str, Any]] = Field(
        None, description="关联分析结果"
    )
    multi_dimension_insights: List[str] = Field(
        default_factory=list, description="多维度洞察"
    )
    multi_dimension_reports: Dict[str, Any] = Field(default_factory=dict, description="基于最终各维度及关联结果的报告")
    summary_statistics: Dict[str, Any] = Field(
        default_factory=dict, description="汇总统计信息"
    )
    processing_time_seconds: float = Field(..., description="处理时间(秒)")
    analyzed_dimensions: List[str] = Field(..., description="分析的维度列表")
    timestamp: datetime = Field(default_factory=datetime.now, description="分析时间")


class PredictionReportResponse(BaseModel):
    """预测报告响应模型"""

    analysis_id: str = Field(..., description="分析ID")
    report_type: str = Field(..., description="报告类型")
    report_style: str = Field(..., description="报告风格")
    status: str = Field(..., description="生成状态")
    report_content: Optional[str] = Field(None, description="报告内容")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="报告元数据")
    generated_at: datetime = Field(default_factory=datetime.now, description="生成时间")


class AICapabilitiesResponse(BaseModel):
    """AI功能能力响应模型"""

    ai_enhanced_prediction: Dict[str, Any] = Field(
        default_factory=dict, description="AI增强预测功能"
    )
    multi_dimension_analysis: Dict[str, Any] = Field(
        default_factory=dict, description="多维度分析功能"
    )
    intelligent_reporting: Dict[str, Any] = Field(
        default_factory=dict, description="智能报告功能"
    )
    ai_models: Dict[str, Any] = Field(default_factory=dict, description="AI模型信息")
    service_info: Dict[str, Any] = Field(default_factory=dict, description="服务信息")


class ModelInfoResponse(BaseModel):
    """模型信息响应模型"""

    models: List[Dict[str, Any]]
    total_models: int
    loaded_models: int
    status: str
    timestamp: str


__all__ = [
    "PredictionType",
    "PredictionGranularity",
    "ScalingAction",
    "ResourceConstraints",
    "PredictionDataPoint",
    "ResourceUtilization",
    "ScalingRecommendation",
    "CostAnalysis",
    "AnomalyPrediction",
    "ModelInfo",
    "BasePredictionRequest",
    "QpsPredictionRequest",
    "CpuPredictionRequest",
    "MemoryPredictionRequest",
    "DiskPredictionRequest",
    "PredictionResponse",
    "PredictionServiceHealthResponse",
    "AIAnalysisContext",
    "AIPredictionInterpretation",
    "AIReport",
    "AIEnhancedPredictionResponse",
    "MultiDimensionPredictionResponse",
    "PredictionReportResponse",
    "AICapabilitiesResponse",
    "ModelInfoResponse",
]
