#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
K8sStateCollector: 采集 Kubernetes 对象状态快照
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
        pods = await self._svc.get_pods(namespace=self.namespace)
        deployments = await self._svc.get_deployments(namespace=self.namespace)
        services = await self._svc.get_services(namespace=self.namespace)
        return {
            "namespace": self.namespace,
            "pods": pods,
            "deployments": deployments,
            "services": services,
        }
