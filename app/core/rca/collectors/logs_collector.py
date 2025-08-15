#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 容器日志采集器
"""

from __future__ import annotations

from typing import Dict, List, Optional, Any
import re

from app.config.settings import config
from app.services.kubernetes import KubernetesService


class LogsCollector:
    """日志采集器：按命名空间抓取若干Pod的最近日志。"""

    def __init__(self, namespace: Optional[str] = None):
        self.namespace = namespace or config.k8s.namespace
        self._svc = KubernetesService()

    async def pull(self) -> List[Dict]:
        """
        采集命名空间下 Deploy/Pods 的少量日志，并仅保留告警级别及以上的行
        """
        # 允许通过上游 include_logs 显式开启；若未显式开启，则跟随全局
        if not getattr(config, "logs", None):
            return []

        max_total = max(1, int(config.logs.max_pods))
        tail_lines = max(1, int(config.logs.tail_lines))
        include_prev = bool(config.logs.include_previous)

        # 先按 Deployment 的 selector 采集对应 Pods 日志，尽量覆盖不同工作负载
        results: List[Dict[str, Any]] = []

        try:
            deployments = await self._svc.get_deployments_async(
                namespace=self.namespace
            )
        except Exception:
            deployments = []

        def _build_label_selector(
            match_labels: Optional[Dict[str, str]],
        ) -> Optional[str]:
            if not match_labels:
                return None
            parts = []
            for k, v in match_labels.items():
                if not k or v is None:
                    continue
                parts.append(f"{k}={v}")
            return ",".join(parts) if parts else None

        remaining = max_total

        # 用简单的严重级别匹配：warning 及以上
        severity_pattern = re.compile(
            r"\b(warn|warning|error|err|fatal|critical|panic|severe|exception)\b",
            re.IGNORECASE,
        )

        async def _filter_logs_entry(
            entry: Dict[str, Any], deployment_name: Optional[str] = None
        ) -> Dict[str, Any]:
            def _filter_text(text: Optional[str]) -> str:
                if not text:
                    return ""
                lines = [
                    ln for ln in str(text).splitlines() if severity_pattern.search(ln)
                ]
                # 再次裁剪，避免过多输出
                return "\n".join(lines[:tail_lines])

            filtered = dict(entry)
            filtered["logs"] = _filter_text(entry.get("logs"))
            filtered["previous_logs"] = _filter_text(entry.get("previous_logs"))
            if deployment_name:
                filtered["deployment"] = deployment_name
            return filtered

        # 优先按 Deployment 选择器采样
        for d in deployments or []:
            if remaining <= 0:
                break
            spec = (d or {}).get("spec") or {}
            selector = (spec.get("selector") or {}).get("match_labels") or {}
            label_selector = _build_label_selector(selector)

            try:
                per_deploy = 1 if max_total <= 2 else 2
                per_deploy = min(per_deploy, remaining)
                entries = await self._svc.get_recent_pod_logs(
                    namespace=self.namespace,
                    label_selector=label_selector,
                    max_pods=per_deploy,
                    tail_lines=tail_lines,
                    include_previous=include_prev,
                )
                for e in entries:
                    results.append(
                        await _filter_logs_entry(
                            e, deployment_name=(d.get("metadata") or {}).get("name")
                        )
                    )
                remaining = max(0, remaining - len(entries))
            except Exception:
                continue

        # 若未达到配额或未找到 Deployment，则对整个命名空间做兜底采样
        if remaining > 0 and len(results) < max_total:
            try:
                entries = await self._svc.get_recent_pod_logs(
                    namespace=self.namespace,
                    label_selector=None,
                    max_pods=remaining,
                    tail_lines=tail_lines,
                    include_previous=include_prev,
                )
                for e in entries:
                    results.append(await _filter_logs_entry(e))
            except Exception:
                pass

        # 最终裁剪，确保不超过总量
        return results[:max_total]
