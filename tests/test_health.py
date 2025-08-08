#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 健康检查模块测试，验证系统健康状态和组件可用性
"""

import os
import sys
import pytest
import json
import logging

# 添加项目路径到sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("test_health")

def test_health_endpoint(client):
    """测试健康检查API端点"""
    logger.info("测试健康检查API端点")
    
    response = client.get('/api/v1/health')
    
    assert response.status_code == 200
    
    data = json.loads(response.data)
    assert 'code' in data
    assert data['code'] == 0
    assert 'data' in data
    assert 'status' in data['data']
    assert data['data']['status'] == 'healthy'
    
    # 检查组件状态
    assert 'components' in data['data']
    
    # 服务启动时间
    assert 'timestamp' in data['data']
    
    logger.info("健康检查API端点测试通过")

def test_prometheus_health(prometheus_service):
    """测试Prometheus健康状态"""
    logger.info("测试Prometheus健康状态")
    
    assert prometheus_service.is_healthy() == True
    
    logger.info("Prometheus健康状态测试通过")

def test_kubernetes_health(k8s_service):
    """测试Kubernetes健康状态"""
    logger.info("测试Kubernetes健康状态")
    
    assert k8s_service.is_healthy() == True
    
    logger.info("Kubernetes健康状态测试通过")

@pytest.mark.skipif(
    os.environ.get("SKIP_LLM_TESTS", "false").lower() == "true",
    reason="LLM API测试被环境变量禁用"
)
def test_llm_health(llm_service):
    """测试LLM服务健康状态"""
    logger.info("测试LLM服务健康状态")
    
    assert llm_service.is_healthy() == True
    
    logger.info("LLM服务健康状态测试通过")

if __name__ == "__main__":
    pytest.main(["-xvs", __file__])