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

from datetime import datetime
from typing import Dict, List, Optional

from app.config.settings import config
from app.services.tracing import TracingService


class TracingCollector:
    """Trace采集器：按时间窗口抓取trace摘要。"""

    def __init__(self):
        self._svc = TracingService()

    async def pull(
        self,
        start_time: datetime,
        end_time: datetime,
        service: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict]:
        if not config.tracing.enabled or not self._svc.is_enabled():
            return []
        return await self._svc.search_traces(
            start_time=start_time,
            end_time=end_time,
            service=service,
            limit=limit,
        )
