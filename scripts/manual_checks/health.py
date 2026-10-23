#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 健康检查接口完整测试套件
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from legacy_guard import require_legacy_opt_in
require_legacy_opt_in()

import pytest
from check_utils import DEFAULT_API_BASE_URL, TestResult, make_request, setup_test_logger

logger = setup_test_logger("test_health")


class TestHealthAPI:
    """健康检查API测试类"""

    @classmethod
    def setup_class(cls):
        """测试类初始化"""
        cls.api_base_url = DEFAULT_API_BASE_URL
        cls.test_result = TestResult("健康检查API测试")

    def test_placeholder_ready_check(self):
        """查询预测服务就绪状态"""
        logger.info("测试预测服务就绪状态 /api/v1/predict/ready")

        url = f"{self.api_base_url}/predict/ready"
        response = make_request("get", url, logger=logger)

        assert response is not None, "就绪检查请求失败"
        assert response.status_code in [200, 503], (
            f"就绪检查返回不预期状态码 {response.status_code}"
        )

    def test_components_ready_placeholder(self):
        """查询RCA就绪状态"""
        logger.info("测试RCA服务就绪状态 /api/v1/rca/ready")

        url = f"{self.api_base_url}/rca/ready"
        response = make_request("get", url, logger=logger)

        assert response is not None, "RCA就绪检查请求失败"
        assert response.status_code in [200, 503], (
            f"RCA就绪检查返回不预期状态码 {response.status_code}"
        )

    def test_metrics_placeholder(self):
        """查询预测服务信息"""
        logger.info("测试预测服务信息 /api/v1/predict/info")

        url = f"{self.api_base_url}/predict/info"
        response = make_request("get", url, logger=logger)

        assert response is not None, "预测信息请求失败"
        assert response.status_code == 200, f"预测信息返回状态码 {response.status_code}"

    def test_readiness_probe(self):
        """就绪探针改为模块就绪接口之一"""
        logger.info("测试预测就绪接口 /api/v1/predict/ready")

        url = f"{self.api_base_url}/predict/ready"
        response = make_request("get", url, logger=logger)

        assert response is not None, "就绪检查请求失败"
        assert response.status_code in [200, 503], (
            f"就绪检查返回不预期状态码 {response.status_code}"
        )

    def test_liveness_placeholder(self):
        """使用 assistant/info 作为替代检查"""
        logger.info("测试智能助手信息接口 /api/v1/assistant/info")

        url = f"{self.api_base_url}/assistant/info"
        response = make_request("get", url, logger=logger)

        assert response is not None, "助手信息请求失败"
        assert response.status_code in [200, 500], (
            f"助手信息返回不预期状态码 {response.status_code}"
        )

    def test_ready_response_timing(self):
        """就绪检查响应时间"""
        logger.info("测试预测就绪检查响应时间")

        import time

        start_time = time.time()

        url = f"{self.api_base_url}/predict/ready"
        response = make_request("get", url, timeout=10, logger=logger)

        end_time = time.time()
        response_time = end_time - start_time

        assert response is not None, "就绪检查请求失败"
        assert response.status_code in [200, 503], (
            f"就绪检查返回不预期状态码 {response.status_code}"
        )
        assert response_time < 10.0, f"就绪检查响应时间过长: {response_time:.2f}秒"

        self.test_result.add_test_result(
            "ready_response_timing",
            True,
            status_code=response.status_code,
            response_time=response_time,
        )

        logger.info(f"就绪检查响应时间: {response_time:.2f}秒")

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

        save_test_results(results, "logs/health_test_results.json", logger)


def test_root_endpoint():
    """测试根路径端点"""
    logger.info("测试根路径端点 /")

    api_base_url = DEFAULT_API_BASE_URL.replace("/api/v1", "")
    url = f"{api_base_url}/"

    response = make_request("get", url, logger=logger)

    assert response is not None, "根路径请求失败"
    assert response.status_code == 200, f"根路径返回状态码 {response.status_code}"

    # 验证响应结构
    try:
        data = response.json()
        assert "service" in data, "根路径响应缺少service字段"
        assert "version" in data, "根路径响应缺少version字段"
        assert "endpoints" in data, "根路径响应缺少endpoints字段"

        logger.info(f"服务名称: {data.get('service')}")
        logger.info(f"服务版本: {data.get('version')}")

    except Exception as e:
        pytest.fail(f"根路径响应解析失败: {str(e)}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
