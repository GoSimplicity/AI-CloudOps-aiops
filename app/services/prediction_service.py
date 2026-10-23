#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps智能预测服务 - 提供四种资源预测能力
"""

from datetime import datetime, timedelta
import hashlib
import logging
import math
from typing import Any, Dict, List, Optional

from app.common.exceptions import HistoricalDataError, PredictionError, ValidationError
from app.core.prediction import (
    AnomalyDetector,
    CostAnalyzer,
    FeatureExtractor,
    IntelligentPredictor,
    IntelligentReportGenerator,
    ModelManager,
    PredictionAnalyzer,
    ScalingAdvisor,
    UnifiedPredictor,
)
from app.models import (
    AnomalyPrediction,
    CostAnalysis,
    PredictionDataPoint,
    PredictionGranularity,
    PredictionType,
    ResourceConstraints,
    ResourceUtilization,
    ScalingRecommendation,
)
from app.services.base import BaseService, HealthCheckMixin

logger = logging.getLogger("aiops.services.prediction")


class PredictionService(BaseService, HealthCheckMixin):
    """统一预测服务 - 支持QPS、CPU、内存、磁盘多维度预测"""

    def __init__(self) -> None:
        super().__init__("prediction")
        # 基础预测组件
        self._predictor: Optional[UnifiedPredictor] = None
        self._feature_extractor: Optional[FeatureExtractor] = None
        self._anomaly_detector: Optional[AnomalyDetector] = None
        self._scaling_advisor: Optional[ScalingAdvisor] = None
        self._cost_analyzer: Optional[CostAnalyzer] = None
        self._model_manager: Optional[ModelManager] = None

        # 增强组件
        self._intelligent_predictor: Optional[IntelligentPredictor] = None
        self._prediction_analyzer: Optional[PredictionAnalyzer] = None
        self._report_generator: Optional[IntelligentReportGenerator] = None

        # Redis缓存管理器
        self._cache_manager = None

        self._initialized = False

    async def _do_initialize(self) -> None:
        """初始化预测服务组件"""
        try:
            from app.config.settings import config
            from app.core.cache.redis_cache_manager import RedisCacheManager
            from app.core.prediction import ModelManager

            # 初始化Redis缓存管理器
            try:
                redis_config = {
                    "host": config.redis.host,
                    "port": config.redis.port,
                    "db": config.redis.db + 2,  # 使用单独的db用于预测缓存
                    "password": config.redis.password
                    if hasattr(config.redis, "password")
                    else "",
                }
                self._cache_manager = RedisCacheManager(
                    redis_config=redis_config,
                    cache_prefix="predict_cache:",
                    default_ttl=3600,  # 1小时缓存
                    max_cache_size=5000,
                    enable_compression=True,
                )
                self.logger.info("预测服务Redis缓存管理器初始化成功")
            except Exception as cache_e:
                self.logger.warning(
                    f"Redis缓存管理器初始化失败: {str(cache_e)}，将在无缓存模式下运行"
                )
                self._cache_manager = None

            self._model_manager = ModelManager()
            await self._model_manager.initialize()

            # 初始化特征提取器
            self._feature_extractor = FeatureExtractor()

            # 初始化核心预测器
            self._predictor = UnifiedPredictor(
                model_manager=self._model_manager,
                feature_extractor=self._feature_extractor,
            )
            await self._predictor.initialize()

            # 初始化异常检测器
            self._anomaly_detector = AnomalyDetector()

            # 初始化扩缩容顾问
            self._scaling_advisor = ScalingAdvisor()

            # 初始化成本分析器
            self._cost_analyzer = CostAnalyzer()

            # 初始化增强组件
            try:
                # 预测引擎（带外部分析）
                from app.services.llm import LLMService

                llm_service = LLMService()
                self._intelligent_predictor = IntelligentPredictor(
                    model_manager=self._model_manager,
                    feature_extractor=self._feature_extractor,
                    llm_client=llm_service,
                )
                await self._intelligent_predictor.initialize()

                # 预测分析器
                self._prediction_analyzer = PredictionAnalyzer(llm_service)

                # 报告生成器
                self._report_generator = IntelligentReportGenerator(llm_service)

                self.logger.info("增强组件初始化完成")

            except Exception as ai_e:
                self.logger.warning(
                    f"增强组件初始化失败: {str(ai_e)}，将使用基础预测模式"
                )
                self._intelligent_predictor = None
                self._prediction_analyzer = None
                self._report_generator = None

            self._initialized = True
            self.logger.info("预测服务所有组件初始化完成")

        except Exception as e:
            self.logger.error(f"预测服务初始化失败: {str(e)}")
            self._initialized = False
            raise PredictionError(f"服务初始化失败: {str(e)}")

    async def _do_health_check(self) -> bool:
        """健康检查"""
        try:
            if not self._initialized:
                return False

            # 检查模型管理器
            if not self._model_manager or not await self._model_manager.is_healthy():
                return False

            # 检查预测器
            if not self._predictor or not await self._predictor.is_healthy():
                return False

            return True

        except Exception as e:
            self.logger.warning(f"健康检查失败: {str(e)}")
            return False

    def _ensure_initialized(self) -> None:
        """确保服务已初始化"""
        if not self._initialized:
            raise PredictionError("预测服务未初始化，请先调用 initialize()")

    async def predict_qps(
        self,
        current_qps: float,
        metric_query: Optional[str] = None,
        prediction_hours: int = 24,
        granularity: str = "hour",
        resource_constraints: Optional[Dict] = None,
        include_confidence: bool = True,
        include_anomaly_detection: bool = True,
        consider_historical_pattern: bool = True,
        target_utilization: float = 0.7,
        sensitivity: float = 0.8,
    ) -> Dict[str, Any]:
        """QPS负载预测"""
        try:
            self._ensure_initialized()
            # 验证参数
            self._validate_qps_params(current_qps, prediction_hours)

            # 生成缓存键
            cache_key = self._generate_prediction_cache_key(
                prediction_type="qps",
                current_value=current_qps,
                metric_query=metric_query,
                prediction_hours=prediction_hours,
                granularity=granularity,
                resource_constraints=resource_constraints,
                ai_enhanced=False,
                target_utilization=target_utilization,
                sensitivity=sensitivity,
                include_confidence=include_confidence,
                include_anomaly_detection=include_anomaly_detection,
                consider_historical_pattern=consider_historical_pattern,
            )

            # 尝试从缓存获取结果
            cached_result = await self._get_from_cache(cache_key)
            if cached_result:
                self.logger.info("QPS预测缓存命中，直接返回结果")
                return cached_result

            # 获取历史数据
            historical_data = await self._fetch_historical_data(
                PredictionType.QPS, metric_query, hours=48
            )

            # 执行预测
            predictions = await self._predictor.predict(
                prediction_type=PredictionType.QPS,
                current_value=current_qps,
                historical_data=historical_data,
                prediction_hours=prediction_hours,
                granularity=PredictionGranularity(granularity),
                consider_pattern=consider_historical_pattern,
            )

            # 生成扩缩容建议
            scaling_recommendations = (
                await self._scaling_advisor.generate_recommendations(
                    predictions=predictions,
                    prediction_type=PredictionType.QPS,
                    target_utilization=target_utilization,
                    constraints=(
                        ResourceConstraints(**resource_constraints)
                        if resource_constraints
                        else None
                    ),
                )
            )

            # 异常检测
            anomaly_predictions = []
            if include_anomaly_detection:
                anomaly_predictions = await self._anomaly_detector.detect_anomalies(
                    predictions=predictions, sensitivity=sensitivity
                )

            # 成本分析
            cost_analysis = None
            if resource_constraints:
                cost_analysis = await self._cost_analyzer.analyze_cost(
                    predictions=predictions,
                    scaling_recommendations=scaling_recommendations,
                    constraints=ResourceConstraints(**resource_constraints),
                )

            # 构建响应
            result = self._build_prediction_response(
                prediction_type=PredictionType.QPS,
                current_value=current_qps,
                predictions=predictions,
                scaling_recommendations=scaling_recommendations,
                anomaly_predictions=anomaly_predictions,
                cost_analysis=cost_analysis,
                prediction_hours=prediction_hours,
                granularity=granularity,
                include_confidence=include_confidence,
            )

            # 保存到缓存
            await self._save_to_cache(cache_key, result, ttl=3600)  # 1小时缓存

            return result

        except (ValidationError, HistoricalDataError):
            # 参数验证错误直接透传
            raise
        except Exception as e:
            self.logger.error(f"QPS预测失败: {str(e)}")
            raise PredictionError(f"QPS预测失败: {str(e)}")

    async def predict_cpu_utilization(
        self,
        current_cpu_percent: float,
        metric_query: Optional[str] = None,
        prediction_hours: int = 24,
        granularity: str = "hour",
        resource_constraints: Optional[Dict] = None,
        include_confidence: bool = True,
        include_anomaly_detection: bool = True,
        consider_historical_pattern: bool = True,
        target_utilization: float = 0.7,
        sensitivity: float = 0.8,
    ) -> Dict[str, Any]:
        """CPU使用率预测"""
        try:
            self._ensure_initialized()
            # 验证参数
            self._validate_utilization_params(current_cpu_percent, prediction_hours)

            # 生成缓存键
            cache_key = self._generate_prediction_cache_key(
                prediction_type="cpu",
                current_value=current_cpu_percent,
                metric_query=metric_query,
                prediction_hours=prediction_hours,
                granularity=granularity,
                resource_constraints=resource_constraints,
                ai_enhanced=False,
                target_utilization=target_utilization,
                sensitivity=sensitivity,
                include_confidence=include_confidence,
                include_anomaly_detection=include_anomaly_detection,
                consider_historical_pattern=consider_historical_pattern,
            )

            # 尝试从缓存获取结果
            cached_result = await self._get_from_cache(cache_key)
            if cached_result:
                self.logger.info("CPU预测缓存命中，直接返回结果")
                return cached_result

            # 获取历史数据
            historical_data = await self._fetch_historical_data(
                PredictionType.CPU, metric_query, hours=48
            )

            # 执行预测
            predictions = await self._predictor.predict(
                prediction_type=PredictionType.CPU,
                current_value=current_cpu_percent,
                historical_data=historical_data,
                prediction_hours=prediction_hours,
                granularity=PredictionGranularity(granularity),
                consider_pattern=consider_historical_pattern,
            )

            # 生成扩缩容建议
            scaling_recommendations = (
                await self._scaling_advisor.generate_recommendations(
                    predictions=predictions,
                    prediction_type=PredictionType.CPU,
                    target_utilization=target_utilization,
                    constraints=(
                        ResourceConstraints(**resource_constraints)
                        if resource_constraints
                        else None
                    ),
                )
            )

            # 异常检测
            anomaly_predictions = []
            if include_anomaly_detection:
                anomaly_predictions = await self._anomaly_detector.detect_anomalies(
                    predictions=predictions, sensitivity=sensitivity
                )

            # 成本分析
            cost_analysis = None
            if resource_constraints:
                cost_analysis = await self._cost_analyzer.analyze_cost(
                    predictions=predictions,
                    scaling_recommendations=scaling_recommendations,
                    constraints=ResourceConstraints(**resource_constraints),
                )

            # 构建响应
            result = self._build_prediction_response(
                prediction_type=PredictionType.CPU,
                current_value=current_cpu_percent,
                predictions=predictions,
                scaling_recommendations=scaling_recommendations,
                anomaly_predictions=anomaly_predictions,
                cost_analysis=cost_analysis,
                prediction_hours=prediction_hours,
                granularity=granularity,
                include_confidence=include_confidence,
            )

            # 保存到缓存
            await self._save_to_cache(cache_key, result, ttl=3600)  # 1小时缓存

            return result

        except (ValidationError, HistoricalDataError):
            raise
        except Exception as e:
            self.logger.error(f"CPU预测失败: {str(e)}")
            raise PredictionError(f"CPU预测失败: {str(e)}")

    async def predict_memory_utilization(
        self,
        current_memory_percent: float,
        metric_query: Optional[str] = None,
        prediction_hours: int = 24,
        granularity: str = "hour",
        resource_constraints: Optional[Dict] = None,
        include_confidence: bool = True,
        include_anomaly_detection: bool = True,
        consider_historical_pattern: bool = True,
        target_utilization: float = 0.7,
        sensitivity: float = 0.8,
    ) -> Dict[str, Any]:
        """内存使用率预测"""
        try:
            self._ensure_initialized()
            # 验证参数
            self._validate_utilization_params(current_memory_percent, prediction_hours)

            # 生成缓存键
            cache_key = self._generate_prediction_cache_key(
                prediction_type="memory",
                current_value=current_memory_percent,
                metric_query=metric_query,
                prediction_hours=prediction_hours,
                granularity=granularity,
                resource_constraints=resource_constraints,
                ai_enhanced=False,
                target_utilization=target_utilization,
                sensitivity=sensitivity,
                include_confidence=include_confidence,
                include_anomaly_detection=include_anomaly_detection,
                consider_historical_pattern=consider_historical_pattern,
            )

            # 尝试从缓存获取结果
            cached_result = await self._get_from_cache(cache_key)
            if cached_result:
                self.logger.info("内存预测缓存命中，直接返回结果")
                return cached_result

            # 获取历史数据
            historical_data = await self._fetch_historical_data(
                PredictionType.MEMORY, metric_query, hours=48
            )

            # 执行预测
            predictions = await self._predictor.predict(
                prediction_type=PredictionType.MEMORY,
                current_value=current_memory_percent,
                historical_data=historical_data,
                prediction_hours=prediction_hours,
                granularity=PredictionGranularity(granularity),
                consider_pattern=consider_historical_pattern,
            )

            # 生成扩缩容建议
            scaling_recommendations = (
                await self._scaling_advisor.generate_recommendations(
                    predictions=predictions,
                    prediction_type=PredictionType.MEMORY,
                    target_utilization=target_utilization,
                    constraints=(
                        ResourceConstraints(**resource_constraints)
                        if resource_constraints
                        else None
                    ),
                )
            )

            # 异常检测
            anomaly_predictions = []
            if include_anomaly_detection:
                anomaly_predictions = await self._anomaly_detector.detect_anomalies(
                    predictions=predictions, sensitivity=sensitivity
                )

            # 成本分析
            cost_analysis = None
            if resource_constraints:
                cost_analysis = await self._cost_analyzer.analyze_cost(
                    predictions=predictions,
                    scaling_recommendations=scaling_recommendations,
                    constraints=ResourceConstraints(**resource_constraints),
                )

            # 构建响应
            result = self._build_prediction_response(
                prediction_type=PredictionType.MEMORY,
                current_value=current_memory_percent,
                predictions=predictions,
                scaling_recommendations=scaling_recommendations,
                anomaly_predictions=anomaly_predictions,
                cost_analysis=cost_analysis,
                prediction_hours=prediction_hours,
                granularity=granularity,
                include_confidence=include_confidence,
            )

            # 保存到缓存
            await self._save_to_cache(cache_key, result, ttl=3600)  # 1小时缓存

            return result

        except (ValidationError, HistoricalDataError):
            raise
        except Exception as e:
            self.logger.error(f"内存预测失败: {str(e)}")
            raise PredictionError(f"内存预测失败: {str(e)}")

    async def predict_disk_utilization(
        self,
        current_disk_percent: float,
        metric_query: Optional[str] = None,
        prediction_hours: int = 24,
        granularity: str = "hour",
        resource_constraints: Optional[Dict] = None,
        include_confidence: bool = True,
        include_anomaly_detection: bool = True,
        consider_historical_pattern: bool = True,
        target_utilization: float = 0.7,
        sensitivity: float = 0.8,
    ) -> Dict[str, Any]:
        """磁盘使用率预测"""
        try:
            self._ensure_initialized()
            # 验证参数
            self._validate_utilization_params(current_disk_percent, prediction_hours)

            # 生成缓存键
            cache_key = self._generate_prediction_cache_key(
                prediction_type="disk",
                current_value=current_disk_percent,
                metric_query=metric_query,
                prediction_hours=prediction_hours,
                granularity=granularity,
                resource_constraints=resource_constraints,
                ai_enhanced=False,
                target_utilization=target_utilization,
                sensitivity=sensitivity,
                include_confidence=include_confidence,
                include_anomaly_detection=include_anomaly_detection,
                consider_historical_pattern=consider_historical_pattern,
            )

            # 尝试从缓存获取结果
            cached_result = await self._get_from_cache(cache_key)
            if cached_result:
                self.logger.info("磁盘预测缓存命中，直接返回结果")
                return cached_result

            # 获取历史数据
            historical_data = await self._fetch_historical_data(
                PredictionType.DISK, metric_query, hours=48
            )

            # 执行预测
            predictions = await self._predictor.predict(
                prediction_type=PredictionType.DISK,
                current_value=current_disk_percent,
                historical_data=historical_data,
                prediction_hours=prediction_hours,
                granularity=PredictionGranularity(granularity),
                consider_pattern=consider_historical_pattern,
            )

            # 生成扩缩容建议
            scaling_recommendations = (
                await self._scaling_advisor.generate_recommendations(
                    predictions=predictions,
                    prediction_type=PredictionType.DISK,
                    target_utilization=target_utilization,
                    constraints=(
                        ResourceConstraints(**resource_constraints)
                        if resource_constraints
                        else None
                    ),
                )
            )

            # 异常检测
            anomaly_predictions = []
            if include_anomaly_detection:
                anomaly_predictions = await self._anomaly_detector.detect_anomalies(
                    predictions=predictions, sensitivity=sensitivity
                )

            # 成本分析
            cost_analysis = None
            if resource_constraints:
                cost_analysis = await self._cost_analyzer.analyze_cost(
                    predictions=predictions,
                    scaling_recommendations=scaling_recommendations,
                    constraints=ResourceConstraints(**resource_constraints),
                )

            # 构建响应
            result = self._build_prediction_response(
                prediction_type=PredictionType.DISK,
                current_value=current_disk_percent,
                predictions=predictions,
                scaling_recommendations=scaling_recommendations,
                anomaly_predictions=anomaly_predictions,
                cost_analysis=cost_analysis,
                prediction_hours=prediction_hours,
                granularity=granularity,
                include_confidence=include_confidence,
            )

            # 保存到缓存
            await self._save_to_cache(cache_key, result, ttl=3600)  # 1小时缓存

            return result

        except (ValidationError, HistoricalDataError):
            raise
        except Exception as e:
            self.logger.error(f"磁盘预测失败: {str(e)}")
            raise PredictionError(f"磁盘预测失败: {str(e)}")

    # 增强预测方法

    async def predict_with_ai_analysis(
        self,
        prediction_type: str,
        current_value: float,
        metric_query: Optional[str] = None,
        prediction_hours: int = 24,
        granularity: str = "hour",
        resource_constraints: Optional[Dict] = None,
        enable_ai_insights: bool = True,
        report_style: str = "professional",
        target_utilization: float = 0.7,
        sensitivity: float = 0.8,
    ) -> Dict[str, Any]:
        """增强预测分析"""
        try:
            self._ensure_initialized()

            # 验证参数
            pred_type = PredictionType(prediction_type)
            if pred_type == PredictionType.QPS:
                self._validate_qps_params(current_value, prediction_hours)
            else:
                self._validate_utilization_params(current_value, prediction_hours)

            # 生成缓存键（增强预测使用更长缓存过期时间）
            cache_key = self._generate_prediction_cache_key(
                prediction_type=prediction_type,
                current_value=current_value,
                metric_query=metric_query,
                prediction_hours=prediction_hours,
                granularity=granularity,
                resource_constraints=resource_constraints,
                ai_enhanced=True,
                enable_ai_insights=enable_ai_insights,
                report_style=report_style,
                target_utilization=target_utilization,
                sensitivity=sensitivity,
            )

            # 尝试从缓存获取结果
            cached_result = await self._get_from_cache(cache_key)
            if cached_result:
                self.logger.info("AI增强预测缓存命中，直接返回结果")
                return cached_result

            # 检查增强组件是否可用
            if not self._intelligent_predictor:
                self.logger.warning("增强组件不可用，降级到基础预测")
                return await self._fallback_to_basic_prediction(
                    pred_type,
                    current_value,
                    metric_query,
                    prediction_hours,
                    granularity,
                    resource_constraints,
                    target_utilization,
                    sensitivity,
                )

            # 获取历史数据
            historical_data = await self._fetch_historical_data(
                pred_type, metric_query, hours=48
            )

            # 执行增强预测
            ai_prediction_result = (
                await self._intelligent_predictor.predict_with_ai_analysis(
                    prediction_type=pred_type,
                    current_value=current_value,
                    historical_data=historical_data,
                    prediction_hours=prediction_hours,
                    granularity=PredictionGranularity(granularity),
                    consider_pattern=True,
                    enable_ai_insights=enable_ai_insights,
                    report_style=report_style,
                )
            )

            # 增强结果（添加扩缩容建议和成本分析）
            enhanced_result = await self._enhance_ai_prediction_result(
                ai_prediction_result, resource_constraints, target_utilization
            )

            # 保存到缓存（增强预测缓存2小时）
            await self._save_to_cache(cache_key, enhanced_result, ttl=7200)

            return enhanced_result

        except (ValidationError, HistoricalDataError):
            raise
        except Exception as e:
            self.logger.error(f"增强预测失败: {str(e)}")
            # 降级到基础预测
            return await self._fallback_to_basic_prediction(
                PredictionType(prediction_type),
                current_value,
                metric_query,
                prediction_hours,
                granularity,
                resource_constraints,
                target_utilization,
                sensitivity,
            )

    async def predict_multi_dimension_with_correlation(
        self,
        prediction_configs: List[Dict[str, Any]],
        enable_correlation_analysis: bool = True,
        report_style: str = "professional",
    ) -> Dict[str, Any]:
        """多维度预测与关联分析"""
        try:
            self._ensure_initialized()

            if not self._intelligent_predictor:
                raise PredictionError("AI增强组件不可用，无法执行多维度关联分析")

            # 验证配置
            validated_configs = []
            for config in prediction_configs:
                pred_type = PredictionType(config["prediction_type"])
                current_value = config["current_value"]

                # 验证参数
                if pred_type == PredictionType.QPS:
                    self._validate_qps_params(
                        current_value, config.get("prediction_hours", 24)
                    )
                else:
                    self._validate_utilization_params(
                        current_value, config.get("prediction_hours", 24)
                    )

                # 获取历史数据
                historical_data = await self._fetch_historical_data(
                    pred_type, config.get("metric_query"), hours=48
                )
                config["historical_data"] = historical_data
                validated_configs.append(config)

            # 执行多维度预测
            result = await self._intelligent_predictor.predict_multi_dimension_with_correlation(
                prediction_configs=validated_configs,
                enable_correlation_analysis=enable_correlation_analysis,
                report_style=report_style,
            )

            return result

        except (ValidationError, HistoricalDataError):
            raise
        except Exception as e:
            self.logger.error(f"多维度预测失败: {str(e)}")
            raise PredictionError(f"多维度预测失败: {str(e)}")

    async def generate_prediction_report(
        self,
        analysis_id: str,
        report_type: str = "comprehensive",  # comprehensive, executive, action_plan, risk_assessment
        report_style: str = "professional",
    ) -> Dict[str, Any]:
        """基于分析ID生成定制化报告"""
        try:
            self._ensure_initialized()

            if not self._report_generator:
                raise PredictionError("报告生成器不可用")

            # 从缓存获取分析结果（这里简化处理）
            # 实际应用中应该有完整的分析结果缓存机制

            return {
                "analysis_id": analysis_id,
                "report_type": report_type,
                "report_style": report_style,
                "status": "report_generation_not_implemented_for_cache_lookup",
                "message": "报告生成功能需要完整的分析上下文，请使用预测方法中的集成报告功能",
            }

        except Exception as e:
            self.logger.error(f"生成预测报告失败: {str(e)}")
            raise PredictionError(f"生成预测报告失败: {str(e)}")

    async def _enhance_ai_prediction_result(
        self,
        ai_result: Dict[str, Any],
        resource_constraints: Optional[Dict] = None,
        target_utilization: float = 0.7,
    ) -> Dict[str, Any]:
        """增强AI预测结果 - 添加扩缩容建议和成本分析"""
        try:
            # 获取预测数据
            predictions = ai_result.get("predicted_data", [])
            if not predictions:
                return ai_result

            # 转换为PredictionDataPoint对象
            prediction_points = [
                PredictionDataPoint(**pred) if isinstance(pred, dict) else pred
                for pred in predictions
            ]

            # 生成扩缩容建议
            scaling_recommendations = []
            cost_analysis = None

            if self._scaling_advisor:
                try:
                    scaling_recommendations = (
                        await self._scaling_advisor.generate_recommendations(
                            predictions=prediction_points,
                            prediction_type=PredictionType(
                                ai_result.get("prediction_type")
                            ),
                            target_utilization=target_utilization,
                            constraints=(
                                ResourceConstraints(**resource_constraints)
                                if resource_constraints
                                else None
                            ),
                        )
                    )
                except Exception as e:
                    self.logger.warning(f"生成扩缩容建议失败: {str(e)}")

            # 成本分析
            if self._cost_analyzer and resource_constraints:
                try:
                    cost_analysis = await self._cost_analyzer.analyze_cost(
                        predictions=prediction_points,
                        scaling_recommendations=scaling_recommendations,
                        constraints=ResourceConstraints(**resource_constraints),
                    )
                except Exception as e:
                    self.logger.warning(f"成本分析失败: {str(e)}")

            # 更新结果
            ai_result.update(
                {
                    "scaling_recommendations": [
                        r.model_dump() for r in scaling_recommendations
                    ],
                    "cost_analysis": (
                        cost_analysis.model_dump() if cost_analysis else None
                    ),
                    "enhancement_timestamp": datetime.now(),
                }
            )

            return ai_result

        except Exception as e:
            self.logger.error(f"增强AI预测结果失败: {str(e)}")
            return ai_result

    async def _fallback_to_basic_prediction(
        self,
        prediction_type: PredictionType,
        current_value: float,
        metric_query: Optional[str],
        prediction_hours: int,
        granularity: str,
        resource_constraints: Optional[Dict],
        target_utilization: float,
        sensitivity: float,
    ) -> Dict[str, Any]:
        """降级到基础预测"""

        self.logger.info("执行基础预测作为AI增强预测的降级方案")

        # 根据预测类型调用相应的基础预测方法
        if prediction_type == PredictionType.QPS:
            result = await self.predict_qps(
                current_qps=current_value,
                metric_query=metric_query,
                prediction_hours=prediction_hours,
                granularity=granularity,
                resource_constraints=resource_constraints,
                target_utilization=target_utilization,
                sensitivity=sensitivity,
            )
        elif prediction_type == PredictionType.CPU:
            result = await self.predict_cpu_utilization(
                current_cpu_percent=current_value,
                metric_query=metric_query,
                prediction_hours=prediction_hours,
                granularity=granularity,
                resource_constraints=resource_constraints,
                target_utilization=target_utilization,
                sensitivity=sensitivity,
            )
        elif prediction_type == PredictionType.MEMORY:
            result = await self.predict_memory_utilization(
                current_memory_percent=current_value,
                metric_query=metric_query,
                prediction_hours=prediction_hours,
                granularity=granularity,
                resource_constraints=resource_constraints,
                target_utilization=target_utilization,
                sensitivity=sensitivity,
            )
        else:  # DISK
            result = await self.predict_disk_utilization(
                current_disk_percent=current_value,
                metric_query=metric_query,
                prediction_hours=prediction_hours,
                granularity=granularity,
                resource_constraints=resource_constraints,
                target_utilization=target_utilization,
                sensitivity=sensitivity,
            )

        # 添加降级标记
        result.update(
            {
                "ai_enhanced": False,
                "fallback_mode": True,
                "fallback_reason": "AI增强组件不可用",
                "ai_insights": [
                    "当前使用基础预测模式",
                    "AI增强功能暂时不可用",
                    "预测结果基于传统算法生成",
                ],
            }
        )

        return result

    async def get_service_health_info(self) -> Dict[str, Any]:
        """获取服务健康信息"""
        try:
            models_info = (
                await self._model_manager.get_models_info()
                if self._model_manager
                else []
            )

            return {
                "service_status": (
                    "healthy" if await self.health_check() else "unhealthy"
                ),
                "model_status": (
                    "loaded"
                    if self._model_manager and self._model_manager.models_loaded
                    else "not_loaded"
                ),
                "models_loaded": models_info,
                "supported_prediction_types": [t.value for t in PredictionType],
                "last_prediction_time": getattr(self, "_last_prediction_time", None),
                "total_predictions": getattr(self, "_total_predictions", 0),
                "error_rate": 0.0,
                "average_response_time_ms": getattr(self, "_avg_response_time", None),
                "resource_usage": await self._get_resource_usage(),
                "timestamp": datetime.now(),
            }

        except Exception as e:
            self.logger.error(f"获取健康信息失败: {str(e)}")
            return {
                "service_status": "error",
                "model_status": "unknown",
                "error": str(e),
                "timestamp": datetime.now(),
            }

    async def get_model_info(self) -> Dict[str, Any]:
        """获取模型信息"""
        try:
            if not self._model_manager:
                return {"models": [], "status": "not_initialized"}

            return await self._model_manager.get_detailed_info()

        except Exception as e:
            self.logger.error(f"获取模型信息失败: {str(e)}")
            return {"models": [], "status": "error", "error_message": str(e)}

    # 私有辅助方法

    def _validate_qps_params(self, qps: float, hours: int) -> None:
        """验证QPS预测参数"""
        if qps < 0 or qps > 100000:
            raise ValidationError("current_qps", "QPS值应在0-100000之间")
        self._validate_hours(hours)

    def _validate_utilization_params(self, percent: float, hours: int) -> None:
        """验证利用率预测参数"""
        if percent < 0 or percent > 100:
            raise ValidationError("utilization", "利用率应在0-100%之间")
        self._validate_hours(hours)

    def _validate_hours(self, hours: int) -> None:
        """验证预测时长"""
        from app.config.settings import config

        min_hours = config.prediction.min_prediction_hours
        max_hours = config.prediction.max_prediction_hours

        if hours < min_hours or hours > max_hours:
            raise ValidationError(
                "prediction_hours", f"预测时长应在{min_hours}-{max_hours}小时之间"
            )

    async def _fetch_historical_data(
        self, prediction_type: PredictionType, metric_query: Optional[str], hours: int
    ) -> List[Dict[str, Any]]:
        """获取历史数据"""

        try:
            prometheus_data = await self._get_prometheus_data(
                prediction_type, metric_query, hours
            )
        except PredictionError:
            raise
        except Exception as e:
            raise PredictionError("Historical metrics are unavailable; prediction was not performed") from e

        if not prometheus_data:
            raise HistoricalDataError("No historical metrics were returned; prediction was not performed")
        if len(prometheus_data) < 2:
            raise HistoricalDataError("Insufficient historical metrics: at least two observations are required; prediction was not performed")

        # 特征提取按小时位置取历史值，不能将缺失小时压缩成连续样本
        try:
            timestamps = [datetime.fromisoformat(str(point["timestamp"]).replace("Z", "+00:00")) for point in prometheus_data]
            valid_values = all(math.isfinite(float(point["value"])) for point in prometheus_data)
            hourly = all(right - left == timedelta(hours=1) for left, right in zip(timestamps, timestamps[1:]))
        except (KeyError, ValueError, TypeError, OverflowError) as e:
            raise HistoricalDataError("Historical metrics contain invalid samples; prediction was not performed") from e
        if not valid_values or not hourly:
            raise HistoricalDataError("Historical metrics require finite, consecutive hourly observations; prediction was not performed")
        self.logger.info(f"成功从Prometheus获取{len(prometheus_data)}条历史数据")
        return prometheus_data

    async def _get_prometheus_data(
        self, prediction_type: PredictionType, custom_query: Optional[str], hours: int
    ) -> Optional[List[Dict[str, Any]]]:
        """从Prometheus获取真实数据"""
        from app.services.prometheus import PrometheusService

        prom_service = PrometheusService()

        # 检查Prometheus连接
        if not await prom_service.health_check():
            raise PredictionError("Prometheus upstream is unavailable; prediction was not performed")

        # 确定查询语句
        query = custom_query or self._get_default_query(prediction_type)
        if not query:
            return None

        # 计算时间范围
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=hours)

        # 查询数据
        df = await prom_service.query_range(
            query=query,
            start_time=start_time,
            end_time=end_time,
            step="1h",  # 使用小时级步长
        )

        if df is None or df.empty:
            self.logger.warning(f"无法从Prometheus获取{prediction_type.value}数据")
            return None

        # 转换为预测服务所需的格式
        return self._convert_prometheus_data(df)

    def _get_default_query(self, prediction_type: PredictionType) -> Optional[str]:
        """获取默认的Prometheus查询语句 - 使用node_exporter标准指标"""
        try:
            # QPS 默认查询优先使用配置文件中的 prediction.prometheus_query
            if prediction_type == PredictionType.QPS:
                from app.config.settings import config as _config

                query = _config.prediction.prometheus_query
                return query

            # 其他资源类型沿用内置默认查询
            default_queries = {
                PredictionType.CPU: (
                    '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
                ),
                PredictionType.MEMORY: (
                    "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100"
                ),
                PredictionType.DISK: (
                    '(1 - (node_filesystem_avail_bytes{fstype!="tmpfs"} / node_filesystem_size_bytes{fstype!="tmpfs"})) * 100'
                ),
            }
            return default_queries.get(prediction_type)
        except Exception:
            return None

    def _convert_prometheus_data(self, df) -> List[Dict[str, Any]]:
        """转换Prometheus数据格式"""
        if "value" not in df.columns:
            return []
        if df.index.has_duplicates:
            raise HistoricalDataError("Multiple metric series require an explicit aggregate in PromQL; prediction was not performed")

        data = []
        for timestamp, row in df.sort_index().iterrows():
            try:
                value = float(row["value"])
            except (ValueError, TypeError, OverflowError):
                continue
            if not math.isfinite(value):
                continue
            data.append({"timestamp": timestamp.isoformat(), "value": value})
        return data

    def _build_prediction_response(
        self,
        prediction_type: PredictionType,
        current_value: float,
        predictions: List[PredictionDataPoint],
        scaling_recommendations: List[ScalingRecommendation],
        anomaly_predictions: List[AnomalyPrediction],
        cost_analysis: Optional[CostAnalysis],
        prediction_hours: int,
        granularity: str,
        include_confidence: bool,
    ) -> Dict[str, Any]:
        """构建预测响应"""
        # 生成资源利用率预测
        resource_utilization = self._generate_resource_utilization(
            predictions, prediction_type
        )

        # 生成模式分析
        pattern_analysis = self._analyze_patterns(predictions)

        # 生成趋势洞察
        trend_insights = self._generate_insights(
            predictions, scaling_recommendations, anomaly_predictions
        )

        # 生成预测摘要
        prediction_summary = {
            "max_value": max(p.predicted_value for p in predictions),
            "min_value": min(p.predicted_value for p in predictions),
            "avg_value": sum(p.predicted_value for p in predictions) / len(predictions),
            "trend": self._detect_trend(predictions),
            "peak_time": self._find_peak_time(predictions),
        }

        return {
            "prediction_type": prediction_type,
            "prediction_hours": prediction_hours,
            "granularity": granularity,
            "current_value": current_value,
            "predicted_data": [p.model_dump() for p in predictions],
            "resource_utilization": [r.model_dump() for r in resource_utilization],
            "scaling_recommendations": [
                s.model_dump() for s in scaling_recommendations
            ],
            "anomaly_predictions": [a.model_dump() for a in anomaly_predictions],
            "cost_analysis": cost_analysis.model_dump() if cost_analysis else None,
            "pattern_analysis": pattern_analysis,
            "trend_insights": trend_insights,
            "model_accuracy": 0.85,  # 这里应该从模型获取实际准确率
            "prediction_summary": prediction_summary,
            "timestamp": datetime.now(),
        }

    def _generate_resource_utilization(
        self, predictions: List[PredictionDataPoint], prediction_type: PredictionType
    ) -> List[ResourceUtilization]:
        """生成资源利用率预测"""
        utilizations = []
        for pred in predictions:
            util = ResourceUtilization(
                timestamp=pred.timestamp, predicted_load=pred.predicted_value
            )

            if prediction_type == PredictionType.CPU:
                util.cpu_utilization = pred.predicted_value
            elif prediction_type == PredictionType.MEMORY:
                util.memory_utilization = pred.predicted_value
            elif prediction_type == PredictionType.DISK:
                util.disk_utilization = pred.predicted_value

            utilizations.append(util)
        return utilizations

    def _analyze_patterns(
        self, predictions: List[PredictionDataPoint]
    ) -> Dict[str, Any]:
        """分析预测模式"""
        values = [p.predicted_value for p in predictions]
        return {
            "has_periodicity": self._check_periodicity(values),
            "volatility": self._calculate_volatility(values),
            "seasonality": self._detect_seasonality(predictions),
        }

    def _generate_insights(
        self,
        predictions: List[PredictionDataPoint],
        scaling_recommendations: List[ScalingRecommendation],
        anomaly_predictions: List[AnomalyPrediction],
    ) -> List[str]:
        """生成趋势洞察"""
        insights = []

        # 趋势洞察
        trend = self._detect_trend(predictions)
        if trend == "increasing":
            insights.append("资源使用呈上升趋势，建议提前规划扩容")
        elif trend == "decreasing":
            insights.append("资源使用呈下降趋势，可考虑优化成本")

        # 扩缩容洞察
        if scaling_recommendations:
            scale_up_count = sum(
                1 for r in scaling_recommendations if r.action.value == "scale_up"
            )
            if scale_up_count > 0:
                insights.append(
                    f"未来{len(predictions)}个时间点中有{scale_up_count}个需要扩容"
                )

        # 异常洞察
        if anomaly_predictions:
            high_risk = [
                a
                for a in anomaly_predictions
                if a.impact_level == "high" or a.impact_level == "critical"
            ]
            if high_risk:
                insights.append(f"检测到{len(high_risk)}个高风险异常点，需要重点关注")

        return insights

    def _detect_trend(self, predictions: List[PredictionDataPoint]) -> str:
        """检测趋势"""
        if len(predictions) < 2:
            return "stable"

        values = [p.predicted_value for p in predictions]
        first_half_avg = sum(values[: len(values) // 2]) / (len(values) // 2)
        second_half_avg = sum(values[len(values) // 2 :]) / (
            len(values) - len(values) // 2
        )

        # 零起点只判断方向，不构造无定义的相对百分比
        if first_half_avg == 0:
            if second_half_avg > 0:
                return "increasing"
            if second_half_avg < 0:
                return "decreasing"
            return "stable"

        diff_percent = (second_half_avg - first_half_avg) / first_half_avg * 100

        if diff_percent > 10:
            return "increasing"
        elif diff_percent < -10:
            return "decreasing"
        else:
            return "stable"

    def _find_peak_time(
        self, predictions: List[PredictionDataPoint]
    ) -> Optional[datetime]:
        """找出峰值时间"""
        if not predictions:
            return None
        max_pred = max(predictions, key=lambda p: p.predicted_value)
        return max_pred.timestamp

    def _check_periodicity(self, values: List[float]) -> bool:
        """检查周期性"""
        # 简单的周期性检测逻辑
        if len(values) < 24:
            return False
        # 这里可以实现更复杂的周期性检测算法
        return True

    def _calculate_volatility(self, values: List[float]) -> float:
        """计算波动性"""
        if len(values) < 2:
            return 0.0
        import numpy as np

        return float(np.std(values))

    def _detect_seasonality(
        self, predictions: List[PredictionDataPoint]
    ) -> Dict[str, Any]:
        """检测季节性"""
        return {
            "daily_pattern": True,
            "weekly_pattern": False,
            "monthly_pattern": False,
        }

    async def _get_resource_usage(self) -> Dict[str, float]:
        """获取资源使用情况"""
        # 这里应该获取实际的资源使用情况
        return {"cpu_percent": 15.0, "memory_percent": 25.0, "disk_percent": 10.0}

    def _generate_prediction_cache_key(
        self,
        prediction_type: str,
        current_value: float,
        metric_query: Optional[str] = None,
        prediction_hours: int = 24,
        granularity: str = "hour",
        resource_constraints: Optional[Dict] = None,
        ai_enhanced: bool = False,
        **kwargs,
    ) -> str:
        """生成预测缓存键"""
        try:
            # 构建缓存输入参数
            cache_params = {
                "type": prediction_type,
                "data_contract": "observed-hourly-v1",
                "time_contract": "utc-v1",
                "current": round(current_value, 2),  # 避免浮点数精度问题
                "query": metric_query or "default",
                "hours": prediction_hours,
                "granularity": granularity,
                "ai": ai_enhanced,
            }

            # 添加资源约束（如果有）
            if resource_constraints:
                # 对约束参数进行排序确保一致性
                sorted_constraints = dict(sorted(resource_constraints.items()))
                cache_params["constraints"] = str(sorted_constraints)

            # 添加其他重要参数
            for key, value in kwargs.items():
                if key in ["target_utilization", "sensitivity", "report_style"]:
                    cache_params[key] = value

            # 生成缓存键字符串
            cache_input = "|".join(
                [f"{k}:{v}" for k, v in sorted(cache_params.items())]
            )

            # 生成哈希
            cache_hash = hashlib.sha256(cache_input.encode("utf-8")).hexdigest()[:16]

            return f"prediction:{prediction_type}:{cache_hash}"

        except Exception as e:
            self.logger.error(f"生成缓存键失败: {str(e)}")
            # 降级到简单键
            return (
                f"prediction:{prediction_type}:utc-v1:{int(current_value)}:{prediction_hours}"
            )

    async def _get_from_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """从缓存获取预测结果"""
        if not self._cache_manager:
            return None

        try:
            cached_result = self._cache_manager.get(cache_key)
            if cached_result:
                self.logger.debug(f"缓存命中: {cache_key}")
                # 添加缓存标识
                cached_result["from_cache"] = True
                cached_result["cache_timestamp"] = datetime.now()
                return cached_result
        except Exception as e:
            self.logger.warning(f"从缓存获取数据失败: {str(e)}")

        return None

    async def _save_to_cache(
        self, cache_key: str, result: Dict[str, Any], ttl: int = 3600
    ) -> None:
        """保存预测结果到缓存"""
        if not self._cache_manager:
            return

        try:
            # 移除不需要缓存的临时数据
            cache_result = result.copy()
            cache_result.pop("from_cache", None)
            cache_result.pop("cache_timestamp", None)

            # 添加缓存元数据
            cache_result["cached_at"] = datetime.now().isoformat()
            cache_result["cache_ttl"] = ttl

            self._cache_manager.set(
                question=cache_key, response_data=cache_result, ttl=ttl
            )
            self.logger.debug(f"结果已缓存: {cache_key}")
        except Exception as e:
            self.logger.warning(f"保存到缓存失败: {str(e)}")

    def is_initialized(self) -> bool:
        """检查服务是否已初始化"""
        return self._initialized

    async def cleanup(self) -> None:
        """清理资源"""
        try:
            self._initialized = False

            # 清理缓存管理器
            if self._cache_manager:
                try:
                    self._cache_manager.shutdown()
                except Exception as e:
                    self.logger.warning(f"关闭缓存管理器失败: {str(e)}")
                self._cache_manager = None

            self._predictor = None
            self._feature_extractor = None
            self._anomaly_detector = None
            self._scaling_advisor = None
            self._cost_analyzer = None
            self._model_manager = None
            self.logger.info("预测服务资源已清理")
        except Exception as e:
            self.logger.error(f"清理资源失败: {str(e)}")
