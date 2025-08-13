#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

import asyncio
import logging
from typing import Any, Dict, List

from app.services.kubernetes import KubernetesService
from app.utils.time_utils import iso_utc_now

logger = logging.getLogger("aiops.verifier")


class K8sVerifierAgent:
    """Kubernetes修复结果验证Agent"""

    def __init__(self):
        self.k8s_service = KubernetesService()

    async def verify_deployment_health(
        self, name: str, namespace: str, *, wait_seconds: int = 20
    ) -> Dict[str, Any]:
        """验证Deployment在修复后的健康状态。

        设计意图：
        - 等待一小段时间以让滚动更新/探针生效
        - 综合 Pod Ready、Running 数量与不可用副本数，给出明确的通过/部分/失败结论
        """
        try:
            await asyncio.sleep(max(0, min(wait_seconds, 60)))

            pods = await self.k8s_service.get_pods_async(
                namespace=namespace, label_selector=f"app={name}"
            )
            total = len(pods)
            running_ready = 0
            not_ready_pods: List[str] = []

            for pod in pods:
                status = pod.get("status", {})
                if status.get("phase") == "Running":
                    ready = any(
                        c.get("type") == "Ready" and c.get("status") == "True"
                        for c in status.get("conditions", [])
                    )
                    if ready:
                        running_ready += 1
                    else:
                        not_ready_pods.append(pod.get("metadata", {}).get("name", ""))
                else:
                    not_ready_pods.append(pod.get("metadata", {}).get("name", ""))

            status: str
            success_rate = (running_ready / total) if total > 0 else 0.0
            if success_rate >= 0.8:
                status = "success"
            elif success_rate >= 0.5:
                status = "partial"
            else:
                status = "failed"

            return {
                "deployment": name,
                "namespace": namespace,
                "status": status,
                "running_ready": running_ready,
                "total_pods": total,
                "success_rate": round(success_rate * 100, 2),
                "not_ready_pods": not_ready_pods[:10],
                "timestamp": iso_utc_now(),
            }

        except Exception as e:
            logger.error(f"验证Deployment健康失败: {str(e)}")
            return {"status": "failed", "error": str(e)}
