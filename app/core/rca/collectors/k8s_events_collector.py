#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: K8s 事件采集器
"""

from __future__ import annotations

from typing import Dict, List, Optional

from app.services.kubernetes import KubernetesService


class K8sEventsCollector:
    """K8s 事件采集器。"""

    def __init__(self, namespace: Optional[str] = None):
        self.namespace = namespace
        self._svc = KubernetesService()

    async def pull(self, limit: int = 200) -> List[Dict]:
        events = await self._svc.get_events(namespace=self.namespace, limit=limit)
        # 仅保留关键信息，便于结果序列化
        trimmed: List[Dict] = []
        for e in events:
            meta = e.get("metadata", {}) if isinstance(e, dict) else {}
            trimmed.append(
                {
                    "name": meta.get("name"),
                    "namespace": meta.get("namespace"),
                    "type": e.get("type"),
                    "reason": e.get("reason"),
                    "message": e.get("message"),
                    "first_timestamp": e.get("first_timestamp")
                    or meta.get("creation_timestamp"),
                    "last_timestamp": e.get("last_timestamp"),
                }
            )
        return trimmed
