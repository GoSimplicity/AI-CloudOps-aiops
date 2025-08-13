#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
from typing import Any, Dict


class DetectionRules:
    """检测规则定义类"""
    
    @staticmethod
    def get_pod_rules() -> Dict[str, Any]:
        """获取Pod检测规则"""
        return {
            "crash_loop": {
                "severity": "critical",
                "auto_fix": True,
                "description": "Pod处于CrashLoopBackOff状态"
            },
            "image_pull_error": {
                "severity": "high", 
                "auto_fix": False,
                "description": "Pod镜像拉取失败"
            },
            "mount_failure": {
                "severity": "medium",
                "auto_fix": False,
                "description": "Pod卷挂载失败"
            },
            "resource_pressure": {
                "severity": "medium",
                "auto_fix": True,
                "description": "Pod因资源压力无法调度"
            },
            "pending_timeout": {
                "severity": "medium",
                "auto_fix": True,
                "description": "Pod长时间处于Pending状态"
            }
        }
    
    @staticmethod 
    def get_deployment_rules() -> Dict[str, Any]:
        """获取Deployment检测规则"""
        return {
            "replica_mismatch": {
                "severity": "high",
                "auto_fix": True,
                "description": "部署副本数不匹配"
            },
            "unavailable_replicas": {
                "severity": "critical",
                "auto_fix": True,
                "description": "部署有不可用副本"
            }
        }
    
    @staticmethod
    def get_service_rules() -> Dict[str, Any]:
        """获取Service检测规则"""
        return {
            "no_endpoints": {
                "severity": "high",
                "auto_fix": True,
                "description": "服务没有可用的Endpoints"
            }
        }
    
    @staticmethod
    def get_all_rules() -> Dict[str, Any]:
        """获取所有检测规则"""
        return {
            "pod_issues": DetectionRules.get_pod_rules(),
            "deployment_issues": DetectionRules.get_deployment_rules(),
            "service_issues": DetectionRules.get_service_rules()
        }