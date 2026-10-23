#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 智能助手完整测试套件
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from legacy_guard import require_legacy_opt_in
require_legacy_opt_in()

from datetime import datetime

import pytest
from check_utils import (
    DEFAULT_API_BASE_URL,
    TestResult,
    make_request,
    setup_test_logger,
    validate_response_structure,
)

logger = setup_test_logger("test_assistant")


class TestAssistantAPI:
    """智能助手API测试类"""

    @classmethod
    def setup_class(cls):
        """测试类初始化"""
        cls.api_base_url = DEFAULT_API_BASE_URL
        cls.test_result = TestResult("智能助手API测试")
        cls.session_id = None

    def test_assistant_ready_check(self):
        """测试智能助手服务就绪性检查"""
        logger.info("测试智能助手服务就绪性检查 /api/v1/assistant/ready")

        url = f"{self.api_base_url}/assistant/ready"
        response = make_request("get", url, logger=logger)

        assert response is not None, "智能助手就绪性检查请求失败"
        # 就绪性检查可能返回503(未就绪)或200(就绪)
        assert response.status_code in [200, 503], (
            f"智能助手就绪性检查返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        data = validation["data"]["data"]
        assert "status" in data, "响应数据缺少status字段"

        self.test_result.add_test_result(
            "assistant_ready_check",
            True,
            status_code=response.status_code,
            ready_status=data.get("status"),
        )

    def test_assistant_info(self):
        """测试智能助手服务信息接口"""
        logger.info("测试智能助手服务信息接口 /api/v1/assistant/info")

        url = f"{self.api_base_url}/assistant/info"
        response = make_request("get", url, logger=logger)

        assert response is not None, "智能助手服务信息请求失败"
        # 服务信息可能返回200或500
        assert response.status_code in [200, 500], (
            f"智能助手服务信息返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        if response.status_code == 200:
            data = validation["data"]["data"]
            assert "service" in data, "响应数据缺少service字段"
            assert "version" in data, "响应数据缺少version字段"
            assert "initialized" in data, "响应数据缺少initialized字段"

        self.test_result.add_test_result(
            "assistant_info", True, status_code=response.status_code
        )

    def test_assistant_create_session(self):
        """测试创建会话接口"""
        logger.info("测试创建会话接口 /api/v1/assistant/session")

        url = f"{self.api_base_url}/assistant/session"
        response = make_request("post", url, logger=logger)

        assert response is not None, "创建会话请求失败"
        # 创建会话可能成功(200)或失败(500)
        assert response.status_code in [200, 500], (
            f"创建会话返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        if response.status_code == 200:
            data = validation["data"]["data"]
            assert "session_id" in data, "响应数据缺少session_id字段"
            assert "timestamp" in data, "响应数据缺少timestamp字段"

            # 保存会话ID供后续测试使用
            self.__class__.session_id = data["session_id"]
            logger.info(f"创建会话成功，会话ID: {self.__class__.session_id}")

        self.test_result.add_test_result(
            "assistant_create_session",
            True,
            status_code=response.status_code,
            session_created=(response.status_code == 200),
        )

    def test_assistant_query_without_session(self):
        """测试不带会话ID的查询"""
        logger.info("测试不带会话ID的查询 /api/v1/assistant/query")

        url = f"{self.api_base_url}/assistant/query"
        payload = {"question": "AI-CloudOps平台是什么？", "max_context_docs": 4}

        response = make_request(
            "post", url, json_data=payload, logger=logger, timeout=30
        )

        assert response is not None, "智能助手查询请求失败"
        # 查询可能成功(200)或失败(500)，取决于助手初始化状态
        assert response.status_code in [200, 400, 500], (
            f"智能助手查询返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        if response.status_code == 200:
            data = validation["data"]["data"]
            assert "answer" in data, "响应数据缺少answer字段"
            assert "session_id" in data, "响应数据缺少session_id字段"

            # 验证回答不为空
            assert len(data["answer"]) > 0, "回答内容不应为空"

        self.test_result.add_test_result(
            "assistant_query_without_session",
            True,
            status_code=response.status_code,
            answer_received=(response.status_code == 200),
        )

    def test_assistant_query_with_session(self):
        """测试带会话ID的查询"""
        logger.info("测试带会话ID的查询")

        # 如果之前没有创建会话成功，跳过此测试
        if not self.__class__.session_id:
            logger.warning("没有可用的会话ID，跳过带会话查询测试")
            pytest.skip("没有可用的会话ID")

        url = f"{self.api_base_url}/assistant/query"
        payload = {
            "question": "请介绍一下负载预测功能",
            "session_id": self.__class__.session_id,
            "max_context_docs": 3,
        }

        response = make_request(
            "post", url, json_data=payload, logger=logger, timeout=30
        )

        assert response is not None, "智能助手查询请求失败"
        # 查询可能成功(200)或失败(500)
        assert response.status_code in [200, 500], (
            f"智能助手查询返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        if response.status_code == 200:
            data = validation["data"]["data"]
            assert "answer" in data, "响应数据缺少answer字段"
            assert "session_id" in data, "响应数据缺少session_id字段"

            # 验证会话ID匹配
            assert data["session_id"] == self.__class__.session_id, "返回的会话ID不匹配"

        self.test_result.add_test_result(
            "assistant_query_with_session",
            True,
            status_code=response.status_code,
            session_preserved=(response.status_code == 200),
        )

    def test_assistant_query_invalid_request(self):
        """测试无效请求格式"""
        logger.info("测试无效请求格式")

        url = f"{self.api_base_url}/assistant/query"
        payload = {
            # 缺少question字段
            "max_context_docs": 4
        }

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "智能助手查询请求失败"
        assert response.status_code == 400, (
            f"无效请求应返回400状态码，实际: {response.status_code}"
        )

        self.test_result.add_test_result(
            "assistant_query_invalid_request", True, status_code=response.status_code
        )

    def test_assistant_query_empty_question(self):
        """测试空问题查询"""
        logger.info("测试空问题查询")

        url = f"{self.api_base_url}/assistant/query"
        payload = {
            "question": "",  # 空问题
            "max_context_docs": 4,
        }

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "智能助手查询请求失败"
        assert response.status_code == 400, (
            f"空问题应返回400状态码，实际: {response.status_code}"
        )

        self.test_result.add_test_result(
            "assistant_query_empty_question", True, status_code=response.status_code
        )

    def test_assistant_refresh_knowledge_base(self):
        """测试刷新知识库接口"""
        logger.info("测试刷新知识库接口 /api/v1/assistant/refresh")

        url = f"{self.api_base_url}/assistant/refresh"
        response = make_request("post", url, logger=logger, timeout=60)

        assert response is not None, "刷新知识库请求失败"
        # 刷新知识库可能成功(200)或失败(500)
        assert response.status_code in [200, 500], (
            f"刷新知识库返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        if response.status_code == 200:
            data = validation["data"]["data"]
            assert "documents_count" in data, "响应数据缺少documents_count字段"
            assert "timestamp" in data, "响应数据缺少timestamp字段"

        self.test_result.add_test_result(
            "assistant_refresh_knowledge_base",
            True,
            status_code=response.status_code,
            refresh_success=(response.status_code == 200),
        )

    def test_assistant_add_document(self):
        """测试添加文档接口"""
        logger.info("测试添加文档接口 /api/v1/assistant/add-document")

        url = f"{self.api_base_url}/assistant/add-document"
        payload = {
            "content": "这是一个测试文档，用于验证智能助手的文档添加功能。包含关键信息：AIOps平台测试、负载预测、根因分析。",
            "metadata": {
                "source": "测试脚本",
                "type": "测试文档",
                "created_at": datetime.utcnow().isoformat(),
            },
        }

        response = make_request(
            "post", url, json_data=payload, logger=logger, timeout=30
        )

        assert response is not None, "添加文档请求失败"
        # 添加文档可能成功(200)或失败(500)
        assert response.status_code in [200, 500], (
            f"添加文档返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        self.test_result.add_test_result(
            "assistant_add_document",
            True,
            status_code=response.status_code,
            document_added=(response.status_code == 200),
        )

    def test_assistant_add_document_empty_content(self):
        """测试添加空内容文档"""
        logger.info("测试添加空内容文档")

        url = f"{self.api_base_url}/assistant/add-document"
        payload = {
            "content": "",  # 空内容
            "metadata": {"source": "测试"},
        }

        response = make_request("post", url, json_data=payload, logger=logger)

        assert response is not None, "添加文档请求失败"
        assert response.status_code == 400, (
            f"空内容应返回400状态码，实际: {response.status_code}"
        )

        self.test_result.add_test_result(
            "assistant_add_document_empty_content",
            True,
            status_code=response.status_code,
        )

    def test_assistant_clear_cache(self):
        """测试清除缓存接口"""
        logger.info("测试清除缓存接口 /api/v1/assistant/clear-cache")

        url = f"{self.api_base_url}/assistant/clear-cache"
        response = make_request("post", url, logger=logger)

        assert response is not None, "清除缓存请求失败"
        # 清除缓存可能成功(200)或失败(500)
        assert response.status_code in [200, 500], (
            f"清除缓存返回不预期状态码 {response.status_code}"
        )

        # 验证响应结构
        required_fields = ["code", "message", "data"]
        validation = validate_response_structure(response, required_fields, logger)
        assert validation["valid"], f"响应结构验证失败: {validation['missing_fields']}"

        if response.status_code == 200:
            data = validation["data"]["data"]
            assert "cleared_items" in data, "响应数据缺少cleared_items字段"
            assert "timestamp" in data, "响应数据缺少timestamp字段"

        self.test_result.add_test_result(
            "assistant_clear_cache",
            True,
            status_code=response.status_code,
            cache_cleared=(response.status_code == 200),
        )

    def test_assistant_response_timing(self):
        """测试智能助手接口响应时间"""
        logger.info("测试智能助手接口响应时间")

        import time

        start_time = time.time()

        url = f"{self.api_base_url}/assistant/info"
        response = make_request("get", url, timeout=15, logger=logger)

        end_time = time.time()
        response_time = end_time - start_time

        assert response is not None, "智能助手信息请求失败"
        # 信息接口应该响应快
        assert response_time < 15.0, f"智能助手信息响应时间过长: {response_time:.2f}秒"

        self.test_result.add_test_result(
            "assistant_response_timing",
            True,
            status_code=response.status_code,
            response_time=response_time,
        )

        logger.info(f"智能助手信息接口响应时间: {response_time:.2f}秒")

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

        save_test_results(results, "logs/assistant_test_results.json", logger)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
