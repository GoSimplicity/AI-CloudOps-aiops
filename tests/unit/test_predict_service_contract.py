#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 预测服务单元测试
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.common.exceptions import PredictionError, ValidationError
from app.models.predict_models import PredictionDataPoint, PredictionType
from app.services.prediction_service import PredictionService


@pytest.fixture(autouse=True)
def unavailable_cache(monkeypatch):
    """初始化路径显式模拟不可用缓存，不隐式连接开发机 Redis"""
    from app.core.cache import redis_cache_manager

    monkeypatch.setattr(redis_cache_manager, "RedisCacheManager", Mock(side_effect=RuntimeError("offline cache unavailable")))


@pytest.fixture
def prediction_service():
    """创建预测服务实例"""
    service = PredictionService()
    service._get_prometheus_data = AsyncMock(return_value=[
        {"timestamp": "2026-10-22T10:00:00", "value": 100.0},
        {"timestamp": "2026-10-22T11:00:00", "value": 105.0},
    ])
    return service


@pytest.fixture
def mock_model_manager():
    """创建模拟模型管理器"""
    mock_manager = AsyncMock()
    mock_manager.models_loaded = True
    mock_manager.get_model.return_value = Mock()
    mock_manager.get_scaler.return_value = Mock()
    mock_manager.is_healthy.return_value = True
    mock_manager.get_models_info.return_value = []
    return mock_manager


@pytest.fixture
def mock_predictor():
    """创建模拟预测器"""
    mock_predictor = AsyncMock()
    mock_predictor.predict.return_value = [
        PredictionDataPoint(
            timestamp=datetime.now() + timedelta(hours=1),
            predicted_value=150.0,
            confidence_level=0.85,
        ),
        PredictionDataPoint(
            timestamp=datetime.now() + timedelta(hours=2),
            predicted_value=160.0,
            confidence_level=0.83,
        ),
    ]
    mock_predictor.is_healthy.return_value = True
    return mock_predictor


@pytest.fixture
def sample_resource_constraints():
    """创建示例资源约束"""
    return {
        "cpu_cores": 4.0,
        "memory_gb": 16.0,
        "max_instances": 10,
        "min_instances": 2,
        "cost_per_hour": 0.5,
    }


