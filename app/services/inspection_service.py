#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Kubernetes 智能巡检服务
"""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import time
from typing import Any, Dict, List, Optional

from app.config.settings import config
from app.core.inspection.profiles import get_profile_rules
from app.core.inspection.rules.base import RuleContext
from app.core.inspection.scoring import severity_to_score
from app.models.inspection_models import (
    InspectionFinding,
    InspectionReport,
    InspectionRunRequest,
    InspectionSummary,
    InspectionTaskStatus,
)
from app.services.base import BaseService
from app.services.kubernetes import KubernetesService
from app.services.notification import NotificationService
from app.services.prometheus import PrometheusService

logger = logging.getLogger("aiops.services.inspection")


class InspectionService(BaseService):
    """巡检服务：并发采集 + 规则检查 + 报告构建"""

    def __init__(self) -> None:
        super().__init__("inspection")
        self._k8s: Optional[KubernetesService] = None
        self._prom: Optional[PrometheusService] = None

        # 简易任务与历史缓存（内存）
        self._tasks: Dict[str, InspectionTaskStatus] = {}
        self._reports: Dict[str, InspectionReport] = {}
        # 简易调度器
        self._scheduler_task: Optional[asyncio.Task] = None
        self._scheduler_running: bool = False

    async def _do_initialize(self) -> None:
        # 依赖初始化（尽量轻量）
        try:
            self._k8s = await asyncio.to_thread(KubernetesService)
        except Exception as e:
            logger.warning(f"Kubernetes服务初始化失败: {e}")
            self._k8s = None

        try:
            self._prom = PrometheusService()
            await self._prom.initialize()
        except Exception as e:
            logger.warning(f"Prometheus服务初始化失败: {e}")
            self._prom = None

    async def _do_health_check(self) -> bool:
        # 按最小要求：任一数据源可用即认为可执行巡检
        k = bool(self._k8s and await self._k8s.health_check())
        p = bool(self._prom and self._prom.is_initialized())
        return k or p

    async def start_scheduler(self) -> Dict[str, Any]:
        """启动内部简单调度（基于sleep间隔近似cron）"""
        if self._scheduler_running:
            return {"running": True}

        interval_minutes = 30
        try:
            cron = config.inspection.scheduler_cron
            # 粗略解析 */N * * * *
            if cron.startswith("*/"):
                try:
                    interval_minutes = max(5, int(cron.split(" ")[0].replace("*/", "")))
                except Exception:
                    interval_minutes = 30
        except Exception:
            interval_minutes = 30

        self._scheduler_running = True

        async def _loop():
            while self._scheduler_running:
                try:
                    req = InspectionRunRequest(
                        scope="namespace",
                        namespace=None,
                        profiles=[config.inspection.default_profile],
                        time_window_minutes=config.inspection.time_window_minutes,
                        include_events=config.inspection.include_events,
                        include_logs=False,
                        severity_threshold=config.inspection.severity_threshold,
                    )
                    await self.run_inspection(req)
                except Exception:
                    pass
                await asyncio.sleep(interval_minutes * 60)

        self._scheduler_task = asyncio.create_task(_loop())
        return {"running": True, "interval_minutes": interval_minutes}

    async def stop_scheduler(self) -> Dict[str, Any]:
        if not self._scheduler_running:
            return {"running": False}
        self._scheduler_running = False
        try:
            if self._scheduler_task:
                self._scheduler_task.cancel()
        except Exception:
            pass
        return {"running": False}

    async def scheduler_status(self) -> Dict[str, Any]:
        return {"running": self._scheduler_running}

    # 对外接口
    async def run_inspection(self, req: InspectionRunRequest) -> Dict[str, Any]:
        """执行一次巡检（同步）"""
        self._ensure_initialized()

        start_ts = time.time()
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=req.time_window_minutes)

        # 采集阶段（按配置并行，最小可用）
        pods_task = self._collect_pods(namespace=req.namespace)
        events_task = (
            self._collect_events(namespace=req.namespace)
            if req.include_events
            else asyncio.sleep(0, result=[])
        )
        prom_task = self._collect_prometheus(namespace=req.namespace, start=start_time, end=end_time)
        nodes_task = self._collect_nodes()
        services_task = self._collect_services(namespace=req.namespace)
        endpoints_task = self._collect_endpoints(namespace=req.namespace)
        rq_task = self._collect_resource_quotas(namespace=req.namespace)
        pvcs_task = self._collect_pvcs(namespace=req.namespace)

        pods, events, prom_series, nodes, services, endpoints, rqs, pvcs = await asyncio.gather(
            pods_task, events_task, prom_task, nodes_task, services_task, endpoints_task, rq_task, pvcs_task
        )

        # 规则检查（按 Profile 执行）
        findings = self._apply_profile_rules(
            profiles=req.profiles or [config.inspection.default_profile],
            namespace=req.namespace,
            pods=pods,
            events=events,
            prom=prom_series,
            nodes=nodes,
            services=services,
            endpoints=endpoints,
            resource_quotas=rqs,
            pvcs=pvcs,
        )

        # 过滤阈值与分级聚合
        filtered = [
            f for f in findings if severity_to_score(f.severity) >= req.severity_threshold
        ]
        high = sum(1 for f in filtered if f.severity == "high")
        medium = sum(1 for f in filtered if f.severity == "medium")
        low = sum(1 for f in filtered if f.severity == "low")

        report_id = f"insp-{int(time.time()*1000)}"
        report = InspectionReport(
            report_id=report_id,
            summary=InspectionSummary(
                scope=req.scope,
                namespace=req.namespace,
                time_window_minutes=req.time_window_minutes,
                total_checks=len(findings),
                issues_found=len(filtered),
                high=high,
                medium=medium,
                low=low,
            ),
            findings=filtered,
            stats={
                "prom_queries": len(prom_series.get("queries", [])) if isinstance(prom_series, dict) else 0,
                "k8s_calls": (1 + (1 if req.include_events else 0)),
                "duration_sec": round(time.time() - start_ts, 3),
            },
            recommendations=self._collect_recommendations(filtered),
            timestamp=end_time.isoformat(),
        )

        # 保留历史
        if config.inspection.retention_enabled:
            self._reports[report_id] = report
            # 简单保留上限
            if len(self._reports) > max(10, config.inspection.max_reports):
                # FIFO 删除最旧
                oldest_id = sorted(self._reports.keys())[0]
                self._reports.pop(oldest_id, None)

        # 高危项通知（可选）
        try:
            if any(f.severity == "high" for f in filtered):
                message = f"巡检发现 {high} 个高危问题，报告ID: {report_id}"
                try:
                    notif = NotificationService()
                    await notif.initialize()
                    await notif.send_notification("巡检高危告警", message, notification_type="critical")
                except Exception:
                    pass
        except Exception:
            pass

        return report.model_dump()

    async def run_inspection_async(self, req: InspectionRunRequest) -> Dict[str, Any]:
        """创建一个异步巡检任务（简单内存任务）"""
        self._ensure_initialized()
        task_id = f"task-{int(time.time()*1000)}"
        self._tasks[task_id] = InspectionTaskStatus(task_id=task_id, status="running")

        async def _job():
            try:
                result = await self.run_inspection(req)
                report_id = result.get("report_id")
                self._tasks[task_id] = InspectionTaskStatus(
                    task_id=task_id, status="completed", progress=1.0, report_id=report_id
                )
            except Exception as e:
                self._tasks[task_id] = InspectionTaskStatus(
                    task_id=task_id, status="failed", progress=1.0, error=str(e)
                )

        asyncio.create_task(_job())
        return {"task_id": task_id, "status": "running"}

    async def get_task_status(self, task_id: str) -> Dict[str, Any]:
        task = self._tasks.get(task_id)
        if not task:
            return {"task_id": task_id, "status": "not_found"}
        return task.model_dump()

    async def list_history(self, limit: int = 50) -> Dict[str, Any]:
        ids = sorted(self._reports.keys(), reverse=True)[:limit]
        items = [self._reports[i].summary.model_dump() | {"report_id": i} for i in ids]
        return {"items": items, "total": len(items)}

    async def get_report(self, report_id: str) -> Dict[str, Any]:
        report = self._reports.get(report_id)
        if not report:
            return {"error": "report_not_found", "report_id": report_id}
        return report.model_dump()

    # 采集实现（最小）
    async def _collect_pods(self, namespace: Optional[str]) -> List[Dict[str, Any]]:
        if not self._k8s:
            return []
        try:
            return await self._k8s.get_pods(namespace=namespace)
        except Exception:
            return []

    async def _collect_events(self, namespace: Optional[str]) -> List[Dict[str, Any]]:
        if not self._k8s:
            return []
        try:
            return await self._k8s.get_events(namespace=namespace, limit=200)
        except Exception:
            return []

    async def _collect_nodes(self) -> List[Dict[str, Any]]:
        if not self._k8s:
            return []
        try:
            return await self._k8s.list_nodes()
        except Exception:
            return []

    async def _collect_services(self, namespace: Optional[str]) -> List[Dict[str, Any]]:
        if not self._k8s:
            return []
        try:
            return await self._k8s.list_services(namespace=namespace)
        except Exception:
            return []

    async def _collect_endpoints(self, namespace: Optional[str]) -> List[Dict[str, Any]]:
        if not self._k8s:
            return []
        try:
            return await self._k8s.list_endpoints(namespace=namespace)
        except Exception:
            return []

    async def _collect_resource_quotas(self, namespace: Optional[str]) -> List[Dict[str, Any]]:
        if not self._k8s or not namespace:
            return []
        try:
            return await self._k8s.list_resource_quotas(namespace=namespace)
        except Exception:
            return []

    async def _collect_pvcs(self, namespace: Optional[str]) -> List[Dict[str, Any]]:
        if not self._k8s:
            return []
        try:
            return await self._k8s.list_pvcs(namespace=namespace)
        except Exception:
            return []

    async def _collect_prometheus(
        self, namespace: Optional[str], start: datetime, end: datetime
    ) -> Dict[str, Any]:
        if not self._prom:
            return {"queries": [], "series": []}

        queries: List[str] = [
            # CPU 限流
            f'rate(container_cpu_cfs_throttled_seconds_total{{namespace="{namespace}"}}[5m])'
            if namespace
            else 'rate(container_cpu_cfs_throttled_seconds_total[5m])',
            # Pod 重启
            f'increase(kube_pod_container_status_restarts_total{{namespace="{namespace}"}}[1h])'
            if namespace
            else 'increase(kube_pod_container_status_restarts_total[1h])',
            # HTTP 错误率（示例）
            (
                f'rate(http_requests_total{{namespace="{namespace}",code=~"5.."}}[5m])'
                if namespace
                else 'rate(http_requests_total{code=~"5.."}[5m])'
            ),
        ]

        series: List[Any] = []
        for q in queries:
            try:
                df = await self._prom.query_range(q, start, end, step=config.inspection.prometheus_step)
                if df is not None:
                    series.append({"query": q, "points": int(df.shape[0])})
            except Exception:
                continue

        return {"queries": queries, "series": series}

    # 规则（profile 驱动）
    def _apply_profile_rules(
        self,
        profiles: List[str],
        namespace: Optional[str],
        pods: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
        prom: Dict[str, Any],
        nodes: List[Dict[str, Any]] = None,
        services: List[Dict[str, Any]] = None,
        endpoints: List[Dict[str, Any]] = None,
        resource_quotas: List[Dict[str, Any]] = None,
        pvcs: List[Dict[str, Any]] = None,
    ) -> List[InspectionFinding]:
        ctx = RuleContext(
            pods=pods,
            events=events,
            prom=prom,
            namespace=namespace,
            nodes=nodes or [],
            services=services or [],
            endpoints=endpoints or [],
            resource_quotas=resource_quotas or [],
            pvcs=pvcs or [],
        )
        flat: List[InspectionFinding] = []
        for p in profiles:
            for rule in get_profile_rules(p):
                try:
                    for f in rule.check(ctx):
                        flat.append(
                            InspectionFinding(
                                rule_id=f["rule_id"],
                                title=f["title"],
                                severity=f.get("severity", "low"),
                                resource=f.get("resource", {}),
                                description=f.get("description", ""),
                                evidence=f.get("evidence", []),
                                recommendations=f.get("recommendations", []),
                            )
                        )
                except Exception:
                    continue
        return flat

    def _collect_recommendations(self, findings: List[InspectionFinding]) -> List[str]:
        recs: List[str] = []
        for f in findings:
            for r in f.recommendations:
                if r not in recs:
                    recs.append(r)
        # 附加通用建议
        if any(f.severity == "high" for f in findings):
            recs.append("为核心服务启用 HPA/VPA")
            recs.append("配置 PDB 保障可用性")
        return recs[:10]


