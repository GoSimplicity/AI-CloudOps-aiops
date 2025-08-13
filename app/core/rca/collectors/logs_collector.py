#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

from __future__ import annotations

from typing import Dict, List, Optional

from app.config.settings import config
from app.services.kubernetes import KubernetesService


class LogsCollector:
    """日志采集器：按命名空间抓取若干Pod的最近日志。"""

    def __init__(self, namespace: Optional[str] = None):
        self.namespace = namespace or config.k8s.namespace
        self._svc = KubernetesService()

    async def pull(self) -> List[Dict]:
        if not config.logs.enabled:
            return []
        return await self._svc.get_recent_pod_logs(
            namespace=self.namespace,
            label_selector=None,
            max_pods=config.logs.max_pods,
            tail_lines=config.logs.tail_lines,
            include_previous=config.logs.include_previous,
        )
