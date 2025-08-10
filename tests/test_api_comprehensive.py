#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 全面的API接口测试 - 使用pytest标准格式，确保高测试覆盖率
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
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
    with patch('app.services.kubernetes.KubernetesService') as mock:
        mock_instance = Mock()
        mock.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def mock_prometheus_service():
    """模拟Prometheus服务"""
    with patch('app.services.prometheus.PrometheusService') as mock:
        mock_instance = Mock()
        mock.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def mock_llm_service():
    """模拟LLM服务"""
    with patch('app.services.llm.LLMService') as mock:
        mock_instance = Mock()
        mock.return_value = mock_instance
        yield mock_instance


class TestRootAPI:
    """测试根路径API"""
    
    def test_root_endpoint(self, client):
        """测试根路径端点"""
        response = client.get("/")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["message"] == "AIOps Platform API"
        assert "data" in data
        assert data["data"]["service"] == "AIOps Platform"
        assert data["data"]["version"] == "1.0.0"
        assert data["data"]["status"] == "running"


class TestHealthAPI:
    """测试健康检查API"""
    
    def test_health_check(self, client):
        """测试基础健康检查"""
        response = client.get("/api/v1/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert "data" in data
        assert "status" in data["data"]
    
    def test_health_detailed(self, client):
        """测试详细健康检查"""
        response = client.get("/api/v1/health/detailed")
        
        assert response.status_code in [200, 503]  # 可能因服务不可用返回503
        data = response.json()
        assert "data" in data
    
    @patch('app.api.routes.health.KubernetesService')
    def test_health_k8s(self, mock_k8s, client):
        """测试Kubernetes健康检查"""
        # 模拟成功的Kubernetes连接
        mock_k8s.return_value.check_connectivity.return_value = True
        
        response = client.get("/api/v1/health/k8s")
        
        assert response.status_code in [200, 503]
        data = response.json()
        assert "data" in data
    
    @patch('app.api.routes.health.PrometheusService')
    def test_health_prometheus(self, mock_prometheus, client):
        """测试Prometheus健康检查"""
        # 模拟成功的Prometheus连接
        mock_prometheus.return_value.check_connectivity.return_value = True
        
        response = client.get("/api/v1/health/prometheus")
        
        assert response.status_code in [200, 503]
        data = response.json()
        assert "data" in data
    
    def test_health_system(self, client):
        """测试系统健康检查"""
        response = client.get("/api/v1/health/system")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert "data" in data
        assert "cpu_percent" in data["data"]
        assert "memory_percent" in data["data"]
        assert "disk_usage" in data["data"]


class TestPredictionAPI:
    """测试预测API"""
    
    @patch('app.core.prediction.predictor.Predictor')
    def test_prediction_health(self, mock_predictor, client):
        """测试预测服务健康检查"""
        mock_predictor.return_value.health_check.return_value = {"status": "healthy"}
        
        response = client.get("/api/v1/predict/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.prediction.predictor.Predictor')
    def test_prediction_post(self, mock_predictor, client):
        """测试POST预测接口"""
        # 模拟预测结果
        mock_predictor.return_value.predict.return_value = {
            "predicted_replicas": 5,
            "current_replicas": 3,
            "confidence": 0.85
        }
        
        payload = {
            "namespace": "default",
            "deployment": "nginx",
            "metrics": ["cpu_usage", "memory_usage"],
            "duration_minutes": 60
        }
        
        response = client.post("/api/v1/predict", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert "data" in data
    
    @patch('app.core.prediction.predictor.Predictor')
    def test_prediction_get(self, mock_predictor, client):
        """测试GET预测接口"""
        mock_predictor.return_value.predict.return_value = {
            "predicted_replicas": 3,
            "current_replicas": 2,
            "confidence": 0.75
        }
        
        response = client.get(
            "/api/v1/predict",
            params={
                "namespace": "default",
                "deployment": "test-app",
                "duration_minutes": 30
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    def test_prediction_invalid_params(self, client):
        """测试无效参数的预测请求"""
        payload = {
            "namespace": "",  # 空的命名空间
            "deployment": "test",
            "duration_minutes": -10  # 无效的时长
        }
        
        response = client.post("/api/v1/predict", json=payload)
        
        assert response.status_code == 422  # 验证错误
    
    @patch('app.core.prediction.predictor.Predictor')
    def test_trend_prediction(self, mock_predictor, client):
        """测试趋势预测"""
        mock_predictor.return_value.predict_trend.return_value = {
            "trend": "increasing",
            "slope": 0.05,
            "r_squared": 0.8
        }
        
        response = client.get(
            "/api/v1/predict/trend",
            params={
                "namespace": "default",
                "deployment": "app",
                "metric": "cpu_usage",
                "hours": 2
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.prediction.predictor.Predictor')
    def test_model_info(self, mock_predictor, client):
        """测试模型信息接口"""
        mock_predictor.return_value.get_model_info.return_value = {
            "model_type": "linear_regression",
            "features": ["cpu_usage", "memory_usage"],
            "accuracy": 0.92
        }
        
        response = client.get("/api/v1/predict/model/info")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0


class TestRCAAPI:
    """测试根因分析API"""
    
    def test_rca_health(self, client):
        """测试RCA健康检查"""
        response = client.get("/api/v1/rca/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.rca.analyzer.RCAAnalyzer')
    def test_get_metrics(self, mock_analyzer, client):
        """测试获取可用指标"""
        mock_analyzer.return_value.get_available_metrics.return_value = [
            "cpu_usage",
            "memory_usage",
            "network_io"
        ]
        
        response = client.get("/api/v1/rca/metrics")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert "data" in data
    
    @patch('app.core.rca.topology.graph.TopologyGraph')
    def test_topology_graph(self, mock_graph, client):
        """测试拓扑图接口"""
        mock_graph.return_value.get_graph_data.return_value = {
            "nodes": ["pod1", "pod2"],
            "edges": [{"source": "pod1", "target": "pod2"}]
        }
        
        response = client.get(
            "/api/v1/rca/topology",
            params={"namespace": "default"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.rca.analyzer.RCAAnalyzer')
    def test_anomaly_detection(self, mock_analyzer, client):
        """测试异常检测"""
        mock_analyzer.return_value.detect_anomalies.return_value = {
            "anomalies": [
                {
                    "metric": "cpu_usage",
                    "timestamp": "2024-01-01T12:00:00Z",
                    "value": 95.0,
                    "threshold": 80.0
                }
            ]
        }
        
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=1)
        
        payload = {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "namespace": "default",
            "metrics": ["cpu_usage"]
        }
        
        response = client.post("/api/v1/rca/detect", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.rca.correlator.Correlator')
    def test_correlation_analysis(self, mock_correlator, client):
        """测试相关性分析"""
        mock_correlator.return_value.analyze_correlations.return_value = {
            "correlations": [
                {
                    "metric1": "cpu_usage",
                    "metric2": "response_time",
                    "correlation": 0.85
                }
            ]
        }
        
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=1)
        
        payload = {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "namespace": "default",
            "metrics": ["cpu_usage", "response_time"]
        }
        
        response = client.post("/api/v1/rca/correlate", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.rca.analyzer.RCAAnalyzer')
    def test_root_cause_analysis(self, mock_analyzer, client):
        """测试完整根因分析"""
        mock_analyzer.return_value.analyze.return_value = {
            "root_causes": [
                {
                    "component": "pod/nginx-123",
                    "issue": "高CPU使用率",
                    "confidence": 0.9,
                    "recommendations": ["增加资源限制"]
                }
            ]
        }
        
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=2)
        
        payload = {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "namespace": "default",
            "incident_description": "应用响应缓慢"
        }
        
        response = client.post("/api/v1/rca/analyze", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0


class TestAutofixAPI:
    """测试自动修复API"""
    
    def test_autofix_health(self, client):
        """测试自动修复健康检查"""
        response = client.get("/api/v1/autofix/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.agents.detector.Detector')
    def test_diagnose_cluster(self, mock_detector, client):
        """测试集群诊断"""
        mock_detector.return_value.diagnose.return_value = {
            "issues": [
                {
                    "type": "resource_issue",
                    "severity": "high",
                    "description": "Pod内存不足"
                }
            ]
        }
        
        response = client.post(
            "/api/v1/autofix/diagnose",
            json={"namespace": "default"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.agents.coordinator.Coordinator')
    def test_autofix_deployment(self, mock_coordinator, client):
        """测试自动修复部署"""
        mock_coordinator.return_value.fix_deployment.return_value = {
            "status": "success",
            "actions_taken": ["增加资源限制"],
            "fixed_issues": 1
        }
        
        response = client.post(
            "/api/v1/autofix/fix",
            json={
                "namespace": "default",
                "deployment": "nginx",
                "issues": ["resource_issue"]
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.agents.notifier.Notifier')
    def test_notification_webhook(self, mock_notifier, client):
        """测试通知webhook"""
        mock_notifier.return_value.send_notification.return_value = True
        
        payload = {
            "type": "fix_completed",
            "namespace": "default",
            "deployment": "test-app",
            "message": "修复完成"
        }
        
        response = client.post("/api/v1/autofix/notify", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.agents.coordinator.Coordinator')
    def test_workflow_execution(self, mock_coordinator, client):
        """测试工作流执行"""
        mock_coordinator.return_value.execute_workflow.return_value = {
            "workflow_id": "wf_123",
            "status": "running",
            "steps": [
                {"name": "detect", "status": "completed"},
                {"name": "fix", "status": "running"}
            ]
        }
        
        payload = {
            "workflow_type": "full_autofix",
            "namespace": "default",
            "target": "deployment/nginx"
        }
        
        response = client.post("/api/v1/autofix/workflow", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0


class TestMultiAgentAPI:
    """测试多Agent协作API"""
    
    def test_multi_agent_health(self, client):
        """测试多Agent系统健康检查"""
        response = client.get("/api/v1/multi-agent/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.agents.supervisor.Supervisor')
    def test_agent_status(self, mock_supervisor, client):
        """测试获取Agent状态"""
        mock_supervisor.return_value.get_agents_status.return_value = {
            "agents": [
                {"name": "detector", "status": "active", "tasks": 2},
                {"name": "fixer", "status": "idle", "tasks": 0}
            ]
        }
        
        response = client.get("/api/v1/multi-agent/status")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.agents.supervisor.Supervisor')
    def test_task_execution(self, mock_supervisor, client):
        """测试任务执行"""
        mock_supervisor.return_value.execute_task.return_value = {
            "task_id": "task_123",
            "status": "started",
            "assigned_agents": ["detector", "analyzer"]
        }
        
        payload = {
            "task_type": "incident_response",
            "priority": "high",
            "parameters": {
                "namespace": "default",
                "incident": "pod_crash"
            }
        }
        
        response = client.post("/api/v1/multi-agent/execute", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.agents.supervisor.Supervisor')
    def test_coordination_status(self, mock_supervisor, client):
        """测试协调状态"""
        mock_supervisor.return_value.get_coordination_status.return_value = {
            "active_tasks": 3,
            "completed_tasks": 15,
            "agent_utilization": 0.65
        }
        
        response = client.get("/api/v1/multi-agent/coordination")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0


class TestAssistantAPI:
    """测试智能助手API"""
    
    def test_assistant_health(self, client):
        """测试助手健康检查"""
        response = client.get("/api/v1/assistant/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.agents.assistant.core.AssistantCore')
    def test_chat(self, mock_assistant, client):
        """测试聊天接口"""
        mock_assistant.return_value.process_query.return_value = {
            "response": "根据您的描述，建议检查Pod的资源配置。",
            "confidence": 0.85,
            "sources": ["k8s_best_practices.md"]
        }
        
        payload = {
            "query": "我的Pod一直重启，该怎么办？",
            "session_id": "session_123",
            "context": {"namespace": "default"}
        }
        
        response = client.post("/api/v1/assistant/chat", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.agents.assistant.retrieval.vector_store_manager.VectorStoreManager')
    def test_knowledge_search(self, mock_vector_store, client):
        """测试知识库搜索"""
        mock_vector_store.return_value.search.return_value = [
            {
                "content": "Pod重启通常由内存不足引起",
                "score": 0.9,
                "source": "troubleshooting_guide.md"
            }
        ]
        
        payload = {
            "query": "Pod重启问题",
            "top_k": 5
        }
        
        response = client.post("/api/v1/assistant/search", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0


class TestStorageAPI:
    """测试存储API"""
    
    def test_storage_health(self, client):
        """测试存储健康检查"""
        response = client.get("/api/v1/storage/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.agents.assistant.storage.document_loader.DocumentLoader')
    def test_upload_document(self, mock_loader, client):
        """测试文档上传"""
        mock_loader.return_value.load_document.return_value = {
            "document_id": "doc_123",
            "chunks": 5,
            "indexed": True
        }
        
        # 模拟文件上传
        files = {"file": ("test.md", "# 测试文档\n这是一个测试文档。", "text/markdown")}
        
        response = client.post("/api/v1/storage/upload", files=files)
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.agents.assistant.storage.document_loader.DocumentLoader')
    def test_list_documents(self, mock_loader, client):
        """测试列出文档"""
        mock_loader.return_value.list_documents.return_value = [
            {
                "id": "doc_1",
                "name": "guide.md",
                "size": 1024,
                "created_at": "2024-01-01T12:00:00Z"
            }
        ]
        
        response = client.get("/api/v1/storage/documents")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
    
    @patch('app.core.agents.assistant.storage.document_loader.DocumentLoader')
    def test_delete_document(self, mock_loader, client):
        """测试删除文档"""
        mock_loader.return_value.delete_document.return_value = True
        
        response = client.delete("/api/v1/storage/documents/doc_123")
        
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0


# 错误处理测试
class TestErrorHandling:
    """测试错误处理"""
    
    def test_404_not_found(self, client):
        """测试404错误"""
        response = client.get("/api/v1/nonexistent")
        
        assert response.status_code == 404
    
    def test_422_validation_error(self, client):
        """测试422验证错误"""
        # 发送无效的JSON数据
        response = client.post("/api/v1/predict", json={"invalid": "data"})
        
        assert response.status_code == 422
    
    def test_500_server_error(self, client):
        """测试500服务器错误"""
        # 通过patch制造异常
        with patch('app.api.routes.health.get_system_info', side_effect=Exception("测试异常")):
            response = client.get("/api/v1/health/system")
            
            # 根据错误处理中间件的实现，可能返回200或500
            assert response.status_code in [200, 500]


# 性能测试
class TestPerformance:
    """测试性能相关"""
    
    def test_concurrent_requests(self, client):
        """测试并发请求"""
        import threading
        import time
        
        results = []
        
        def make_request():
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
            thread = threading.Thread(target=make_request)
            threads.append(thread)
            thread.start()
        
        # 等待所有线程完成
        for thread in threads:
            thread.join()
        
        # 验证所有请求都成功
        assert len(results) == 10
        for result in results:
            assert result["status_code"] == 200
            assert result["response_time"] < 5.0  # 响应时间应该小于5秒
    
    def test_response_time(self, client):
        """测试响应时间"""
        import time
        
        start_time = time.time()
        response = client.get("/api/v1/health")
        end_time = time.time()
        
        assert response.status_code == 200
        assert (end_time - start_time) < 2.0  # 响应时间应该小于2秒


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=app", "--cov-report=term-missing"])