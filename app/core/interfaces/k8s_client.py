#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
License: Apache 2.0
Description: Kubernetes 客户端接口定义与空实现（Core层）
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol


class K8sClient(Protocol):
    async def health_check(self) -> bool: ...

    async def get_pods(
        self, namespace: str, label_selector: Optional[str] = None
    ) -> List[Dict]: ...

    async def get_events(
        self,
        namespace: str,
        field_selector: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]: ...

    async def get_pod(self, namespace: str, pod_name: str) -> Optional[Dict]: ...

    async def get_pod_logs(
        self,
        namespace: str,
        pod_name: str,
        container_name: Optional[str] = None,
        since_time: Optional[datetime] = None,
        tail_lines: Optional[int] = None,
        follow: bool = False,
    ) -> Optional[str]: ...

    # 可选：与Deployment相关的方法，供需要的上层使用
    async def get_deployment(
        self, name: str, namespace: str
    ) -> Optional[Dict[str, Any]]: ...

    async def patch_deployment(
        self, name: str, patch: Dict[str, Any], namespace: str
    ) -> bool: ...

    async def can_i(self, verb: str, resource: str, namespace: str) -> bool: ...


class NullK8sClient:
    async def health_check(self) -> bool:
        return False

    async def get_pods(
        self, namespace: str, label_selector: Optional[str] = None
    ) -> List[Dict]:
        return []

    async def get_events(
        self,
        namespace: str,
        field_selector: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        return []

    async def get_pod(self, namespace: str, pod_name: str) -> Optional[Dict]:
        return None

    async def get_pod_logs(
        self,
        namespace: str,
        pod_name: str,
        container_name: Optional[str] = None,
        since_time: Optional[datetime] = None,
        tail_lines: Optional[int] = None,
        follow: bool = False,
    ) -> Optional[str]:
        return None

    async def get_deployment(
        self, name: str, namespace: str
    ) -> Optional[Dict[str, Any]]:
        return None

    async def patch_deployment(
        self, name: str, patch: Dict[str, Any], namespace: str
    ) -> bool:
        return False

    async def can_i(self, verb: str, resource: str, namespace: str) -> bool:
        return False
