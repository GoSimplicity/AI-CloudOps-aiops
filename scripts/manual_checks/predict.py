#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 新预测服务完整测试套件 - 支持多种预测类型
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from legacy_guard import require_legacy_opt_in
require_legacy_opt_in()

from check_utils import (
    DEFAULT_API_BASE_URL,
    TestResult,
    make_request,
    setup_test_logger,
    validate_response_structure,
)

logger = setup_test_logger("test_predict_new")


class TestNewPredictAPI:
    """新预测API测试类 - 支持QPS、CPU、内存、磁盘预测"""

    @classmethod
    def setup_class(cls):
        """测试类初始化"""
        cls.api_base_url = DEFAULT_API_BASE_URL
        cls.test_result = TestResult("新预测服务API测试")

    def test_predict_ready_check(self):
        """测试预测服务就绪性检查"""
        logger.info("测试预测服务就绪性检查 /api/v1/predict/ready")

        url = f"{self.api_base_url}/predict/ready"
        response = make_request("get", url, logger=logger)

        assert response is not None, "预测就绪性检查请求失败"
        # 就绪检查可能返回200或503
        assert response.status_code in [200, 503], (
            f"预测就绪性检查返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert "ready" in data, "响应数据缺少ready字段"
        assert "initialized" in data, "响应数据缺少initialized字段"
        assert "healthy" in data, "响应数据缺少healthy字段"

        self.test_result.add_test_result(
            "predict_ready_check",
            True,
            status_code=response.status_code,
            ready=data.get("ready"),
            initialized=data.get("initialized"),
        )

    def test_predict_info(self):
        """测试预测服务信息接口"""
        logger.info("测试预测服务信息接口 /api/v1/predict/info")

        url = f"{self.api_base_url}/predict/info"
        response = make_request("get", url, logger=logger)

        assert response is not None, "预测服务信息请求失败"
        assert response.status_code == 200, (
            f"预测服务信息返回状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert "service" in data, "响应数据缺少service字段"
        assert "version" in data, "响应数据缺少version字段"
        assert "supported_prediction_types" in data, (
            "响应数据缺少supported_prediction_types字段"
        )
        assert "endpoints" in data, "响应数据缺少endpoints字段"

        # 验证支持的预测类型
        supported_types = data["supported_prediction_types"]
        expected_types = ["qps", "cpu", "memory", "disk"]
        for pred_type in expected_types:
            type_found = any(t.get("type") == pred_type for t in supported_types)
            assert type_found, f"未找到支持的预测类型: {pred_type}"

        self.test_result.add_test_result(
            "predict_info",
            True,
            status_code=response.status_code,
            service=data.get("service"),
            supported_types=[t.get("type") for t in supported_types],
        )

    def test_predict_models(self):
        """测试预测模型信息接口"""
        logger.info("测试预测模型信息接口 /api/v1/predict/models")

        url = f"{self.api_base_url}/predict/models"
        response = make_request("get", url, logger=logger)

        assert response is not None, "模型信息请求失败"
        assert response.status_code == 200, f"模型信息返回状态码 {response.status_code}"

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert "models" in data, "响应数据缺少models字段"
        assert "total_models" in data, "响应数据缺少total_models字段"
        assert "models_loaded" in data, "响应数据缺少models_loaded字段"

        self.test_result.add_test_result(
            "predict_models",
            True,
            status_code=response.status_code,
            total_models=data.get("total_models"),
            models_loaded=data.get("models_loaded"),
        )

    def test_qps_prediction(self):
        """测试QPS预测接口"""
        logger.info("测试QPS预测接口 /api/v1/predict/qps")

        url = f"{self.api_base_url}/predict/qps"
        payload = {
            "current_qps": 150.5,
            "prediction_hours": 12,
            "granularity": "hour",
            "include_confidence": True,
            "include_anomaly_detection": True,
            "consider_historical_pattern": True,
            "target_utilization": 0.7,
            "sensitivity": 0.8,
        }

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "QPS预测请求失败"
        assert response.status_code == 200, f"QPS预测返回状态码 {response.status_code}"

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert "prediction_type" in data, "响应数据缺少prediction_type字段"
        assert "predicted_data" in data, "响应数据缺少predicted_data字段"
        assert "scaling_recommendations" in data, (
            "响应数据缺少scaling_recommendations字段"
        )
        assert "current_value" in data, "响应数据缺少current_value字段"

        # 验证预测数据
        assert data["prediction_type"] == "qps", "预测类型不匹配"
        assert data["current_value"] == 150.5, "当前值不匹配"
        assert isinstance(data["predicted_data"], list), "预测数据应为列表"
        assert len(data["predicted_data"]) > 0, "预测数据不能为空"

        # 验证预测数据点结构
        first_prediction = data["predicted_data"][0]
        assert "timestamp" in first_prediction, "预测点缺少timestamp字段"
        assert "predicted_value" in first_prediction, "预测点缺少predicted_value字段"
        assert "confidence_level" in first_prediction, "预测点缺少confidence_level字段"

        self.test_result.add_test_result(
            "qps_prediction",
            True,
            status_code=response.status_code,
            prediction_type=data.get("prediction_type"),
            prediction_count=len(data["predicted_data"]),
            recommendations_count=len(data.get("scaling_recommendations", [])),
        )

    def test_cpu_prediction(self):
        """测试CPU预测接口"""
        logger.info("测试CPU预测接口 /api/v1/predict/cpu")

        url = f"{self.api_base_url}/predict/cpu"
        payload = {
            "current_cpu_percent": 75.2,
            "prediction_hours": 24,
            "granularity": "hour",
            "resource_constraints": {
                "cpu_cores": 4.0,
                "max_instances": 10,
                "min_instances": 2,
            },
            "include_confidence": True,
            "include_anomaly_detection": True,
            "target_utilization": 0.65,
        }

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "CPU预测请求失败"
        assert response.status_code == 200, f"CPU预测返回状态码 {response.status_code}"

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert data["prediction_type"] == "cpu", "CPU预测类型不匹配"
        assert data["current_value"] == 75.2, "CPU当前值不匹配"
        assert "resource_utilization" in data, "缺少资源利用率预测"

        self.test_result.add_test_result(
            "cpu_prediction",
            True,
            status_code=response.status_code,
            prediction_type=data.get("prediction_type"),
            current_value=data.get("current_value"),
        )

    def test_memory_prediction(self):
        """测试内存预测接口"""
        logger.info("测试内存预测接口 /api/v1/predict/memory")

        url = f"{self.api_base_url}/predict/memory"
        payload = {
            "current_memory_percent": 68.5,
            "prediction_hours": 48,
            "granularity": "hour",
            "resource_constraints": {"memory_gb": 16.0, "cost_per_hour": 0.5},
            "include_confidence": False,
            "include_anomaly_detection": True,
        }

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "内存预测请求失败"
        assert response.status_code == 200, f"内存预测返回状态码 {response.status_code}"

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert data["prediction_type"] == "memory", "内存预测类型不匹配"
        assert "cost_analysis" in data, "缺少成本分析"

        self.test_result.add_test_result(
            "memory_prediction",
            True,
            status_code=response.status_code,
            prediction_type=data.get("prediction_type"),
            has_cost_analysis=data.get("cost_analysis") is not None,
        )

    def test_disk_prediction(self):
        """测试磁盘预测接口"""
        logger.info("测试磁盘预测接口 /api/v1/predict/disk")

        url = f"{self.api_base_url}/predict/disk"
        payload = {
            "current_disk_percent": 82.3,
            "prediction_hours": 72,
            "granularity": "day",
            "resource_constraints": {"disk_gb": 500.0, "max_instances": 5},
            "sensitivity": 0.9,
        }

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "磁盘预测请求失败"
        assert response.status_code == 200, f"磁盘预测返回状态码 {response.status_code}"

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert data["prediction_type"] == "disk", "磁盘预测类型不匹配"
        assert data["granularity"] == "day", "磁盘预测粒度不匹配"

        self.test_result.add_test_result(
            "disk_prediction",
            True,
            status_code=response.status_code,
            prediction_type=data.get("prediction_type"),
            granularity=data.get("granularity"),
        )

    def test_prediction_validation_errors(self):
        """测试预测参数验证错误"""
        logger.info("测试预测参数验证错误处理")

        # 测试无效的预测类型
        url = f"{self.api_base_url}/predict/qps"
        invalid_payload = {
            "current_qps": 100,
            "prediction_type_invalid": "invalid_type",
        }

        response = make_request("post", url, json_data=invalid_payload, logger=logger)
        assert response is not None, "验证错误测试请求失败"
        assert response.status_code == 400, (
            f"期望400错误状态码，实际: {response.status_code}"
        )

        # 测试无效的QPS值
        invalid_qps_payload = {
            "current_qps": -10  # 负数QPS
        }

        response = make_request(
            "post", url, json_data=invalid_qps_payload, logger=logger
        )
        assert response is not None, "负数QPS验证测试请求失败"
        assert response.status_code == 422, (
            f"期望422验证错误状态码，实际: {response.status_code}"
        )

        # 测试无效的预测时长
        invalid_hours_payload = {
            "current_qps": 100,
            "prediction_hours": 0,  # 无效时长
        }

        response = make_request(
            "post", url, json_data=invalid_hours_payload, logger=logger
        )
        assert response is not None, "无效时长验证测试请求失败"
        assert response.status_code == 422, (
            f"期望422验证错误状态码，实际: {response.status_code}"
        )

        self.test_result.add_test_result(
            "prediction_validation_errors",
            True,
            invalid_type_handled=True,
            invalid_qps_handled=True,
            invalid_hours_handled=True,
        )

    def test_prediction_pattern_analysis(self):
        """测试预测模式分析功能"""
        logger.info("测试预测模式分析功能")

        url = f"{self.api_base_url}/predict/qps"
        payload = {
            "current_qps": 200,
            "prediction_hours": 24,
            "consider_historical_pattern": True,
            "include_confidence": True,
        }

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "模式分析测试请求失败"
        assert response.status_code == 200, f"模式分析返回状态码 {response.status_code}"

        data = response.json()["data"]
        assert "pattern_analysis" in data, "缺少模式分析结果"
        assert "trend_insights" in data, "缺少趋势洞察"
        assert "prediction_summary" in data, "缺少预测摘要"

        pattern_analysis = data["pattern_analysis"]
        assert "has_periodicity" in pattern_analysis, "缺少周期性分析"
        assert "volatility" in pattern_analysis, "缺少波动性分析"

        self.test_result.add_test_result(
            "prediction_pattern_analysis",
            True,
            status_code=response.status_code,
            has_pattern_analysis=True,
            has_trend_insights=len(data.get("trend_insights", [])) > 0,
        )

    @classmethod
    def teardown_class(cls):
        """测试类清理"""
        logger.info("预测服务测试完成")
        logger.info(f"测试结果统计: {cls.test_result.get_summary()}")


def test_predict_service_comprehensive():
    """预测服务综合测试"""
    test_class = TestNewPredictAPI()
    test_class.setup_class()

    # 运行所有测试
    test_methods = [
        test_class.test_predict_ready_check,
        test_class.test_predict_info,
        test_class.test_predict_models,
        test_class.test_qps_prediction,
        test_class.test_cpu_prediction,
        test_class.test_memory_prediction,
        test_class.test_disk_prediction,
        test_class.test_prediction_validation_errors,
        test_class.test_prediction_pattern_analysis,
    ]

    for test_method in test_methods:
        try:
            test_method()
            logger.info(f"✓ {test_method.__name__} 通过")
        except Exception as e:
            logger.error(f"✗ {test_method.__name__} 失败: {str(e)}")

    test_class.teardown_class()


if __name__ == "__main__":
    # 直接运行综合测试
    test_predict_service_comprehensive()