class TestPredictionService:
    """预测服务测试类"""

    @pytest.mark.asyncio
    async def test_service_initialization(self, prediction_service):
        """测试服务初始化"""
        assert not prediction_service._initialized
        assert prediction_service._predictor is None
        assert prediction_service._model_manager is None

        # 测试初始化状态检查
        assert not prediction_service.is_initialized()

    @pytest.mark.asyncio
    async def test_qps_prediction_success(
        self, prediction_service, mock_model_manager, mock_predictor
    ):
        """测试QPS预测成功案例"""
        # Mock 依赖组件
        prediction_service._model_manager = mock_model_manager
        prediction_service._predictor = mock_predictor
        prediction_service._feature_extractor = AsyncMock()
        prediction_service._anomaly_detector = AsyncMock()
        prediction_service._scaling_advisor = AsyncMock()
        prediction_service._cost_analyzer = AsyncMock()
        prediction_service._initialized = True

        # Mock返回值
        prediction_service._anomaly_detector.detect_anomalies.return_value = []
        prediction_service._scaling_advisor.generate_recommendations.return_value = []
        prediction_service._cost_analyzer.analyze_cost.return_value = None

        # 执行预测
        result = await prediction_service.predict_qps(
            current_qps=100.0, prediction_hours=12, include_confidence=True
        )

        # 验证结果
        assert result is not None
        assert result["prediction_type"] == PredictionType.QPS
        assert result["current_value"] == 100.0
        assert result["prediction_hours"] == 12
        assert "predicted_data" in result
        assert len(result["predicted_data"]) == 2

        # 验证调用
        mock_predictor.predict.assert_called_once()
        prediction_service._anomaly_detector.detect_anomalies.assert_called_once()
        prediction_service._scaling_advisor.generate_recommendations.assert_called_once()

    @pytest.mark.asyncio
    async def test_cpu_prediction_with_constraints(
        self,
        prediction_service,
        mock_model_manager,
        mock_predictor,
        sample_resource_constraints,
    ):
        """测试带资源约束的CPU预测"""
        # Mock 依赖组件
        prediction_service._model_manager = mock_model_manager
        prediction_service._predictor = mock_predictor
        prediction_service._feature_extractor = AsyncMock()
        prediction_service._anomaly_detector = AsyncMock()
        prediction_service._scaling_advisor = AsyncMock()
        prediction_service._cost_analyzer = AsyncMock()
        prediction_service._initialized = True

        # Mock返回值
        prediction_service._anomaly_detector.detect_anomalies.return_value = []
        prediction_service._scaling_advisor.generate_recommendations.return_value = []

        # Mock成本分析返回值
        from app.models.predict_models import CostAnalysis

        mock_cost_analysis = CostAnalysis(
            current_hourly_cost=1.0,
            predicted_hourly_cost=1.2,
            cost_savings_potential=-5.0,
        )
        prediction_service._cost_analyzer.analyze_cost.return_value = mock_cost_analysis

        # 执行预测
        result = await prediction_service.predict_cpu_utilization(
            current_cpu_percent=75.5,
            prediction_hours=24,
            resource_constraints=sample_resource_constraints,
            target_utilization=0.65,
        )

        # 验证结果
        assert result is not None
        assert result["prediction_type"] == PredictionType.CPU
        assert result["current_value"] == 75.5
        assert "cost_analysis" in result
        assert result["cost_analysis"] is not None

        # 验证成本分析被调用
        prediction_service._cost_analyzer.analyze_cost.assert_called_once()

    @pytest.mark.asyncio
    async def test_memory_prediction_validation(self, prediction_service):
        """测试内存预测参数验证"""
        prediction_service._initialized = True

        # 测试无效的内存百分比
        with pytest.raises(ValidationError) as exc_info:
            await prediction_service.predict_memory_utilization(
                current_memory_percent=150.0  # 无效值
            )
        assert "利用率应在0-100%之间" in str(exc_info.value)

        # 测试无效的预测时长
        with pytest.raises(ValidationError) as exc_info:
            await prediction_service.predict_memory_utilization(
                current_memory_percent=80.0,
                prediction_hours=0,  # 无效值
            )
        assert "预测时长应在" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_disk_prediction_granularity(
        self, prediction_service, mock_model_manager, mock_predictor
    ):
        """测试磁盘预测不同时间粒度"""
        # Mock 依赖组件
        prediction_service._model_manager = mock_model_manager
        prediction_service._predictor = mock_predictor
        prediction_service._feature_extractor = AsyncMock()
        prediction_service._anomaly_detector = AsyncMock()
        prediction_service._scaling_advisor = AsyncMock()
        prediction_service._cost_analyzer = AsyncMock()
        prediction_service._initialized = True

        # Mock返回值
        prediction_service._anomaly_detector.detect_anomalies.return_value = []
        prediction_service._scaling_advisor.generate_recommendations.return_value = []

        # 测试不同粒度
        granularities = ["minute", "hour", "day"]

        for granularity in granularities:
            result = await prediction_service.predict_disk_utilization(
                current_disk_percent=85.0, prediction_hours=24, granularity=granularity
            )

            assert result["granularity"] == granularity
            assert result["prediction_type"] == PredictionType.DISK

    @pytest.mark.asyncio
    async def test_service_health_check(self, prediction_service, mock_model_manager):
        """测试服务健康检查"""
        prediction_service._model_manager = mock_model_manager
        prediction_service._predictor = AsyncMock()
        prediction_service._predictor.is_healthy.return_value = True
        prediction_service._initialized = True

        # 健康状态
        is_healthy = await prediction_service.health_check()
        assert is_healthy

        # 获取健康信息
        health_info = await prediction_service.get_service_health_info()
        assert health_info is not None
        assert "service_status" in health_info
        assert "model_status" in health_info
        assert "supported_prediction_types" in health_info

    @pytest.mark.asyncio
    async def test_uninitialized_service_error(self, prediction_service):
        """测试未初始化服务的错误处理"""
        # 服务未初始化时应该抛出异常
        with pytest.raises(PredictionError) as exc_info:
            await prediction_service.predict_qps(current_qps=100.0)
        assert "服务未初始化" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_anomaly_detection_integration(
        self, prediction_service, mock_model_manager, mock_predictor
    ):
        """测试异常检测集成"""
        # Mock 依赖组件
        prediction_service._model_manager = mock_model_manager
        prediction_service._predictor = mock_predictor
        prediction_service._feature_extractor = AsyncMock()
        prediction_service._scaling_advisor = AsyncMock()
        prediction_service._cost_analyzer = AsyncMock()
        prediction_service._initialized = True

        # Mock异常检测器返回异常
        from app.models.predict_models import AnomalyPrediction

        mock_anomaly = AnomalyPrediction(
            timestamp=datetime.now(),
            anomaly_score=0.95,
            anomaly_type="spike",
            impact_level="high",
            predicted_value=200.0,
            expected_value=150.0,
        )

        prediction_service._anomaly_detector = AsyncMock()
        prediction_service._anomaly_detector.detect_anomalies.return_value = [
            mock_anomaly
        ]
        prediction_service._scaling_advisor.generate_recommendations.return_value = []

        # 执行预测
        result = await prediction_service.predict_qps(
            current_qps=100.0, include_anomaly_detection=True, sensitivity=0.9
        )

        # 验证异常检测结果
        assert "anomaly_predictions" in result
        assert len(result["anomaly_predictions"]) == 1
        anomaly = result["anomaly_predictions"][0]
        assert anomaly["anomaly_score"] == 0.95
        assert anomaly["impact_level"] == "high"

    @pytest.mark.asyncio
    async def test_scaling_recommendations_integration(
        self, prediction_service, mock_model_manager, mock_predictor
    ):
        """测试扩缩容建议集成"""
        # Mock 依赖组件
        prediction_service._model_manager = mock_model_manager
        prediction_service._predictor = mock_predictor
        prediction_service._feature_extractor = AsyncMock()
        prediction_service._anomaly_detector = AsyncMock()
        prediction_service._cost_analyzer = AsyncMock()
        prediction_service._initialized = True

        # Mock扩缩容建议
        from app.models.predict_models import ScalingAction, ScalingRecommendation

        mock_recommendation = ScalingRecommendation(
            action=ScalingAction.SCALE_UP,
            trigger_time=datetime.now() + timedelta(hours=2),
            confidence=0.88,
            reason="QPS预测值(200)超过阈值，建议扩容至4个实例",
            target_instances=4,
        )

        prediction_service._scaling_advisor = AsyncMock()
        prediction_service._scaling_advisor.generate_recommendations.return_value = [
            mock_recommendation
        ]
        prediction_service._anomaly_detector.detect_anomalies.return_value = []

        # 执行预测
        result = await prediction_service.predict_qps(
            current_qps=150.0, target_utilization=0.6
        )

        # 验证扩缩容建议
        assert "scaling_recommendations" in result
        assert len(result["scaling_recommendations"]) == 1
        recommendation = result["scaling_recommendations"][0]
        assert recommendation["action"] == "scale_up"
        assert recommendation["target_instances"] == 4
        assert recommendation["confidence"] == 0.88

    @pytest.mark.asyncio
    async def test_pattern_analysis(
        self, prediction_service, mock_model_manager, mock_predictor
    ):
        """测试模式分析功能"""
        # Mock 依赖组件
        prediction_service._model_manager = mock_model_manager
        prediction_service._predictor = mock_predictor
        prediction_service._feature_extractor = AsyncMock()
        prediction_service._anomaly_detector = AsyncMock()
        prediction_service._scaling_advisor = AsyncMock()
        prediction_service._cost_analyzer = AsyncMock()
        prediction_service._initialized = True

        # Mock返回多个预测点以便分析模式
        predictions = []
        for i in range(24):  # 24小时的预测数据
            predictions.append(
                PredictionDataPoint(
                    timestamp=datetime.now() + timedelta(hours=i + 1),
                    predicted_value=100.0 + i * 2.5,  # 递增模式
                    confidence_level=0.8,
                )
            )

        mock_predictor.predict.return_value = predictions
        prediction_service._anomaly_detector.detect_anomalies.return_value = []
        prediction_service._scaling_advisor.generate_recommendations.return_value = []

        # 执行预测
        result = await prediction_service.predict_qps(
            current_qps=100.0, prediction_hours=24, consider_historical_pattern=True
        )

        # 验证模式分析
        assert "pattern_analysis" in result
        assert "trend_insights" in result
        assert "prediction_summary" in result

        pattern_analysis = result["pattern_analysis"]
        assert "has_periodicity" in pattern_analysis
        assert "volatility" in pattern_analysis
        assert "seasonality" in pattern_analysis

        prediction_summary = result["prediction_summary"]
        assert "trend" in prediction_summary
        assert "max_value" in prediction_summary
        assert "min_value" in prediction_summary

        # 由于是递增模式，趋势应该是increasing
        assert prediction_summary["trend"] == "increasing"

    @pytest.mark.asyncio
    async def test_model_info_retrieval(self, prediction_service, mock_model_manager):
        """测试模型信息获取"""
        from app.models.predict_models import ModelInfo

        # Mock模型信息
        mock_model_info = ModelInfo(
            model_name="QPS预测模型",
            model_version="1.0",
            model_type="RandomForest",
            supported_prediction_types=[PredictionType.QPS],
            training_data_size=10000,
            accuracy_metrics={"r2": 0.85, "rmse": 15.2},
        )

        mock_model_manager.get_models_info.return_value = [mock_model_info]
        mock_model_manager.get_detailed_info.return_value = {
            "models": [{"type": "qps", "name": "QPS预测模型", "loaded": True}],
            "total_models": 1,
            "models_loaded": True,
        }

        prediction_service._model_manager = mock_model_manager
        prediction_service._initialized = True

        # 获取模型信息
        model_info = await prediction_service.get_model_info()

        assert model_info is not None
        assert "models" in model_info
        assert model_info["total_models"] == 1
        assert model_info["models_loaded"] is True


