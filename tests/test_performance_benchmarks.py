#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.prediction.predictor import PredictionService
from app.main import create_app


@pytest.fixture
def client():
    """创建测试客户端"""
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def prediction_service():
    """创建预测服务实例"""
    with patch("app.core.prediction.predictor.PrometheusService"):
        service = PredictionService()
        return service


class TestAPIPerformance:
    """API性能测试"""

    def test_health_endpoint_performance(self, client):
        """测试健康检查端点性能"""
        # 单次请求性能
        start_time = time.time()
        response = client.get("/api/v1/health")
        end_time = time.time()

        assert response.status_code == 200
        assert (end_time - start_time) < 2.0  # 2秒内响应（考虑启动时间）

        # 批量请求性能
        response_times = []
        for _ in range(100):
            start_time = time.time()
            response = client.get("/api/v1/health")
            end_time = time.time()

            response_times.append(end_time - start_time)
            assert response.status_code == 200

        # 统计分析
        avg_time = statistics.mean(response_times)
        p95_time = sorted(response_times)[94]  # 95th percentile
        max_time = max(response_times)

        assert avg_time < 0.2  # 平均200ms内
        assert p95_time < 0.5  # 95%的请求在500ms内
        assert max_time < 1.0  # 最慢请求在1秒内

    def test_prediction_endpoint_performance(self, client):
        """测试预测端点性能"""
        # 改为测试趋势列表接口的性能
        # 单次
        start_time = time.time()
        response = client.get(
            "/api/v1/predict/trend/list",
            params={"hours_ahead": 6, "current_qps": 120.0},
        )
        end_time = time.time()
        assert response.status_code == 200
        assert (end_time - start_time) < 2.0

        # 多次稳定性
        response_times = []
        for i in range(20):
            start_time = time.time()
            response = client.get(
                "/api/v1/predict/trend/list",
                params={"hours_ahead": 6, "current_qps": 80.0 + i},
            )
            end_time = time.time()
            response_times.append(end_time - start_time)
            assert response.status_code == 200

        # 性能稳定性验证
        avg_time = statistics.mean(response_times)
        std_dev = statistics.stdev(response_times)

        assert avg_time < 1.5  # 平均1.5秒内
        assert std_dev < 0.5  # 标准差小于500ms（性能稳定）

    def test_concurrent_api_requests(self, client):
        """测试并发API请求性能"""

        def make_request(request_id):
            start_time = time.time()
            response = client.get("/api/v1/health")
            end_time = time.time()

            return {
                "id": request_id,
                "status_code": response.status_code,
                "response_time": end_time - start_time,
            }

        # 10个并发请求
        concurrent_requests = 10

        start_time = time.time()
        with ThreadPoolExecutor(max_workers=concurrent_requests) as executor:
            futures = [
                executor.submit(make_request, i) for i in range(concurrent_requests)
            ]
            results = [future.result() for future in as_completed(futures)]
        end_time = time.time()

        total_time = end_time - start_time

        # 验证结果
        assert len(results) == concurrent_requests
        for result in results:
            assert result["status_code"] == 200
            assert result["response_time"] < 1.0  # 单个请求1秒内

        # 并发处理应该比串行快
        assert total_time < concurrent_requests * 0.5  # 并发效率验证

    def test_high_load_prediction_requests(self, client):
        """测试高负载预测请求"""

        def make_prediction_request(request_id):
            start_time = time.time()
            response = client.get(
                "/api/v1/predict/trend/list",
                params={"hours_ahead": 6, "current_qps": 50.0 + request_id},
            )
            end_time = time.time()
            return {
                "id": request_id,
                "status_code": response.status_code,
                "response_time": end_time - start_time,
                "success": response.status_code == 200,
            }

        # 50个高负载请求
        high_load_requests = 50

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [
                executor.submit(make_prediction_request, i)
                for i in range(high_load_requests)
            ]
            results = [future.result() for future in as_completed(futures)]

        # 统计结果
        successful_requests = sum(1 for r in results if r["success"])
        response_times = [r["response_time"] for r in results if r["success"]]

        # 性能要求
        success_rate = successful_requests / high_load_requests
        assert success_rate >= 0.95  # 95%成功率

        if response_times:
            avg_response_time = statistics.mean(response_times)
            assert avg_response_time < 3.0  # 平均响应时间3秒内


