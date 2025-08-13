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
from typing import Optional

from app.services.prometheus import PrometheusService


class PrometheusCollector:
    def __init__(self, step: str = "1m"):
        self._svc = PrometheusService()
        self.step = step

    async def collect(self, query: str, start_time: datetime, end_time: datetime):
        return await self._svc.query_range_async(query, start_time, end_time, self.step)

    async def collect_instant(self, query: str, ts: Optional[datetime] = None):
        return await self._svc.query_instant_async(query, ts)
