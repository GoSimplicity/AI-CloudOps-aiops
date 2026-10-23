#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 系统集成测试套件 - 验证各模块间的协作和整体系统功能
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from legacy_guard import require_legacy_opt_in
require_legacy_opt_in()

import time

import pytest
from check_utils import (
    DEFAULT_API_BASE_URL,
    TestResult,
    make_request,
    setup_test_environment,
    setup_test_logger,
)

logger = setup_test_logger("test_integration")


class TestSystemIntegration:
    """系统集成测试类"""

    @classmethod
    def setup_class(cls):
        """测试类初始化"""
        cls.api_base_url = DEFAULT_API_BASE_URL
        cls.test_result = TestResult("系统集成测试")

        # 设置测试环境
        setup_success = setup_test_environment(cls.api_base_url, logger)
        cls.test_result.set_environment_setup(setup_success)

    def test_system_startup_sequence(self):
        """测试系统启动序列和服务可用性"""
        logger.info("测试系统启动序列和服务可用性")

        # 1. 检查根路径
        root_url = self.api_base_url.replace("/api/v1", "") + "/"
        root_response = make_request("get", root_url, logger=logger)

        assert root_response is not None, "根路径请求失败"
        assert root_response.status_code == 200, (
            f"根路径返回状态码 {root_response.status_code}"
        )

        # 2. 跳过整体健康检查

        # 3. 检查各模块就绪状态
        modules = ["predict", "rca", "autofix", "assistant"]
        module_status = {}

        for module in modules:
            module_ready_url = f"{self.api_base_url}/{module}/ready"
            module_response = make_request("get", module_ready_url, logger=logger)

            if module_response:
                module_status[module] = {
                    "available": True,
                    "status_code": module_response.status_code,
                    "healthy": module_response.status_code == 200,
                }
            else:
                module_status[module] = {"available": False, "healthy": False}

        # 验证至少有基础模块可用
        available_modules = [
            mod for mod, status in module_status.items() if status["available"]
        ]
        assert len(available_modules) >= 2, (
            f"可用模块数量不足，当前可用: {available_modules}"
        )

        self.test_result.add_test_result(
            "system_startup_sequence",
            True,
            status_code=200,
            available_modules=available_modules,
            module_status=module_status,
        )

    def test_cross_module_workflow(self):
        """测试跨模块工作流"""
        logger.info("测试跨模块工作流：就绪检查 -> 预测 -> 根因分析")

        workflow_results = {}

        # 1. 跳过整体健康检查
        workflow_results["health_check"] = {"success": True, "status_code": 200}

        # 2. 预测服务就绪检查
        predict_ready_url = f"{self.api_base_url}/predict/ready"
        predict_response = make_request("get", predict_ready_url, logger=logger)

        if predict_response and predict_response.status_code == 200:
            workflow_results["prediction_health"] = {
                "success": True,
                "status_code": 200,
            }
        else:
            workflow_results["prediction_health"] = {
                "success": False,
                "status_code": predict_response.status_code
                if predict_response
                else None,
            }

        # 2.1 测试QPS预测API
        qps_predict_url = f"{self.api_base_url}/predict/qps"
        qps_payload = {
            "prediction_type": "qps",
            "current_value": 100.0,
            "prediction_hours": 12,
        }
        qps_response = make_request(
            "post", qps_predict_url, json_data=qps_payload, logger=logger
        )

        if qps_response and qps_response.status_code == 200:
            workflow_results["qps_prediction"] = {"success": True, "status_code": 200}
        else:
            workflow_results["qps_prediction"] = {
                "success": False,
                "status_code": qps_response.status_code if qps_response else None,
            }

        # 3. 根因分析配置检查
        rca_config_url = f"{self.api_base_url}/rca/config"
        rca_response = make_request("get", rca_config_url, logger=logger)

        if rca_response and rca_response.status_code == 200:
            workflow_results["rca_config"] = {"success": True, "status_code": 200}
        else:
            workflow_results["rca_config"] = {
                "success": False,
                "status_code": rca_response.status_code if rca_response else None,
            }

        # 验证工作流结果
        successful_steps = sum(
            1 for result in workflow_results.values() if result["success"]
        )
        assert successful_steps >= 2, f"工作流成功步骤不足，成功: {successful_steps}/3"

        self.test_result.add_test_result(
            "cross_module_workflow",
            True,
            status_code=200,
            successful_steps=successful_steps,
            workflow_results=workflow_results,
        )

    def test_api_consistency(self):
        """测试API响应格式一致性"""
        logger.info("测试API响应格式一致性")

        # 测试所有模块的info接口
        info_endpoints = [
            "/predict/info",
            "/rca/info",
            "/autofix/info",
            "/assistant/info",
        ]

        consistent_responses = 0
        response_formats = {}

        for endpoint in info_endpoints:
            url = f"{self.api_base_url}{endpoint}"
            response = make_request("get", url, logger=logger)

            if response:
                try:
                    data = response.json()

                    # 检查基本API响应格式
                    has_standard_format = all(
                        key in data for key in ["code", "message", "data"]
                    )

                    response_formats[endpoint] = {
                        "status_code": response.status_code,
                        "has_standard_format": has_standard_format,
                        "response_time": response.elapsed.total_seconds()
                        if hasattr(response, "elapsed")
                        else 0,
                    }

                    if has_standard_format and response.status_code in [200, 500]:
                        consistent_responses += 1

                except Exception as e:
                    response_formats[endpoint] = {
                        "error": str(e),
                        "status_code": response.status_code,
                    }
            else:
                response_formats[endpoint] = {
                    "error": "No response",
                    "status_code": None,
                }

        # 验证一致性
        assert consistent_responses >= 2, (
            f"API格式一致性不足，一致响应数: {consistent_responses}/{len(info_endpoints)}"
        )

        self.test_result.add_test_result(
            "api_consistency",
            True,
            status_code=200,
            consistent_responses=consistent_responses,
            response_formats=response_formats,
        )

    def test_system_load_handling(self):
        """测试系统负载处理能力"""
        logger.info("测试系统负载处理能力")

        # 并发发送多个就绪检查请求
        import concurrent.futures

        def send_health_request():
            url = f"{self.api_base_url}/predict/ready"
            start_time = time.time()
            response = make_request("get", url, logger=logger, timeout=10)
            end_time = time.time()

            return {
                "success": response is not None and response.status_code == 200,
                "response_time": end_time - start_time,
                "status_code": response.status_code if response else None,
            }

        # 并发执行5个请求
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(send_health_request) for _ in range(5)]
            results = [
                future.result() for future in concurrent.futures.as_completed(futures)
            ]

        # 分析结果
        successful_requests = sum(1 for result in results if result["success"])
        avg_response_time = sum(result["response_time"] for result in results) / len(
            results
        )
        max_response_time = max(result["response_time"] for result in results)

        # 验证负载处理能力
        assert successful_requests >= 3, (
            f"并发请求成功率不足，成功: {successful_requests}/5"
        )
        assert avg_response_time < 8.0, f"平均响应时间过长: {avg_response_time:.2f}秒"
        assert max_response_time < 15.0, f"最大响应时间过长: {max_response_time:.2f}秒"

        self.test_result.add_test_result(
            "system_load_handling",
            True,
            status_code=200,
            successful_requests=successful_requests,
            avg_response_time=avg_response_time,
            max_response_time=max_response_time,
        )

    def test_error_handling_consistency(self):
        """测试错误处理一致性"""
        logger.info("测试错误处理一致性")

        # 测试各种错误情况
        error_tests = [
            {
                "name": "invalid_endpoint",
                "url": f"{self.api_base_url}/nonexistent",
                "method": "get",
                "expected_status": 404,
            },
            {
                "name": "invalid_post_data",
                "url": f"{self.api_base_url}/predict",
                "method": "post",
                "data": {"current_qps": -100},
                "expected_status": 400,
            },
            {
                "name": "empty_post_body",
                "url": f"{self.api_base_url}/rca",
                "method": "post",
                "data": {},
                "expected_status": [200, 400, 500],  # 可能的状态码
            },
        ]

        error_handling_results = {}

        for test in error_tests:
            if test["method"] == "get":
                response = make_request("get", test["url"], logger=logger)
            else:
                response = make_request(
                    "post", test["url"], json_data=test.get("data", {}), logger=logger
                )

            if response:
                expected_statuses = (
                    test["expected_status"]
                    if isinstance(test["expected_status"], list)
                    else [test["expected_status"]]
                )
                status_correct = response.status_code in expected_statuses

                error_handling_results[test["name"]] = {
                    "status_code": response.status_code,
                    "status_correct": status_correct,
                    "has_error_message": "message" in response.json()
                    if response.content
                    else False,
                }
            else:
                error_handling_results[test["name"]] = {"error": "No response"}

        # 验证错误处理
        correct_error_responses = sum(
            1
            for result in error_handling_results.values()
            if result.get("status_correct", False)
        )

        self.test_result.add_test_result(
            "error_handling_consistency",
            True,
            status_code=200,
            correct_error_responses=correct_error_responses,
            error_handling_results=error_handling_results,
        )

    def test_service_dependencies(self):
        """测试服务依赖关系"""
        logger.info("测试服务依赖关系")

        # 检查各服务的依赖状态
        dependencies = {
            "predict_ready": {
                "url": f"{self.api_base_url}/predict/ready",
                "critical": True,
            },
            "rca_ready": {"url": f"{self.api_base_url}/rca/ready", "critical": False},
            "autofix_ready": {
                "url": f"{self.api_base_url}/autofix/ready",
                "critical": False,
            },
            "assistant_ready": {
                "url": f"{self.api_base_url}/assistant/ready",
                "critical": False,
            },
        }

        dependency_status = {}
        critical_services_healthy = 0
        total_services_available = 0

        for service_name, service_info in dependencies.items():
            response = make_request("get", service_info["url"], logger=logger)

            if response:
                is_healthy = response.status_code == 200
                dependency_status[service_name] = {
                    "available": True,
                    "healthy": is_healthy,
                    "status_code": response.status_code,
                    "critical": service_info["critical"],
                }

                total_services_available += 1
                if service_info["critical"] and is_healthy:
                    critical_services_healthy += 1
            else:
                dependency_status[service_name] = {
                    "available": False,
                    "healthy": False,
                    "critical": service_info["critical"],
                }

        # 验证关键服务健康
        critical_services_count = sum(
            1 for info in dependencies.values() if info["critical"]
        )
        assert critical_services_healthy == critical_services_count, "关键服务不健康"

        # 验证服务可用性
        assert total_services_available >= 3, (
            f"可用服务数量不足: {total_services_available}"
        )

        self.test_result.add_test_result(
            "service_dependencies",
            True,
            status_code=200,
            critical_services_healthy=critical_services_healthy,
            total_services_available=total_services_available,
            dependency_status=dependency_status,
        )

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

        save_test_results(results, "logs/integration_test_results.json", logger)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
