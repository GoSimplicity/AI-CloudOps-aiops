#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 多Agent 模块（detector_helpers）
"""

from typing import Any, Dict


class DetectorHelpers:
    """检测器辅助函数类"""

    # 说明：Pod级消息与详情在主检测器中未直接引用，移除未使用方法以精简体积

    @staticmethod
    def get_deployment_message(deployment: Dict[str, Any], issue_type: str) -> str:
        """获取Deployment问题描述"""
        name = deployment.get("metadata", {}).get("name", "unknown")
        messages = {
            "replica_mismatch": f"部署 {name} 副本数不匹配",
            "unavailable_replicas": f"部署 {name} 有不可用副本",
        }
        return messages.get(issue_type, f"部署 {name} 出现问题")

    @staticmethod
    def get_service_message(service: Dict[str, Any], issue_type: str) -> str:
        """获取Service问题描述"""
        name = service.get("metadata", {}).get("name", "unknown")
        messages = {"no_endpoints": f"服务 {name} 没有可用的Endpoints"}
        return messages.get(issue_type, f"服务 {name} 出现问题")

    # 说明：Pod详情方法未被引用，移除

    @staticmethod
    def get_deployment_details(deployment: Dict[str, Any]) -> Dict[str, Any]:
        """获取Deployment详细信息"""
        return {
            "name": deployment.get("metadata", {}).get("name"),
            "namespace": deployment.get("metadata", {}).get("namespace"),
            "replicas": {
                "desired": deployment.get("spec", {}).get("replicas", 0),
                "available": deployment.get("status", {}).get("available_replicas", 0),
                "ready": deployment.get("status", {}).get("ready_replicas", 0),
            },
        }

    @staticmethod
    def get_service_details(service: Dict[str, Any]) -> Dict[str, Any]:
        """获取Service详细信息"""
        return {
            "name": service.get("metadata", {}).get("name"),
            "namespace": service.get("metadata", {}).get("namespace"),
            "type": service.get("spec", {}).get("type"),
            "selector": service.get("spec", {}).get("selector", {}),
            "ports": service.get("spec", {}).get("ports", []),
        }
