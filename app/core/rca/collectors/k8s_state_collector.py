#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: K8s 状态快照采集器
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.config.settings import config
from app.services.kubernetes import KubernetesService


class K8sStateCollector:
    """K8s 状态采集器。"""

    def __init__(self, namespace: Optional[str] = None):
        self.namespace = namespace or config.k8s.namespace
        self._svc = KubernetesService()

    async def snapshot(self) -> Dict[str, Any]:
        # 使用异步版本以避免阻塞事件循环
        pods_objs = await self._svc.get_pods_async(namespace=self.namespace)
        deps_objs = await self._svc.get_deployments_async(namespace=self.namespace)
        svcs_objs = await self._svc.get_services_async(namespace=self.namespace)
        pods = [p.to_dict() if hasattr(p, "to_dict") else p for p in (pods_objs or [])]
        deployments = [
            d.to_dict() if hasattr(d, "to_dict") else d for d in (deps_objs or [])
        ]
        services = [
            s.to_dict() if hasattr(s, "to_dict") else s for s in (svcs_objs or [])
        ]
        return {
            "namespace": self.namespace,
            "pods": pods,
            "deployments": deployments,
            "services": services,
        }
