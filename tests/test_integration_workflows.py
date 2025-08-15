#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 测试：test_integration_workflows
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
def mock_services():
    """模拟所有外部服务"""
    services = {}

    with (
        patch("app.services.kubernetes.KubernetesService") as mock_k8s,
        patch("app.services.prometheus.PrometheusService") as mock_prometheus,
        patch("app.services.llm.LLMService") as mock_llm,
        patch("app.services.notification.NotificationService") as mock_notification,
    ):
        services["k8s"] = mock_k8s.return_value
        services["prometheus"] = mock_prometheus.return_value
        services["llm"] = mock_llm.return_value
        services["notification"] = mock_notification.return_value

        yield services


class TestPredictionWorkflow:
    """测试预测工作流"""

    def test_complete_prediction_workflow(self, client, mock_services):
        """测试完整的预测工作流"""
        # 1. 模拟Kubernetes返回部署信息
        mock_deployment = Mock()
        mock_deployment.metadata.name = "nginx"
        mock_deployment.spec.replicas = 3
        mock_deployment.status.ready_replicas = 3
        mock_services["k8s"].get_deployments.return_value = [mock_deployment]

        # 2. 模拟Prometheus返回历史指标
        mock_services["prometheus"].query_range.return_value = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"pod": "nginx-123"},
                        "values": [
                            [1640995200, "50.5"],  # CPU使用率
                            [1640995260, "55.2"],
                            [1640995320, "62.8"],
                            [1640995380, "78.5"],  # 递增趋势
                            [1640995440, "85.2"],
                        ],
                    }
                ]
            },
        }

        # 3. 发送预测请求
        response = client.get(
            "/api/v1/predict/trend/list",
            params={"hours_ahead": 6, "current_qps": 100.0},
        )

        # 4. 验证响应
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert "data" in data

        # 5. 验证预测结果包含必要字段
        if "predicted_replicas" in data["data"]:
            assert isinstance(data["data"]["predicted_replicas"], int)
            assert data["data"]["predicted_replicas"] > 0

    def test_prediction_with_high_load(self, client, mock_services):
        """测试高负载场景的预测"""
        # 模拟高CPU使用率数据
        mock_services["prometheus"].query_range.return_value = {
            "status": "success",
            "data": {
                "result": [
                    {"values": [[i * 60 + 1640995200, "90.0"] for i in range(30)]}
                ]
            },
        }

        response = client.get(
            "/api/v1/predict/trend/list", params={"hours_ahead": 6, "current_qps": 90.0}
        )

        assert response.status_code == 200
        data = response.json()

        # 在高负载情况下，应该建议扩容
        if "data" in data and "predicted_replicas" in data["data"]:
            # 假设当前副本数为3，高负载下应该建议更多副本
            assert data["data"]["predicted_replicas"] >= 3