class TestPredictionServiceErrorHandling:
    """预测服务错误处理测试"""

    @pytest.mark.asyncio
    async def test_model_loading_failure(self, prediction_service):
        """测试模型加载失败处理"""
        with patch("app.core.prediction.ModelManager") as mock_manager_class:
            mock_manager = AsyncMock()
            mock_manager.initialize.side_effect = Exception("模型加载失败")
            mock_manager_class.return_value = mock_manager

            # 初始化应该失败
            with pytest.raises(PredictionError) as exc_info:
                await prediction_service._do_initialize()
            assert "服务初始化失败" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_prediction_execution_error(
        self, prediction_service, mock_model_manager
    ):
        """测试预测执行错误处理"""
        # Mock预测器抛出异常
        mock_predictor = AsyncMock()
        mock_predictor.predict.side_effect = Exception("预测执行失败")

        prediction_service._model_manager = mock_model_manager
        prediction_service._predictor = mock_predictor
        prediction_service._feature_extractor = AsyncMock()
        prediction_service._anomaly_detector = AsyncMock()
        prediction_service._scaling_advisor = AsyncMock()
        prediction_service._cost_analyzer = AsyncMock()
        prediction_service._initialized = True

        # 预测应该失败并抛出异常
        with pytest.raises(PredictionError) as exc_info:
            await prediction_service.predict_qps(current_qps=100.0)
        assert "QPS预测失败" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_health_check_failure_handling(self, prediction_service):
        """测试健康检查失败处理"""
        # Mock不健康的组件
        mock_model_manager = AsyncMock()
        mock_model_manager.is_healthy.return_value = False

        prediction_service._model_manager = mock_model_manager
        prediction_service._initialized = True

        # 健康检查应该返回False
        is_healthy = await prediction_service._do_health_check()
        assert not is_healthy


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
