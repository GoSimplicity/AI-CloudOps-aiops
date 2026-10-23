#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 自动修复功能完整测试套件
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from legacy_guard import require_legacy_opt_in
require_legacy_opt_in()

import pytest
from check_utils import (
    DEFAULT_API_BASE_URL,
    TestResult,
    make_request,
    setup_test_logger,
    validate_response_structure,
)

logger = setup_test_logger("test_autofix")


class TestAutoFixAPI:
    """自动修复API测试类"""

    @classmethod
    def setup_class(cls):
        """测试类初始化"""
        cls.api_base_url = DEFAULT_API_BASE_URL
        cls.test_result = TestResult("自动修复API测试")

    def test_autofix_ready_check(self):
        """测试自动修复服务就绪性检查"""
        logger.info("测试自动修复服务就绪性检查 /api/v1/autofix/ready")

        url = f"{self.api_base_url}/autofix/ready"
        response = make_request("get", url, logger=logger)

        assert response is not None, "自动修复就绪性检查请求失败"
        # 就绪性检查可能返回503(未就绪)或200(就绪)
        assert response.status_code in [200, 503], (
            f"自动修复就绪性检查返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert "status" in data, "响应数据缺少status字段"

        self.test_result.add_test_result(
            "autofix_ready_check",
            True,
            status_code=response.status_code,
            ready_status=data.get("status"),
        )

    def test_autofix_info(self):
        """测试自动修复服务信息接口"""
        logger.info("测试自动修复服务信息接口 /api/v1/autofix/info")

        url = f"{self.api_base_url}/autofix/info"
        response = make_request("get", url, logger=logger)

        assert response is not None, "自动修复服务信息请求失败"
        assert response.status_code == 200, (
            f"自动修复服务信息返回状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert "service" in data, "响应数据缺少service字段"
        assert "version" in data, "响应数据缺少version字段"
        assert "components" in data, "响应数据缺少components字段"
        assert "features" in data, "响应数据缺少features字段"

        self.test_result.add_test_result(
            "autofix_info",
            True,
            status_code=response.status_code,
            service=data.get("service"),
        )

    def test_autofix_diagnose_cluster(self):
        """测试集群诊断接口"""
        logger.info("测试集群诊断接口 /api/v1/autofix/diagnose")

        url = f"{self.api_base_url}/autofix/diagnose"
        payload = {"namespace": "default"}

        response = make_request(
            "post", url, json_data=payload, logger=logger, timeout=30
        )

        assert response is not None, "集群诊断请求失败"
        # 集群诊断可能成功(200)或失败(500)，取决于K8s连接状态
        assert response.status_code in [200, 500], (
            f"集群诊断返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        if response.status_code == 200:
            data = validation["data"]["data"]
            assert "status" in data, "响应数据缺少status字段"
            assert "namespace" in data, "响应数据缺少namespace字段"
            assert "diagnosis" in data, "响应数据缺少diagnosis字段"

        self.test_result.add_test_result(
            "autofix_diagnose_cluster", True, status_code=response.status_code
        )

    def test_autofix_diagnose_invalid_namespace(self):
        """测试无效命名空间的集群诊断"""
        logger.info("测试无效命名空间的集群诊断")

        url = f"{self.api_base_url}/autofix/diagnose"
        payload = {
            "namespace": "invalid-namespace-name!"  # 包含无效字符
        }

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "集群诊断请求失败"
        assert response.status_code == 400, (
            f"无效命名空间应返回400状态码，实际: {response.status_code}"
        )

        self.test_result.add_test_result(
            "autofix_diagnose_invalid_namespace", True, status_code=response.status_code
        )

    def test_autofix_k8s_repair_validation(self):
        """测试K8s修复接口参数验证"""
        logger.info("测试K8s修复接口参数验证")

        url = f"{self.api_base_url}/autofix"

        # 测试缺少必需参数
        payload = {
            "namespace": "default"
            # 缺少deployment和event参数
        }

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "K8s修复请求失败"
        assert response.status_code == 400, (
            f"缺少参数应返回400状态码，实际: {response.status_code}"
        )

        self.test_result.add_test_result(
            "autofix_k8s_repair_validation", True, status_code=response.status_code
        )

    def test_autofix_k8s_repair_invalid_deployment(self):
        """测试无效deployment名称的K8s修复"""
        logger.info("测试无效deployment名称的K8s修复")

        url = f"{self.api_base_url}/autofix"

        payload = {
            "deployment": "invalid-deployment-name!",  # 包含无效字符
            "namespace": "default",
            "event": "测试事件描述",
        }

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "K8s修复请求失败"
        assert response.status_code == 400, (
            f"无效deployment名称应返回400状态码，实际: {response.status_code}"
        )

        self.test_result.add_test_result(
            "autofix_k8s_repair_invalid_deployment",
            True,
            status_code=response.status_code,
        )

    def test_autofix_k8s_repair_valid_params(self):
        """测试有效参数的K8s修复"""
        logger.info("测试有效参数的K8s修复")

        url = f"{self.api_base_url}/autofix"

        payload = {
            "deployment": "test-deployment",
            "namespace": "default",
            "event": "Pod处于CrashLoopBackOff状态，需要进行故障排查和修复",
            "force": False,
            "auto_restart": True,
        }

        response = make_request(
            "post", url, json_data=payload, logger=logger, timeout=60
        )

        assert response is not None, "K8s修复请求失败"
        # K8s修复可能成功(200)或失败(500)，取决于集群连接状态和部署是否存在
        assert response.status_code in [200, 500], (
            f"K8s修复返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        if response.status_code == 200:
            data = validation["data"]["data"]
            assert "status" in data, "响应数据缺少status字段"
            assert "deployment" in data, "响应数据缺少deployment字段"
            assert "namespace" in data, "响应数据缺少namespace字段"
            assert "success" in data, "响应数据缺少success字段"

        self.test_result.add_test_result(
            "autofix_k8s_repair_valid_params", True, status_code=response.status_code
        )

    def test_autofix_workflow_execution(self):
        """测试工作流执行接口"""
        logger.info("测试工作流执行接口 /api/v1/autofix/workflow")

        url = f"{self.api_base_url}/autofix/workflow"

        payload = {
            "problem_description": "系统出现高CPU使用率和内存泄漏问题，需要进行自动化故障排查和修复"
        }

        response = make_request(
            "post", url, json_data=payload, logger=logger, timeout=60
        )

        assert response is not None, "工作流执行请求失败"
        # 工作流执行可能成功(200)或失败(500)
        assert response.status_code in [200, 500], (
            f"工作流执行返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        self.test_result.add_test_result(
            "autofix_workflow_execution", True, status_code=response.status_code
        )

    def test_autofix_workflow_missing_description(self):
        """测试缺少问题描述的工作流执行"""
        logger.info("测试缺少问题描述的工作流执行")

        url = f"{self.api_base_url}/autofix/workflow"

        payload = {}  # 缺少problem_description参数

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "工作流执行请求失败"
        assert response.status_code == 400, (
            f"缺少描述应返回400状态码，实际: {response.status_code}"
        )

        self.test_result.add_test_result(
            "autofix_workflow_missing_description",
            True,
            status_code=response.status_code,
        )

    def test_autofix_notification(self):
        """测试通知发送接口"""
        logger.info("测试通知发送接口 /api/v1/autofix/notify")

        url = f"{self.api_base_url}/autofix/notify"

        payload = {
            "title": "自动修复测试通知",
            "message": "这是一条来自测试的自动修复通知消息",
            "type": "info",
        }

        status_code = None
        error_message = None

        try:
            response = make_request("post", url, json_data=payload, logger=logger)

            assert response is not None, "通知发送请求失败"
            status_code = response.status_code
            assert response.status_code == 200, (
                f"通知发送返回状态码 {response.status_code}"
            )

            # 验证响应结构
            required_fields = ["code", "message", "data"]
            validation = validate_response_structure(response, required_fields, logger)
            assert validation["valid"], (
                f"响应结构验证失败: {validation['missing_fields']}"
            )

            data = validation["data"]["data"]
            assert "success" in data, "响应数据缺少success字段"
            assert "type" in data, "响应数据缺少type字段"

            self.test_result.add_test_result(
                "autofix_notification",
                True,
                status_code=response.status_code,
                notification_success=data.get("success"),
            )
        except Exception as e:
            error_message = str(e)
            logger.error(f"通知发送测试失败: {error_message}")
            self.test_result.add_test_result(
                "autofix_notification",
                False,
                status_code=status_code,
                error_message=error_message,
            )
            # 重新抛出异常以让pytest知道测试失败
            raise

    def test_autofix_notification_missing_message(self):
        """测试缺少消息内容的通知发送"""
        logger.info("测试缺少消息内容的通知发送")

        url = f"{self.api_base_url}/autofix/notify"

        payload = {
            "title": "测试通知"
            # 缺少message参数
        }

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "通知发送请求失败"
        assert response.status_code == 400, (
            f"缺少消息应返回400状态码，实际: {response.status_code}"
        )

        self.test_result.add_test_result(
            "autofix_notification_missing_message",
            True,
            status_code=response.status_code,
        )

    def test_autofix_response_timing(self):
        """测试自动修复接口响应时间"""
        logger.info("测试自动修复接口响应时间")

        import time

        start_time = time.time()

        url = f"{self.api_base_url}/autofix/info"
        response = make_request("get", url, timeout=10, logger=logger)

        end_time = time.time()
        response_time = end_time - start_time

        assert response is not None, "自动修复信息请求失败"
        assert response.status_code == 200, (
            f"自动修复信息返回状态码 {response.status_code}"
        )
        assert response_time < 10.0, f"自动修复响应时间过长: {response_time:.2f}秒"

        self.test_result.add_test_result(
            "autofix_response_timing",
            True,
            status_code=response.status_code,
            response_time=response_time,
        )

        logger.info(f"自动修复信息接口响应时间: {response_time:.2f}秒")

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

        save_test_results(results, "logs/autofix_test_results.json", logger)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