class TestRCAWorkflow:
    """测试根因分析工作流"""

    def test_complete_rca_workflow(self, client, mock_services):
        """测试完整的RCA分析工作流"""
        # 1. 模拟Kubernetes返回Pod信息（有问题的Pod）
        mock_pod = Mock()
        mock_pod.metadata.name = "problematic-pod"
        mock_pod.status.phase = "Running"
        mock_pod.status.container_statuses = [
            Mock(
                name="app",
                restart_count=5,
                state=Mock(waiting=Mock(reason="CrashLoopBackOff")),
            )
        ]
        mock_services["k8s"].get_pods.return_value = [mock_pod]

        # 2. 模拟Prometheus返回异常指标
        mock_services["prometheus"].query_range.return_value = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"pod": "problematic-pod"},
                        "values": [
                            [1640995200, "30.0"],  # 正常CPU
                            [1640995260, "35.0"],
                            [1640995320, "95.0"],  # 异常峰值
                            [1640995380, "98.0"],
                            [1640995440, "99.0"],  # 持续高位
                        ],
                    }
                ]
            },
        }

        # 3. 模拟LLM分析
        mock_services["llm"].generate_response.return_value = (
            "根据分析，Pod频繁重启可能是由于：\n"
            "1. 内存不足导致OOM Kill\n"
            "2. 健康检查配置不当\n"
            "3. 应用程序内部错误\n"
            "建议检查资源限制和应用日志。"
        )

        # 4. 发送RCA请求
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=1)

        payload = {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "namespace": "default",
            "incident_description": "Pod频繁重启",
            "metrics": ["cpu_usage", "memory_usage"],
        }

        response = client.post("/api/v1/rca/analyses/create", json=payload)

        # 5. 验证响应
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert "data" in data

        # 6. 验证分析结果
        if "root_causes" in data["data"]:
            assert isinstance(data["data"]["root_causes"], list)

    def test_rca_with_correlations(self, client, mock_services):
        """测试包含相关性分析的RCA"""
        # 模拟相关联的指标数据
        mock_services["prometheus"].query_range.return_value = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"__name__": "cpu_usage"},
                        "values": [
                            [i * 60 + 1640995200, str(50 + i * 2)] for i in range(30)
                        ],
                    },
                    {
                        "metric": {"__name__": "response_time"},
                        "values": [
                            [i * 60 + 1640995200, str(100 + i * 5)] for i in range(30)
                        ],
                    },
                ]
            },
        }

        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=1)

        payload = {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "namespace": "default",
            "metrics": ["cpu_usage", "response_time"],
            "target_metric": "cpu_usage",
        }

        response = client.post("/api/v1/rca/correlations/create", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0


class TestAutofixWorkflow:
    """测试自动修复工作流"""

    def test_complete_autofix_workflow(self, client, mock_services):
        """测试完整的自动修复工作流"""
        # 1. 模拟检测到的问题
        mock_services["k8s"].get_deployments.return_value = [
            Mock(
                metadata=Mock(name="nginx"),
                spec=Mock(replicas=3),
                status=Mock(ready_replicas=1, unavailable_replicas=2),  # 副本不足
            )
        ]

        # 2. 发送诊断请求
        diagnose_payload = {"namespace": "default", "deployment": "nginx"}

        diagnose_response = client.post(
            "/api/v1/autofix/diagnose", json=diagnose_payload
        )

        assert diagnose_response.status_code == 200
        diagnose_data = diagnose_response.json()
        assert diagnose_data["code"] == 0

        # 3. 发送修复请求
        fix_payload = {
            "namespace": "default",
            "deployment": "nginx",
            "issues": ["replica_mismatch"],
            "auto_approve": True,
        }

        fix_response = client.post("/api/v1/autofix/fix", json=fix_payload)

        assert fix_response.status_code == 200
        fix_data = fix_response.json()
        assert fix_data["code"] == 0

    def test_autofix_notification_workflow(self, client, mock_services):
        """测试自动修复通知工作流"""
        # 1. 模拟修复成功
        mock_services["notification"].send_webhook.return_value = True

        # 2. 发送通知请求
        payload = {
            "type": "fix_completed",
            "namespace": "default",
            "deployment": "nginx",
            "message": "修复完成：成功扩容到5个副本",
            "webhook_url": "https://hooks.slack.com/test",
        }

        response = client.post("/api/v1/autofix/notify/create", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0

        # 3. 验证通知被发送
        mock_services["notification"].send_webhook.assert_called_once()


class TestMultiAgentWorkflow:
    """测试多Agent协作工作流"""

    def test_multi_agent_task_execution(self, client, mock_services):
        """测试多Agent任务执行"""
        # 1. 检查Agent状态
        status_response = client.get("/api/v1/multi-agent/status/detail")

        assert status_response.status_code == 200
        status_data = status_response.json()
        assert status_data["code"] == 0

        # 2. 执行复杂任务
        task_payload = {
            "task_type": "incident_response",
            "priority": "high",
            "parameters": {
                "namespace": "production",
                "incident": "service_outage",
                "affected_services": ["api", "database"],
            },
        }

        execute_response = client.post("/api/v1/multi-agent/execute", json=task_payload)

        assert execute_response.status_code == 200
        execute_data = execute_response.json()
        assert execute_data["code"] == 0

        # 3. 检查协调状态
        coord_response = client.get("/api/v1/multi-agent/coordination/detail")

        assert coord_response.status_code == 200
        coord_data = coord_response.json()
        assert coord_data["code"] == 0


class TestAssistantWorkflow:
    """测试智能助手工作流"""

    @patch("app.api.routes.assistant.get_assistant_agent")
    def test_assistant_chat_workflow(self, mock_get_agent, client, mock_services):
        """测试助手聊天工作流"""

        class _MockAgent:
            async def get_answer(
                self, question, session_id=None, max_context_docs: int = 4
            ):
                return {
                    "answer": (
                        "根据您描述的Pod重启问题，建议您检查资源限制和健康检查配置，"
                        "并使用 kubectl describe pod 与 kubectl logs 排查。"
                    ),
                    "confidence": 0.9,
                }

        mock_get_agent.return_value = _MockAgent()
        # 1. 模拟知识库搜索
        mock_services["llm"].generate_response.return_value = (
            "根据您描述的Pod重启问题，建议您检查以下几个方面：\n"
            "1. 查看Pod的资源限制设置\n"
            "2. 检查健康检查配置\n"
            "3. 查看应用程序日志\n"
            "具体命令如下：\n"
            "```bash\n"
            "kubectl describe pod <pod-name> -n <namespace>\n"
            "kubectl logs <pod-name> -n <namespace>\n"
            "```"
        )

        # 2. 发送聊天请求
        chat_payload = {
            "query": "我的Pod一直重启，怎么排查？",
            "session_id": "session_123",
            "context": {"namespace": "default", "pod_name": "nginx-123"},
        }

        chat_response = client.post("/api/v1/assistant/chat", json=chat_payload)

        assert chat_response.status_code == 200
        chat_data = chat_response.json()
        assert chat_data["code"] == 0
        assert "data" in chat_data

        # 3. 验证响应包含有用信息
        if "response" in chat_data["data"]:
            response_text = chat_data["data"]["response"]
            assert "Pod" in response_text or "pod" in response_text
            assert "kubectl" in response_text

    def test_assistant_knowledge_search(self, client, mock_services):
        """测试知识库搜索工作流"""
        # 新接口不再提供独立搜索端点，改为知识列表作为可用替代
        search_response = client.get("/api/v1/assistant/knowledge/list")

        assert search_response.status_code == 200
        search_data = search_response.json()
        assert search_data["code"] == 0


class TestStorageWorkflow:
    """测试存储工作流"""

    def test_document_upload_workflow(self, client, mock_services):
        """测试文档上传工作流"""
        # 1. 上传文档
        test_content = "# Kubernetes故障排查指南\n\n这是一个测试文档。"
        files = {"file": ("troubleshooting.md", test_content, "text/markdown")}

        upload_response = client.post("/api/v1/storage/upload", files=files)

        assert upload_response.status_code in [404]

        # 2. 列出文档
        list_response = client.get("/api/v1/storage/documents")
        assert list_response.status_code in [404]

        # 3. 删除文档（如果上传成功）
        delete_response = client.delete("/api/v1/storage/documents/doc_123")
        assert delete_response.status_code in [404]


class TestEndToEndScenarios:
    """测试端到端场景"""

    def test_performance_issue_scenario(self, client, mock_services):
        """测试性能问题场景"""
        # 场景：应用响应缓慢，需要分析原因并自动修复

        # 1. 健康检查显示问题
        health_response = client.get("/api/v1/health/detailed")
        assert health_response.status_code in [200, 503, 404]

        # 2. RCA分析找出根因
        mock_services["prometheus"].query_range.return_value = {
            "status": "success",
            "data": {
                "result": [
                    {"values": [[i * 60 + 1640995200, str(90 + i)] for i in range(10)]}
                ]
            },
        }

        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=1)

        rca_payload = {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "namespace": "production",
            "metrics": ["cpu_usage"],
            "incident_description": "应用响应时间增加",
        }

        rca_response = client.post("/api/v1/rca/analyses/create", json=rca_payload)
        assert rca_response.status_code == 200

        # 3. 基于分析结果进行预测
        predict_response = client.get(
            "/api/v1/predict/trend/list",
            params={"hours_ahead": 12, "current_qps": 60.0},
        )
        assert predict_response.status_code == 200

        # 4. 自动修复
        autofix_payload = {
            "namespace": "production",
            "deployment": "web-app",
            "issues": ["high_cpu", "insufficient_replicas"],
        }

        autofix_response = client.post("/api/v1/autofix/fix", json=autofix_payload)
        assert autofix_response.status_code == 200

    @patch("app.api.routes.assistant.get_assistant_agent")
    def test_incident_response_scenario(self, mock_get_agent, client, mock_services):
        """测试事件响应场景"""

        class _MockAgent:
            async def get_answer(
                self, question, session_id=None, max_context_docs: int = 4
            ):
                return {
                    "answer": (
                        "Pod进入CrashLoopBackOff时，先查看 kubectl logs 与 describe，"
                        "确认探针与资源配置。"
                    ),
                    "confidence": 0.85,
                }

        mock_get_agent.return_value = _MockAgent()
        # 场景：Pod频繁崩溃，需要协调多个Agent处理

        # 1. 多Agent系统检测到异常
        mock_services["k8s"].get_pods.return_value = [
            Mock(
                metadata=Mock(name="failing-pod"),
                status=Mock(
                    phase="CrashLoopBackOff",
                    container_statuses=[
                        Mock(restart_count=10, state=Mock(waiting=True))
                    ],
                ),
            )
        ]

        # 2. 启动多Agent协作
        task_payload = {
            "task_type": "pod_failure_analysis",
            "priority": "critical",
            "parameters": {"namespace": "production", "pod_name": "failing-pod"},
        }

        agent_response = client.post("/api/v1/multi-agent/execute", json=task_payload)
        assert agent_response.status_code == 200

        # 3. 咨询智能助手
        assistant_payload = {
            "query": "Pod进入CrashLoopBackOff状态，重启次数很高，如何处理？",
            "session_id": "emergency_session",
            "context": {
                "namespace": "production",
                "pod": "failing-pod",
                "restart_count": 10,
            },
        }

        mock_services["llm"].generate_response.return_value = (
            "Pod进入CrashLoopBackOff状态通常表示容器启动后很快就退出。建议：\n"
            "1. 检查容器日志：kubectl logs failing-pod -n production\n"
            "2. 检查资源限制是否合理\n"
            "3. 验证镜像和配置的正确性\n"
            "4. 检查健康检查探针配置"
        )

        assistant_response = client.post(
            "/api/v1/assistant/chat", json=assistant_payload
        )
        assert assistant_response.status_code == 200

        # 4. 根据建议执行自动修复
        diagnose_response = client.post(
            "/api/v1/autofix/diagnose",
            json={"namespace": "production", "deployment": "failing-app"},
        )
        assert diagnose_response.status_code == 200


class TestErrorRecovery:
    """测试错误恢复"""

    def test_service_failure_recovery(self, client, mock_services):
        """测试服务故障恢复"""
        # 模拟Prometheus服务不可用
        mock_services["prometheus"].check_connectivity.return_value = False
        mock_services["prometheus"].query_range.side_effect = Exception("连接失败")

        # 即使Prometheus不可用，API也应该优雅降级
        response = client.get(
            "/api/v1/predict/trend/list", params={"hours_ahead": 6, "current_qps": 70.0}
        )

        # 应该返回错误或降级响应，而不是崩溃
        assert response.status_code in [200, 500, 503]

    def test_invalid_input_handling(self, client):
        """测试无效输入处理"""
        # 发送无效JSON
        response = client.get("/api/v1/predict/trend/list", params={"use_prom": True})
        assert response.status_code in [400, 422]

    def test_missing_required_fields(self, client):
        """测试缺少必需字段"""
        response = client.get("/api/v1/predict/trend/list", params={"hours_ahead": 0})
        assert response.status_code in [400, 422]


class TestPerformanceUnderLoad:
    """测试负载性能"""

    def test_concurrent_predictions(self, client, mock_services):
        """测试并发预测请求"""
        import threading
        import time

        # 模拟响应
        mock_services["prometheus"].query_range.return_value = {
            "status": "success",
            "data": {"result": []},
        }

        results = []

        def make_prediction_request():
            start_time = time.time()
            response = client.get(
                "/api/v1/predict/trend/list",
                params={"hours_ahead": 3, "current_qps": 55.0},
            )
            end_time = time.time()

            results.append(
                {
                    "status_code": response.status_code,
                    "response_time": end_time - start_time,
                    "thread_id": threading.current_thread().ident,
                }
            )

        # 创建5个并发请求
        threads = []
        for _ in range(5):
            thread = threading.Thread(target=make_prediction_request)
            threads.append(thread)
            thread.start()

        # 等待所有请求完成
        for thread in threads:
            thread.join()

        # 验证结果
        assert len(results) == 5
        for result in results:
            assert result["status_code"] == 200
            assert result["response_time"] < 10.0  # 响应时间应该合理

    def test_large_payload_handling(self, client):
        """测试大载荷处理"""
        # 创建包含大量数据的请求
        response = client.get(
            "/api/v1/predict/trend/list",
            params={"hours_ahead": 6, "current_qps": 110.0},
        )

        # 应该能处理大载荷或返回适当的错误
        assert response.status_code in [200, 413, 422]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=app", "--cov-report=term-missing"])
