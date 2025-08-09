#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Trace服务模块 - 提供从Jaeger Query API拉取Trace/Span的能力
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from app.config.settings import config

logger = logging.getLogger("aiops.tracing")

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))


class TracingService:
    """Jaeger Query API 客户端（HTTP）。

    仅实现按时间范围和服务名检索 traces 的最小能力，用于RCA证据与时间线增强。
    """

    def __init__(self):
        self.enabled = bool(getattr(config.tracing, "enabled", False))
        self.base_url = getattr(config.tracing, "jaeger_query_url", "http://127.0.0.1:16686")
        self.timeout = int(getattr(config.tracing, "timeout", 15) or 15)

    def is_enabled(self) -> bool:
        return self.enabled

    def _format_time_us(self, dt: datetime) -> int:
        # Jaeger HTTP API时间以微秒为单位（epoch）
        return int(dt.timestamp() * 1_000_000)

    async def search_traces(
        self,
        start_time: datetime,
        end_time: datetime,
        service: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []

        try:
            params = {
                "start": self._format_time_us(start_time),
                "end": self._format_time_us(end_time),
                "limit": max(1, min(int(limit or 20), int(getattr(config.tracing, "max_traces", 30)) or 30)),
            }
            svc_name = service or getattr(config.tracing, "service_name", None)
            if svc_name:
                params["service"] = svc_name

            # Jaeger Query HTTP API 示例： /api/traces?service=svc&start=...&end=...&limit=...
            url = f"{self.base_url.rstrip('/')}/api/traces"
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json() or {}
            traces = data.get("data") or []
            # 仅返回必要字段以减小负载
            sanitized: List[Dict[str, Any]] = []
            for t in traces:
                trace_id = (t or {}).get("traceID")
                spans = (t or {}).get("spans") or []
                ops = list({(s or {}).get("operationName") for s in spans if s})
                start_ts = None
                duration = None
                if spans:
                    # 按开始时间排序取最早/最晚作为边界
                    sorted_spans = sorted(spans, key=lambda s: (s or {}).get("startTime", 0))
                    start_ts = (sorted_spans[0] or {}).get("startTime")
                    end_ts = ((sorted_spans[-1] or {}).get("startTime", 0)) + ((sorted_spans[-1] or {}).get("duration", 0))
                    duration = max(0, int(end_ts) - int(start_ts))
                sanitized.append(
                    {
                        "trace_id": trace_id,
                        "operations": ops,
                        "span_count": len(spans),
                        "start_us": start_ts,
                        "duration_us": duration,
                    }
                )
            return sanitized
        except Exception as e:
            logger.error(f"查询Jaeger traces失败: {e}")
            return []

