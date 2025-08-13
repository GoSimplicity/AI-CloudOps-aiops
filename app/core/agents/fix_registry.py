#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
from typing import Any, Dict, List


class FixRegistry:
    """修复动作注册表。

    设计意图：
    - 将问题类型与标准修复动作解耦，便于策略Agent生成结构化步骤。
    - 扩展时只需新增映射而非散落到多处逻辑中。
    """

    @staticmethod
    def get_actions_for_issue(issue_type: str) -> List[Dict[str, Any]]:
        mapping = {
            # CrashLoopBackOff：添加/修正探针并重启观察
            "crash_loop": [
                {"type": "check", "action": "validate_deployment_existence"},
                {
                    "type": "modify",
                    "action": "ensure_readiness_probe",
                    "patch": {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "auto",
                                            "readinessProbe": {
                                                "httpGet": {"path": "/", "port": 80},
                                                "initialDelaySeconds": 5,
                                                "periodSeconds": 10,
                                            },
                                        }
                                    ]
                                }
                            }
                        }
                    },
                },
                {"type": "restart", "action": "restart_deployment"},
                {"type": "monitor", "action": "wait_and_check", "wait_time": 20},
            ],
            # 探针失败：修正 liveness/readiness 配置
            "probe_failure": [
                {"type": "check", "action": "validate_deployment_existence"},
                {
                    "type": "modify",
                    "action": "fix_liveness_probe",
                    "patch": {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "auto",
                                            "livenessProbe": {
                                                "httpGet": {"path": "/", "port": 80},
                                                "periodSeconds": 10,
                                                "failureThreshold": 3,
                                            },
                                        }
                                    ]
                                }
                            }
                        }
                    },
                },
                {"type": "monitor", "action": "wait_and_check", "wait_time": 20},
            ],
            # 镜像拉取失败：提示性步骤（仅检查/监控，不自动改动）
            "image_pull_error": [
                {"type": "check", "action": "validate_deployment_existence"},
                {"type": "monitor", "action": "wait_and_check", "wait_time": 15},
            ],
            # 卷挂载失败：提示性步骤（仅检查/监控，不自动改动）
            "mount_failure": [
                {"type": "check", "action": "validate_deployment_existence"},
                {"type": "monitor", "action": "wait_and_check", "wait_time": 15},
            ],
            # 资源压力：降低requests/limits到保守值
            "resource_pressure": [
                {"type": "check", "action": "validate_deployment_existence"},
                {
                    "type": "modify",
                    "action": "adjust_resources",
                    "patch": {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "auto",
                                            "resources": {
                                                "requests": {"memory": "128Mi", "cpu": "100m"},
                                                "limits": {"memory": "256Mi", "cpu": "200m"},
                                            },
                                        }
                                    ]
                                }
                            }
                        }
                    },
                },
                {"type": "monitor", "action": "wait_and_check", "wait_time": 20},
            ],
            # Pending 超时：按资源压力路径处理（降资源门槛 + 观察）
            "pending_timeout": [
                {"type": "check", "action": "validate_deployment_existence"},
                {
                    "type": "modify",
                    "action": "adjust_resources",
                    "patch": {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "auto",
                                            "resources": {
                                                "requests": {"memory": "128Mi", "cpu": "100m"},
                                                "limits": {"memory": "256Mi", "cpu": "200m"},
                                            },
                                        }
                                    ]
                                }
                            }
                        }
                    },
                },
                {"type": "monitor", "action": "wait_and_check", "wait_time": 20},
            ],
            # 部署副本不匹配：仅检查+观察（保守，不直接改replicas）
            "replica_mismatch": [
                {"type": "check", "action": "validate_deployment_existence"},
                {"type": "monitor", "action": "wait_and_check", "wait_time": 15},
            ],
            # 不可用副本：检查+观察
            "unavailable_replicas": [
                {"type": "check", "action": "validate_deployment_existence"},
                {"type": "monitor", "action": "wait_and_check", "wait_time": 15},
            ],
            # Service 无端点：检查+观察（不进行自动注入selector）
            "no_endpoints": [
                {"type": "check", "action": "validate_service_existence"},
                {"type": "monitor", "action": "wait_and_check", "wait_time": 10},
            ],
        }
        return mapping.get(issue_type, [])