class TestPredictionServicePerformance:
    """预测服务性能测试"""

    def test_single_prediction_performance(self, prediction_service):
        """测试单次预测性能"""
        start_time = time.time()

        result = prediction_service.predict(
            namespace="default", deployment="test-app", current_qps=100.0
        )

        end_time = time.time()
        execution_time = end_time - start_time

        assert result is not None
        assert execution_time < 0.5  # 500ms内完成

    def test_batch_prediction_performance(self, prediction_service):
        """测试批量预测性能"""
        deployments = [f"app-{i}" for i in range(50)]

        start_time = time.time()

        results = []
        for deployment in deployments:
            result = prediction_service.predict(
                namespace="default",
                deployment=deployment,
                current_qps=50.0 + len(results),
            )
            results.append(result)

        end_time = time.time()
        total_time = end_time - start_time

        assert len(results) == 50
        assert all(r is not None for r in results)
        assert total_time < 10.0  # 10秒内完成50个预测

        # 平均每个预测时间
        avg_time_per_prediction = total_time / 50
        assert avg_time_per_prediction < 0.2  # 平均200ms每个

    def test_concurrent_predictions_performance(self, prediction_service):
        """测试并发预测性能"""

        def make_prediction(request_id):
            start_time = time.time()

            result = prediction_service.predict(
                namespace="concurrent-test",
                deployment=f"app-{request_id}",
                current_qps=75.0 + request_id,
            )

            end_time = time.time()

            return {
                "id": request_id,
                "result": result,
                "execution_time": end_time - start_time,
            }

        # 20个并发预测
        concurrent_count = 20

        start_time = time.time()
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(make_prediction, i) for i in range(concurrent_count)
            ]
            results = [future.result() for future in as_completed(futures)]
        end_time = time.time()

        total_time = end_time - start_time

        # 验证结果
        assert len(results) == concurrent_count
        for result in results:
            assert result["result"] is not None
            assert result["execution_time"] < 1.0  # 每个预测1秒内

        # 并发处理效率
        assert total_time < concurrent_count * 0.3  # 并发带来的性能提升

    def test_prediction_memory_usage(self, prediction_service):
        """测试预测过程中的内存使用"""
        import gc

        import psutil

        process = psutil.Process()
        initial_memory = process.memory_info().rss

        # 执行大量预测
        for i in range(100):
            result = prediction_service.predict(
                namespace="memory-test", deployment=f"app-{i}", current_qps=100.0 + i
            )
            assert result is not None

        # 强制垃圾回收
        gc.collect()

        final_memory = process.memory_info().rss
        memory_increase = final_memory - initial_memory

        # 内存增长应该控制在合理范围内（50MB）
        max_memory_increase = 50 * 1024 * 1024  # 50MB
        assert memory_increase < max_memory_increase

    def test_prediction_under_stress(self, prediction_service):
        """测试压力条件下的预测性能"""

        def stress_prediction():
            results = []
            start_time = time.time()

            # 快速连续预测
            for i in range(10):
                result = prediction_service.predict(
                    namespace="stress-test",
                    deployment=f"stressed-app-{threading.current_thread().ident}-{i}",
                    current_qps=200.0 + i * 10,
                )
                results.append(result)

            end_time = time.time()
            return {
                "results": results,
                "execution_time": end_time - start_time,
                "thread_id": threading.current_thread().ident,
            }

        # 5个线程同时进行压力测试
        threads = []
        results = []

        start_time = time.time()
        for _ in range(5):
            thread = threading.Thread(
                target=lambda: results.append(stress_prediction())
            )
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()
        end_time = time.time()

        total_time = end_time - start_time

        # 验证所有线程都成功完成
        assert len(results) == 5
        for thread_result in results:
            assert len(thread_result["results"]) == 10
            assert all(r is not None for r in thread_result["results"])
            assert thread_result["execution_time"] < 5.0  # 每个线程5秒内完成

        # 整体压力测试时间
        assert total_time < 8.0  # 8秒内完成所有压力测试


