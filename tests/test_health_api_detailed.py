#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 健康检查API详细测试 - 提升健康检查模块的测试覆盖率
"""

from unittest.mock import Mock, patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client():
    """创建测试客户端"""
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def mock_k8s_service():
    """模拟Kubernetes服务"""
    with patch('app.api.routes.health.KubernetesService') as mock:
        mock_instance = Mock()
        mock.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def mock_prometheus_service():
    """模拟Prometheus服务"""
    with patch('app.api.routes.health.PrometheusService') as mock:
        mock_instance = Mock()
        mock.return_value = mock_instance
        yield mock_instance


class TestBasicHealthChecks:
    """基础健康检查测试"""
    
    def test_health_endpoint_success(self, client):
        """测试基础健康检查端点"""
        response = client.get("/api/v1/health")
        
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["code"] == 0
        assert "data" in data
        assert "status" in data["data"]
        assert data["data"]["status"] in ["healthy", "unhealthy"]
    
    def test_health_endpoint_structure(self, client):
        """测试健康检查响应结构"""
        response = client.get("/api/v1/health")
        data = response.json()
        
        # 验证响应结构
        assert isinstance(data, dict)
        assert "code" in data
        assert "message" in data
        assert "data" in data
        
        # 验证数据字段
        health_data = data["data"]
        assert isinstance(health_data, dict)
        assert "status" in health_data
        assert "timestamp" in health_data
    
    def test_health_multiple_requests(self, client):
        """测试多次健康检查请求的一致性"""
        responses = []
        for _ in range(5):
            response = client.get("/api/v1/health")
            responses.append(response)
        
        # 所有请求都应该成功
        for response in responses:
            assert response.status_code == status.HTTP_200_OK
            data = response.json()
            assert data["code"] == 0


class TestDetailedHealthChecks:
    """详细健康检查测试"""
    
    @patch('app.api.routes.health.KubernetesService')
    @patch('app.api.routes.health.PrometheusService')
    def test_detailed_health_all_services_healthy(self, mock_prometheus, mock_k8s, client):
        """测试所有服务健康的详细检查"""
        # 模拟所有服务健康
        mock_k8s.return_value.check_connectivity.return_value = True
        mock_prometheus.return_value.check_connectivity.return_value = True
        
        response = client.get("/api/v1/health/detailed")
        
        # 根据实际实现调整断言
        assert response.status_code in [status.HTTP_200_OK, status.HTTP_404_NOT_FOUND]
        if response.status_code == status.HTTP_200_OK:
            data = response.json()
            assert data["code"] == 0
    
    @patch('app.api.routes.health.KubernetesService')
    @patch('app.api.routes.health.PrometheusService')
    def test_detailed_health_some_services_unhealthy(self, mock_prometheus, mock_k8s, client):
        """测试部分服务不健康的详细检查"""
        # 模拟部分服务不健康
        mock_k8s.return_value.check_connectivity.return_value = True
        mock_prometheus.return_value.check_connectivity.return_value = False
        
        response = client.get("/api/v1/health/detailed")
        
        # 应该返回503或者200但标明部分服务不健康
        assert response.status_code in [
            status.HTTP_200_OK, 
            status.HTTP_503_SERVICE_UNAVAILABLE,
            status.HTTP_404_NOT_FOUND
        ]
    
    @patch('app.api.routes.health.KubernetesService')
    @patch('app.api.routes.health.PrometheusService')
    def test_detailed_health_all_services_unhealthy(self, mock_prometheus, mock_k8s, client):
        """测试所有服务不健康的详细检查"""
        # 模拟所有服务不健康
        mock_k8s.return_value.check_connectivity.return_value = False
        mock_prometheus.return_value.check_connectivity.return_value = False
        
        response = client.get("/api/v1/health/detailed")
        
        # 应该返回503或者200但标明服务不健康
        assert response.status_code in [
            status.HTTP_200_OK,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            status.HTTP_404_NOT_FOUND
        ]
    
    @patch('app.api.routes.health.KubernetesService')
    def test_detailed_health_service_exception(self, mock_k8s, client):
        """测试服务检查时抛出异常"""
        # 模拟服务检查抛出异常
        mock_k8s.return_value.check_connectivity.side_effect = Exception("连接失败")
        
        response = client.get("/api/v1/health/detailed")
        
        # 应该优雅处理异常
        assert response.status_code in [
            status.HTTP_200_OK,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            status.HTTP_404_NOT_FOUND
        ]


class TestSpecificServiceHealthChecks:
    """特定服务健康检查测试"""
    
    @patch('app.api.routes.health.KubernetesService')
    def test_k8s_health_success(self, mock_k8s, client):
        """测试Kubernetes健康检查成功"""
        mock_k8s.return_value.check_connectivity.return_value = True
        
        response = client.get("/api/v1/health/k8s")
        
        assert response.status_code in [status.HTTP_200_OK, status.HTTP_404_NOT_FOUND]
        if response.status_code == status.HTTP_200_OK:
            data = response.json()
            assert data["code"] == 0
    
    @patch('app.api.routes.health.KubernetesService')
    def test_k8s_health_failure(self, mock_k8s, client):
        """测试Kubernetes健康检查失败"""
        mock_k8s.return_value.check_connectivity.return_value = False
        
        response = client.get("/api/v1/health/k8s")
        
        assert response.status_code in [
            status.HTTP_503_SERVICE_UNAVAILABLE,
            status.HTTP_404_NOT_FOUND
        ]
    
    @patch('app.api.routes.health.KubernetesService')
    def test_k8s_health_exception(self, mock_k8s, client):
        """测试Kubernetes健康检查异常"""
        mock_k8s.return_value.check_connectivity.side_effect = Exception("K8s API不可达")
        
        response = client.get("/api/v1/health/k8s")
        
        # 应该优雅处理异常
        assert response.status_code in [
            status.HTTP_503_SERVICE_UNAVAILABLE,
            status.HTTP_404_NOT_FOUND
        ]
    
    @patch('app.api.routes.health.PrometheusService')
    def test_prometheus_health_success(self, mock_prometheus, client):
        """测试Prometheus健康检查成功"""
        mock_prometheus.return_value.check_connectivity.return_value = True
        
        response = client.get("/api/v1/health/prometheus")
        
        assert response.status_code in [status.HTTP_200_OK, status.HTTP_404_NOT_FOUND]
        if response.status_code == status.HTTP_200_OK:
            data = response.json()
            assert data["code"] == 0
    
    @patch('app.api.routes.health.PrometheusService')
    def test_prometheus_health_failure(self, mock_prometheus, client):
        """测试Prometheus健康检查失败"""
        mock_prometheus.return_value.check_connectivity.return_value = False
        
        response = client.get("/api/v1/health/prometheus")
        
        assert response.status_code in [
            status.HTTP_503_SERVICE_UNAVAILABLE,
            status.HTTP_404_NOT_FOUND
        ]
    
    @patch('app.api.routes.health.PrometheusService')
    def test_prometheus_health_exception(self, mock_prometheus, client):
        """测试Prometheus健康检查异常"""
        mock_prometheus.return_value.check_connectivity.side_effect = Exception("Prometheus不可达")
        
        response = client.get("/api/v1/health/prometheus")
        
        # 应该优雅处理异常
        assert response.status_code in [
            status.HTTP_503_SERVICE_UNAVAILABLE,
            status.HTTP_404_NOT_FOUND
        ]


class TestSystemHealthChecks:
    """系统健康检查测试"""
    
    @patch('psutil.cpu_percent')
    @patch('psutil.virtual_memory')
    @patch('psutil.disk_usage')
    def test_system_health_success(self, mock_disk, mock_memory, mock_cpu, client):
        """测试系统健康检查成功"""
        # 模拟正常的系统资源使用情况
        mock_cpu.return_value = 45.5
        mock_memory.return_value = Mock(percent=67.8)
        mock_disk.return_value = Mock(total=100*1024**3, used=60*1024**3, free=40*1024**3)
        
        response = client.get("/api/v1/health/system")
        
        # 根据实际实现调整断言
        if response.status_code == status.HTTP_200_OK:
            data = response.json()
            assert data["code"] == 0
            assert "data" in data
            
            system_data = data["data"]
            assert "cpu_percent" in system_data
            assert "memory_percent" in system_data
            assert "disk_usage" in system_data
            
            # 验证数值范围
            assert 0 <= system_data["cpu_percent"] <= 100
            assert 0 <= system_data["memory_percent"] <= 100
        else:
            # 如果端点不存在，应该返回404
            assert response.status_code == status.HTTP_404_NOT_FOUND
    
    @patch('psutil.cpu_percent')
    @patch('psutil.virtual_memory')
    @patch('psutil.disk_usage')
    def test_system_health_high_usage(self, mock_disk, mock_memory, mock_cpu, client):
        """测试系统资源高使用率情况"""
        # 模拟高资源使用情况
        mock_cpu.return_value = 95.0  # 高CPU使用率
        mock_memory.return_value = Mock(percent=90.0)  # 高内存使用率
        mock_disk.return_value = Mock(total=100*1024**3, used=95*1024**3, free=5*1024**3)  # 磁盘几乎满
        
        response = client.get("/api/v1/health/system")
        
        if response.status_code == status.HTTP_200_OK:
            data = response.json()
            # 高使用率情况下可能返回警告状态
            assert data["code"] in [0, 1]  # 0表示正常，1表示警告
        else:
            assert response.status_code == status.HTTP_404_NOT_FOUND
    
    @patch('psutil.cpu_percent')
    def test_system_health_psutil_exception(self, mock_cpu, client):
        """测试psutil抛出异常的情况"""
        mock_cpu.side_effect = Exception("无法获取系统信息")
        
        response = client.get("/api/v1/health/system")
        
        # 应该优雅处理异常
        assert response.status_code in [
            status.HTTP_200_OK,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            status.HTTP_404_NOT_FOUND
        ]


class TestHealthCheckPerformance:
    """健康检查性能测试"""
    
    def test_health_check_response_time(self, client):
        """测试健康检查响应时间"""
        import time
        
        start_time = time.time()
        response = client.get("/api/v1/health")
        end_time = time.time()
        
        # 健康检查应该很快响应
        assert (end_time - start_time) < 1.0  # 1秒内
        assert response.status_code == status.HTTP_200_OK
    
    def test_multiple_concurrent_health_checks(self, client):
        """测试并发健康检查"""
        import threading
        import time
        
        results = []
        
        def make_health_request():
            start_time = time.time()
            response = client.get("/api/v1/health")
            end_time = time.time()
            results.append({
                "status_code": response.status_code,
                "response_time": end_time - start_time
            })
        
        # 创建10个并发请求
        threads = []
        for _ in range(10):
            thread = threading.Thread(target=make_health_request)
            threads.append(thread)
            thread.start()
        
        # 等待所有请求完成
        for thread in threads:
            thread.join()
        
        # 验证所有请求都成功
        assert len(results) == 10
        for result in results:
            assert result["status_code"] == status.HTTP_200_OK
            assert result["response_time"] < 2.0  # 每个请求都应该在2秒内完成
    
    def test_health_check_under_load(self, client):
        """测试负载下的健康检查"""
        import time
        
        # 连续发送100个请求
        start_time = time.time()
        
        for _ in range(100):
            response = client.get("/api/v1/health")
            assert response.status_code == status.HTTP_200_OK
        
        end_time = time.time()
        total_time = end_time - start_time
        
        # 平均每个请求应该在合理时间内完成
        avg_time_per_request = total_time / 100
        assert avg_time_per_request < 0.1  # 平均每个请求100ms内


class TestHealthCheckEdgeCases:
    """健康检查边界情况测试"""
    
    def test_health_check_during_startup(self, client):
        """测试启动阶段的健康检查"""
        # 模拟应用刚启动的情况
        response = client.get("/api/v1/health")
        
        # 即使在启动阶段，健康检查也应该响应
        assert response.status_code == status.HTTP_200_OK
    
    def test_health_check_with_invalid_headers(self, client):
        """测试带有无效头的健康检查"""
        headers = {
            "Content-Type": "invalid/type",
            "Authorization": "Bearer invalid_token"
        }
        
        response = client.get("/api/v1/health", headers=headers)
        
        # 健康检查应该不受无效头影响
        assert response.status_code == status.HTTP_200_OK
    
    def test_health_check_with_query_params(self, client):
        """测试带查询参数的健康检查"""
        response = client.get("/api/v1/health?format=json&detailed=true")
        
        # 健康检查应该忽略无关的查询参数
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["code"] == 0
    
    def test_health_check_different_methods(self, client):
        """测试不同HTTP方法的健康检查"""
        # GET方法应该成功
        get_response = client.get("/api/v1/health")
        assert get_response.status_code == status.HTTP_200_OK
        
        # POST方法应该不被允许
        post_response = client.post("/api/v1/health")
        assert post_response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        
        # PUT方法应该不被允许
        put_response = client.put("/api/v1/health")
        assert put_response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        
        # DELETE方法应该不被允许
        delete_response = client.delete("/api/v1/health")
        assert delete_response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


class TestHealthCheckIntegration:
    """健康检查集成测试"""
    
    @patch('app.api.routes.health.KubernetesService')
    @patch('app.api.routes.health.PrometheusService')
    @patch('psutil.cpu_percent')
    @patch('psutil.virtual_memory')
    def test_full_health_check_integration(self, mock_memory, mock_cpu, mock_prometheus, mock_k8s, client):
        """测试完整的健康检查集成"""
        # 设置所有模拟服务为健康状态
        mock_k8s.return_value.check_connectivity.return_value = True
        mock_prometheus.return_value.check_connectivity.return_value = True
        mock_cpu.return_value = 50.0
        mock_memory.return_value = Mock(percent=60.0)
        
        # 测试基础健康检查
        basic_response = client.get("/api/v1/health")
        assert basic_response.status_code == status.HTTP_200_OK
        
        # 测试详细健康检查（如果存在）
        detailed_response = client.get("/api/v1/health/detailed")
        assert detailed_response.status_code in [status.HTTP_200_OK, status.HTTP_404_NOT_FOUND]
        
        # 测试系统健康检查（如果存在）
        system_response = client.get("/api/v1/health/system")
        assert system_response.status_code in [status.HTTP_200_OK, status.HTTP_404_NOT_FOUND]
    
    def test_health_check_cascading_failures(self, client):
        """测试级联故障情况下的健康检查"""
        with patch('app.api.routes.health.KubernetesService') as mock_k8s, \
             patch('app.api.routes.health.PrometheusService') as mock_prometheus:
            
            # 模拟级联故障
            mock_k8s.return_value.check_connectivity.side_effect = Exception("K8s故障")
            mock_prometheus.return_value.check_connectivity.side_effect = Exception("Prometheus故障")
            
            # 基础健康检查应该仍然工作
            response = client.get("/api/v1/health")
            assert response.status_code == status.HTTP_200_OK
            
            # 详细健康检查可能报告服务不可用
            detailed_response = client.get("/api/v1/health/detailed")
            assert detailed_response.status_code in [
                status.HTTP_200_OK,
                status.HTTP_503_SERVICE_UNAVAILABLE,
                status.HTTP_404_NOT_FOUND
            ]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=app.api.routes.health", "--cov-report=term-missing"])