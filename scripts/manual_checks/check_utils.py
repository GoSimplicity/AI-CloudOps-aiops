#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 测试工具模块 - 提供公共的测试函数和工具，消除重复代码
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from legacy_guard import require_legacy_opt_in
require_legacy_opt_in()

from datetime import datetime
import json
import logging
import time
from typing import Any, Dict, Optional

import requests

# 测试配置常量
from app.config.settings import config

DEFAULT_API_BASE_URL = f"http://{config.host}:{config.port}/api/v1"
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_DELAY = 3
DEFAULT_REQUEST_TIMEOUT = 60


def setup_test_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    配置测试日志器

    Args:
        name: 日志器名称
        level: 日志级别

    Returns:
        logging.Logger: 配置好的日志器
    """
    logging.basicConfig(
        level=level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    return logging.getLogger(name)


def print_header(message: str) -> None:
    """
    打印格式化的测试标题

    Args:
        message: 要显示的消息
    """
    print("\n" + "=" * 80)
    print(f" {message}")
    print("=" * 80)


def make_request(
    method: str,
    url: str,
    json_data: Optional[Dict[str, Any]] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    logger: Optional[logging.Logger] = None,
) -> Optional[requests.Response]:
    """
    发送HTTP请求，包含重试逻辑

    Args:
        method: HTTP方法（GET、POST等）
        url: 请求URL
        json_data: JSON数据（用于POST请求）
        max_retries: 最大重试次数
        timeout: 请求超时时间
        logger: 日志记录器

    Returns:
        Optional[requests.Response]: 响应对象，失败时返回None
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    for attempt in range(max_retries):
        try:
            logger.info(
                f"请求 {method.upper()} {url} (尝试 {attempt + 1}/{max_retries})"
            )

            if method.lower() == "get":
                response = requests.get(url, timeout=timeout)
            elif method.lower() == "post":
                if json_data:
                    logger.info(
                        f"发送数据: {json.dumps(json_data, ensure_ascii=False)}"
                    )
                response = requests.post(url, json=json_data, timeout=timeout)
            else:
                logger.error(f"不支持的HTTP方法: {method}")
                return None

            logger.info(f"响应状态码: {response.status_code}")
            return response

        except requests.exceptions.RequestException as e:
            logger.warning(f"请求失败 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"等待 {DEFAULT_RETRY_DELAY} 秒后重试...")
                time.sleep(DEFAULT_RETRY_DELAY)
            else:
                logger.error(f"请求最终失败: {str(e)}")
                return None

    return None


def check_service_health(
    api_base_url: str = DEFAULT_API_BASE_URL,
    max_retries: int = 3,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """
    检查服务健康状态

    Args:
        api_base_url: API基础URL
        max_retries: 最大重试次数
        logger: 日志记录器

    Returns:
        bool: 服务是否健康
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    # 使用预测服务就绪状态作为可用性检查
    health_url = f"{api_base_url}/predict/ready"
    response = make_request("get", health_url, max_retries=max_retries, logger=logger)

    if response and response.status_code == 200:
        logger.info("服务就绪检查通过")
        return True
    else:
        logger.warning("服务就绪检查失败，但继续测试")
        return False


def setup_test_environment(
    api_base_url: str = DEFAULT_API_BASE_URL, logger: Optional[logging.Logger] = None
) -> bool:
    """
    准备测试环境

    Args:
        api_base_url: API基础URL
        logger: 日志记录器

    Returns:
        bool: 环境设置是否成功
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    print_header("准备测试环境")

    try:
        # 检查服务是否运行
        check_service_health(api_base_url, logger=logger)
        return True  # 即使服务不健康也继续测试
    except Exception as e:
        logger.error(f"设置测试环境失败: {str(e)}")
        return False


def validate_response_structure(
    response: requests.Response,
    required_fields: Optional[list] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """
    验证响应结构

    Args:
        response: HTTP响应对象
        required_fields: 必需的字段列表
        logger: 日志记录器

    Returns:
        Dict[str, Any]: 验证结果和解析的数据
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    result = {"valid": False, "data": None, "missing_fields": []}

    try:
        data = response.json()
        result["data"] = data

        if required_fields:
            missing_fields = []
            for field in required_fields:
                if field not in data:
                    missing_fields.append(field)

            if missing_fields:
                result["missing_fields"] = missing_fields
                logger.warning(f"响应缺少字段: {missing_fields}")
            else:
                result["valid"] = True
                logger.info("响应数据结构验证通过")
        else:
            result["valid"] = True

    except json.JSONDecodeError:
        result["data"] = {"raw_response": response.text}
        logger.warning("响应不是有效的JSON格式")

    return result


def save_test_results(
    results: Dict[str, Any], filename: str, logger: Optional[logging.Logger] = None
) -> None:
    """
    保存测试结果到JSON文件

    Args:
        results: 测试结果字典
        filename: 保存的文件名
        logger: 日志记录器
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"测试结果已保存到 {filename}")
    except Exception as e:
        logger.error(f"保存测试结果失败: {str(e)}")


def calculate_test_summary(results: Dict[str, Any]) -> Dict[str, Any]:
    """
    计算测试摘要

    Args:
        results: 测试结果字典

    Returns:
        Dict[str, Any]: 测试摘要统计
    """
    if "results" not in results:
        return {"total_tests": 0, "passed_tests": 0, "success_rate": 0}

    total_tests = len(results["results"])
    passed_tests = sum(
        1 for test_result in results["results"].values() if test_result.get("success")
    )
    success_rate = (passed_tests / total_tests) * 100 if total_tests > 0 else 0

    return {
        "total_tests": total_tests,
        "passed_tests": passed_tests,
        "success_rate": f"{success_rate:.2f}%",
    }


def print_test_summary(results: Dict[str, Any], duration: float = 0) -> None:
    """
    打印测试摘要

    Args:
        results: 测试结果字典
        duration: 测试持续时间（秒）
    """
    summary = calculate_test_summary(results)

    print_header("测试摘要")
    print(f"总测试数: {summary['total_tests']}")
    print(f"通过测试数: {summary['passed_tests']}")
    print(f"成功率: {summary['success_rate']}")
    if duration > 0:
        print(f"测试持续时间: {duration:.2f} 秒")

    # 打印详细结果
    if "results" in results:
        print("\n详细测试结果:")
        for test_name, test_result in results["results"].items():
            status = "✅ 通过" if test_result.get("success") else "❌ 失败"
            status_code = test_result.get("status_code", "N/A")
            print(f"  {test_name}: {status} (状态码: {status_code})")


class TestResult:
    """测试结果封装类"""

    __test__ = False

    def __init__(self, test_name: str):
        self.test_name = test_name
        self.start_time = time.time()
        self.results = {
            "timestamp": datetime.utcnow().isoformat(),
            "results": {},
            "environment_setup": False,
        }

    def add_test_result(self, name: str, success: bool, **kwargs) -> None:
        """添加测试结果"""
        self.results["results"][name] = {"success": success, **kwargs}

    def set_environment_setup(self, success: bool) -> None:
        """设置环境设置状态"""
        self.results["environment_setup"] = success

    def get_summary(self) -> Dict[str, Any]:
        """获取测试结果摘要"""
        return calculate_test_summary(self.results)

    def finalize(self) -> Dict[str, Any]:
        """完成测试并返回最终结果"""
        duration = time.time() - self.start_time
        summary = calculate_test_summary(self.results)
        summary["duration_seconds"] = duration

        self.results["summary"] = summary
        return self.results
