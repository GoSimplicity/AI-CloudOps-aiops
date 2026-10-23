#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 根因分析功能完整测试套件
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from legacy_guard import require_legacy_opt_in
require_legacy_opt_in()

from datetime import datetime, timedelta

import pytest
from check_utils import (
    DEFAULT_API_BASE_URL,
    TestResult,
    make_request,
    setup_test_logger,
    validate_response_structure,
)

logger = setup_test_logger("test_rca")


class TestRCAAPI:
    """根因分析API测试类"""

    @classmethod
    def setup_class(cls):
        """测试类初始化"""
        cls.api_base_url = DEFAULT_API_BASE_URL
        cls.test_result = TestResult("根因分析API测试")

    def test_rca_ready_check(self):
        """测试RCA服务就绪性检查"""
        logger.info("测试RCA服务就绪性检查 /api/v1/rca/ready")

        url = f"{self.api_base_url}/rca/ready"
        response = make_request("get", url, logger=logger)

        assert response is not None, "RCA就绪性检查请求失败"
        assert response.status_code in [200, 503], (
            f"RCA就绪性检查返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert "status" in data, "响应数据缺少status字段"

        self.test_result.add_test_result(
            "rca_ready_check",
            True,
            status_code=response.status_code,
            ready_status=data.get("status"),
        )

    def test_rca_info(self):
        """测试RCA服务信息接口"""
        logger.info("测试RCA服务信息接口 /api/v1/rca/info")

        url = f"{self.api_base_url}/rca/info"
        response = make_request("get", url, logger=logger)

        assert response is not None, "RCA服务信息请求失败"
        assert response.status_code == 200, (
            f"RCA服务信息返回状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert "service" in data, "响应数据缺少service字段"
        assert "version" in data, "响应数据缺少version字段"
        assert "components" in data, "响应数据缺少components字段"

        self.test_result.add_test_result(
            "rca_info",
            True,
            status_code=response.status_code,
            service=data.get("service"),
        )

    def test_rca_config(self):
        """测试RCA配置接口"""
        logger.info("测试RCA配置接口 /api/v1/rca/config")

        url = f"{self.api_base_url}/rca/config"
        response = make_request("get", url, logger=logger)

        assert response is not None, "RCA配置请求失败"
        assert response.status_code == 200, f"RCA配置返回状态码 {response.status_code}"

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert "anomaly_detection" in data, "响应数据缺少anomaly_detection字段"
        assert "correlation_analysis" in data, "响应数据缺少correlation_analysis字段"
        assert "time_range" in data, "响应数据缺少time_range字段"
        assert "metrics" in data, "响应数据缺少metrics字段"

        # 验证配置数据类型
        anomaly_config = data["anomaly_detection"]
        assert "threshold" in anomaly_config, "异常检测配置缺少threshold"
        assert "methods" in anomaly_config, "异常检测配置缺少methods"

        self.test_result.add_test_result(
            "rca_config",
            True,
            status_code=response.status_code,
            anomaly_threshold=anomaly_config.get("threshold"),
        )

    def test_rca_metrics(self):
        """测试获取可用指标接口"""
        logger.info("测试获取可用指标接口 /api/v1/rca/metrics")

        url = f"{self.api_base_url}/rca/metrics"
        response = make_request("get", url, logger=logger)

        assert response is not None, "RCA指标请求失败"
        assert response.status_code == 200, f"RCA指标返回状态码 {response.status_code}"

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert "default_metrics" in data, "响应数据缺少default_metrics字段"
        assert "categories" in data, "响应数据缺少categories字段"

        # 验证默认指标列表
        default_metrics = data["default_metrics"]
        assert isinstance(default_metrics, list), "默认指标应为列表"
        assert len(default_metrics) > 0, "默认指标列表不应为空"

        # 验证指标分类
        categories = data["categories"]
        expected_categories = ["CPU", "Memory", "Network", "Kubernetes"]
        for category in expected_categories:
            assert category in categories, f"缺少指标分类: {category}"

        self.test_result.add_test_result(
            "rca_metrics",
            True,
            status_code=response.status_code,
            default_metrics_count=len(default_metrics),
            categories_count=len(categories),
        )

    def test_rca_analysis_with_minimal_params(self):
        """测试最小参数的根因分析"""
        logger.info("测试最小参数的根因分析")

        url = f"{self.api_base_url}/rca"

        # 使用最小参数，让系统使用默认时间范围和指标
        payload = {}

        response = make_request(
            "post", url, json_data=payload, logger=logger, timeout=30
        )

        assert response is not None, "RCA分析请求失败"
        # 根因分析可能成功(200)或失败(500)，取决于Prometheus连接状态
        assert response.status_code in [200, 500], (
            f"RCA分析返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        if response.status_code == 200:
            data = validation["data"]["data"]
            if "anomalies" in data:
                assert isinstance(data["anomalies"], dict), "异常数据应为字典"
            if "root_cause_candidates" in data:
                assert isinstance(data["root_cause_candidates"], list), (
                    "根因候选应为列表"
                )

        self.test_result.add_test_result(
            "rca_analysis_minimal_params", True, status_code=response.status_code
        )

    def test_rca_analysis_with_time_range(self):
        """测试指定时间范围的根因分析"""
        logger.info("测试指定时间范围的根因分析")

        url = f"{self.api_base_url}/rca"

        # 指定30分钟的时间范围
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(minutes=30)

        payload = {
            "start_time": start_time.isoformat() + "Z",
            "end_time": end_time.isoformat() + "Z",
            "metrics": [
                "container_cpu_usage_seconds_total",
                "container_memory_working_set_bytes",
            ],
        }

        response = make_request(
            "post", url, json_data=payload, logger=logger, timeout=30
        )

        assert response is not None, "RCA分析请求失败"
        # 根因分析可能成功(200)或失败(500)
        assert response.status_code in [200, 500], (
            f"RCA分析返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        self.test_result.add_test_result(
            "rca_analysis_with_time_range", True, status_code=response.status_code
        )

    def test_rca_analysis_invalid_time_range(self):
        """测试无效时间范围的根因分析"""
        logger.info("测试无效时间范围的根因分析")

        url = f"{self.api_base_url}/rca"

        # 时间范围过大(超过最大限制)
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=7)  # 7天，超过限制

        payload = {
            "start_time": start_time.isoformat() + "Z",
            "end_time": end_time.isoformat() + "Z",
        }

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "RCA分析请求失败"
        assert response.status_code == 400, (
            f"无效时间范围应返回400状态码，实际: {response.status_code}"
        )

        self.test_result.add_test_result(
            "rca_analysis_invalid_time_range", True, status_code=response.status_code
        )

    def test_rca_incident_analysis(self):
        """测试特定事件分析"""
        logger.info("测试特定事件分析 /api/v1/rca/incident")

        url = f"{self.api_base_url}/rca/incident"

        payload = {
            "affected_services": ["nginx", "redis"],
            "symptoms": ["高CPU使用率", "响应时间增加"],
            "start_time": (datetime.utcnow() - timedelta(minutes=30)).isoformat() + "Z",
            "end_time": datetime.utcnow().isoformat() + "Z",
        }

        response = make_request(
            "post", url, json_data=payload, logger=logger, timeout=30
        )

        assert response is not None, "RCA事件分析请求失败"
        # 事件分析可能成功(200)或失败(500)
        assert response.status_code in [200, 500], (
            f"RCA事件分析返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        self.test_result.add_test_result(
            "rca_incident_analysis", True, status_code=response.status_code
        )

    def test_rca_incident_analysis_missing_params(self):
        """测试缺少参数的事件分析"""
        logger.info("测试缺少参数的事件分析")

        url = f"{self.api_base_url}/rca/incident"

        # 缺少必需的affected_services参数
        payload = {"symptoms": ["高CPU使用率"]}

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "RCA事件分析请求失败"
        assert response.status_code == 400, (
            f"缺少参数应返回400状态码，实际: {response.status_code}"
        )

        self.test_result.add_test_result(
            "rca_incident_analysis_missing_params",
            True,
            status_code=response.status_code,
        )

    def test_rca_response_timing(self):
        """测试RCA接口响应时间"""
        logger.info("测试RCA接口响应时间")

        import time

        start_time = time.time()

        url = f"{self.api_base_url}/rca/config"
        response = make_request("get", url, timeout=10, logger=logger)

        end_time = time.time()
        response_time = end_time - start_time

        assert response is not None, "RCA配置请求失败"
        assert response.status_code == 200, f"RCA配置返回状态码 {response.status_code}"
        assert response_time < 10.0, f"RCA响应时间过长: {response_time:.2f}秒"

        self.test_result.add_test_result(
            "rca_response_timing",
            True,
            status_code=response.status_code,
            response_time=response_time,
        )

        logger.info(f"RCA配置接口响应时间: {response_time:.2f}秒")

    @classmethod
    def teardown_class(cls):
        """测试类清理"""
        results = cls.test_result.finalize()

        # 打印测试摘要
        from check_utils import print_test_summary

        print_test_summary(
            results, results.get("summary", {}).get("duration_seconds", 0)
        )

        # 保存测试结果
        from check_utils import save_test_results

        save_test_results(results, "logs/rca_test_results.json", logger)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
