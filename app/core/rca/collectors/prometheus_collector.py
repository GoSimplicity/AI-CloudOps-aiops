#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PrometheusCollector: 指标采集与归一化
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List

import pandas as pd

from app.services.prometheus import PrometheusService


class PrometheusCollector:
    """从 Prometheus 拉取时间序列并进行基础归一化。"""

    def __init__(self, step: str = "1m"):
        self.step = step
        self._svc = PrometheusService()

    async def pull_range(
        self, metric_queries: List[str], start_time: datetime, end_time: datetime
    ) -> Dict[str, pd.DataFrame]:
        data: Dict[str, pd.DataFrame] = {}
        for q in metric_queries:
            df = await self._svc.query_range(q, start_time, end_time, self.step)
            if df is not None and not df.empty and "value" in df.columns:
                data[q] = df[["value"]].copy()
        return data