class TestMemoryLeakDetection:
    """内存泄漏检测测试"""

    def test_repeated_api_calls_memory_stability(self, client):
        """测试重复API调用的内存稳定性"""
        import gc

        import psutil

        process = psutil.Process()
        memory_samples = []

        # 收集基线内存使用
        gc.collect()
        baseline_memory = process.memory_info().rss
        memory_samples.append(baseline_memory)

        # 执行大量API调用
        for i in range(200):
            response = client.get("/api/v1/health")
            assert response.status_code == 200

            # 每50次调用记录一次内存使用
            if i % 50 == 49:
                gc.collect()
                current_memory = process.memory_info().rss
                memory_samples.append(current_memory)

        # 分析内存使用趋势
        memory_increases = [mem - baseline_memory for mem in memory_samples[1:]]

        # 内存增长应该稳定（没有明显的泄漏趋势）
        max_increase = max(memory_increases)
        assert max_increase < 20 * 1024 * 1024  # 最大增长不超过20MB

        # 检查是否有持续增长趋势（简单线性趋势检测）
        if len(memory_increases) >= 3:
            trend = (memory_increases[-1] - memory_increases[0]) / len(memory_increases)
            assert abs(trend) < 1024 * 1024  # 趋势斜率小于1MB每次采样

    def test_prediction_service_memory_stability(self, prediction_service):
        """测试预测服务的内存稳定性"""
        import gc

        import psutil

        process = psutil.Process()

        # 基线内存
        gc.collect()
        baseline_memory = process.memory_info().rss

        # 大量预测操作
        for i in range(300):
            result = prediction_service.predict(
                namespace="memory-stability-test",
                deployment=f"app-{i % 20}",  # 循环使用20个不同的deployment名称
                current_qps=50.0 + (i % 100),
            )
            assert result is not None

        # 强制垃圾回收并检查内存
        gc.collect()
        final_memory = process.memory_info().rss
        memory_increase = final_memory - baseline_memory

        # 300次预测操作后，内存增长应该控制在合理范围内
        max_acceptable_increase = 30 * 1024 * 1024  # 30MB
        assert memory_increase < max_acceptable_increase


class TestScalabilityBenchmarks:
    """可扩展性基准测试"""

    @pytest.mark.parametrize("concurrent_users", [1, 5, 10, 20])
    def test_api_scalability_with_users(self, client, concurrent_users):
        """测试不同并发用户数下的API可扩展性"""

        def simulate_user():
            user_response_times = []

            # 每个用户发送10个请求
            for _ in range(10):
                start_time = time.time()
                response = client.get("/api/v1/health")
                end_time = time.time()

                user_response_times.append(end_time - start_time)
                assert response.status_code == 200

            return user_response_times

        # 执行并发测试
        start_time = time.time()
        with ThreadPoolExecutor(max_workers=concurrent_users) as executor:
            futures = [executor.submit(simulate_user) for _ in range(concurrent_users)]
            all_response_times = []
            for future in as_completed(futures):
                all_response_times.extend(future.result())
        end_time = time.time()

        total_time = end_time - start_time

        # 分析结果
        avg_response_time = statistics.mean(all_response_times)
        p95_response_time = sorted(all_response_times)[
            int(0.95 * len(all_response_times))
        ]

        # 性能要求随并发用户数调整
        max_avg_time = 0.1 + (concurrent_users * 0.02)  # 基础100ms + 每用户20ms
        max_p95_time = 0.2 + (concurrent_users * 0.05)  # 基础200ms + 每用户50ms

        assert avg_response_time < max_avg_time
        assert p95_response_time < max_p95_time

        # 吞吐量验证
        total_requests = concurrent_users * 10
        throughput = total_requests / total_time
        min_throughput = max(10, 50 - concurrent_users * 2)  # 最小10 QPS，随并发度降低
        assert throughput > min_throughput

    @pytest.mark.parametrize("request_load", [10, 50, 100, 200])
    def test_prediction_scalability_with_load(self, client, request_load):
        """测试不同请求负载下的预测API可扩展性"""
        # 使用趋势列表接口模拟负载
        successful_requests = 0
        total_response_time = 0

        start_time = time.time()

        # 使用线程池处理不同负载
        max_workers = min(10, request_load // 5 + 1)

        def make_prediction_request(request_id):
            req_start = time.time()
            response = client.get(
                "/api/v1/predict/trend/list",
                params={"hours_ahead": 3, "current_qps": 100.0},
            )
            req_end = time.time()
            return {
                "success": response.status_code == 200,
                "response_time": req_end - req_start,
            }

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(make_prediction_request, i) for i in range(request_load)
            ]

            for future in as_completed(futures):
                result = future.result()
                if result["success"]:
                    successful_requests += 1
                    total_response_time += result["response_time"]

        end_time = time.time()
        total_time = end_time - start_time

        # 性能指标
        success_rate = successful_requests / request_load
        avg_response_time = (
            total_response_time / successful_requests if successful_requests > 0 else 0
        )
        throughput = successful_requests / total_time

        # 性能要求
        min_success_rate = max(0.8, 1.0 - request_load * 0.001)  # 随负载降低成功率要求
        max_avg_response_time = 2.0 + (request_load * 0.01)  # 响应时间随负载增加
        min_throughput = max(5, 30 - request_load * 0.1)  # 吞吐量随负载降低

        assert success_rate >= min_success_rate
        assert avg_response_time <= max_avg_response_time
        assert throughput >= min_throughput


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-x"])  # -x: stop on first failure
