#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd

from app.config.settings import config
from app.core.rca.collectors.k8s_events_collector import K8sEventsCollector
from app.core.rca.collectors.k8s_state_collector import K8sStateCollector
from app.core.rca.collectors.logs_collector import LogsCollector
from app.core.rca.collectors.tracing_collector import TracingCollector
from app.core.rca.correlator import CorrelationAnalyzer
from app.core.rca.detector import AnomalyDetector
from app.core.rca.rules.engine import RuleEngine
from app.core.rca.topology.graph import build_topology_from_state
from app.models.response_models import AnomalyInfo, RootCauseCandidate
from app.services.kubernetes import KubernetesService
from app.services.llm import LLMService
from app.services.prometheus import PrometheusService
from app.utils.time_utils import iso_utc_now

logger = logging.getLogger("aiops.rca")

UTC_TZ = timezone.utc


class RCAAnalyzer:
    """根因分析器 - 核心故障诊断引擎"""

    def __init__(self):
        """初始化根因分析器"""
        self.prometheus = PrometheusService()
        self.kubernetes = KubernetesService()
        self.detector = AnomalyDetector(config.rca.anomaly_threshold)
        self.correlator = CorrelationAnalyzer(config.rca.correlation_threshold)
        self.llm = LLMService()
        logger.info("根因分析器初始化完成")

    async def analyze(
        self,
        start_time: datetime,
        end_time: datetime,
        metrics: Optional[List[str]] = None,
        include_logs: Optional[bool] = None,
        include_traces: Optional[bool] = None,
        namespace: Optional[str] = None,
        service_name: Optional[str] = None,
    ) -> Dict:
        """执行全面的根因分析"""
        analysis_start_ts = time.time()
        try:
            logger.info(f"开始根因分析: {start_time} - {end_time}")

            # 规范化与合并指标：避免将字符串当作可迭代逐字符查询
            def _ensure_metric_list(value) -> List[str]:
                try:
                    if value is None:
                        return []
                    if isinstance(value, list):
                        return [str(x).strip() for x in value if str(x).strip()]
                    # 若为字符串，尝试按逗号切分；否则作为单一指标
                    text = str(value).strip()
                    if not text:
                        return []
                    if "," in text:
                        return [p.strip().strip("'\"") for p in text.split(",") if p.strip()]
                    return [text]
                except Exception:
                    return []

            input_metrics = _ensure_metric_list(metrics)
            default_metrics = _ensure_metric_list(config.rca.default_metrics)
            # 合并去重并保持顺序
            metrics = list(dict.fromkeys(input_metrics + default_metrics)) or default_metrics

            # 解析可选采集开关
            ns = namespace or config.k8s.namespace
            # 明确请求优先：include_logs 显式 True 时启用日志采集；否则跟随全局
            should_collect_logs = bool(include_logs) if include_logs is not None else bool(
                getattr(config, "logs", None) and config.logs.enabled
            )
            # Trace 暂不启用，但保持逻辑完整
            should_collect_traces = (
                bool(include_traces)
                if include_traces is not None
                else bool(getattr(config, "tracing", None) and config.tracing.enabled)
            )

            # 项目目前仅支持基于K8s的事件与状态分析，暂不支持Trace分析
            should_collect_traces = False

            metrics_data = await self._collect_metrics_data(
                start_time, end_time, metrics, namespace=ns
            )

            if not metrics_data:
                return {"error": "未获取到有效的监控数据"}

            logger.info(f"收集到 {len(metrics_data)} 个指标的数据")

            anomalies = await self.detector.detect_anomalies(metrics_data)
            logger.info(f"检测到 {len(anomalies)} 个指标存在异常")

            correlations = await self.correlator.analyze_correlations(metrics_data)
            try:
                lag_result = await self.correlator.analyze_correlations_with_cross_lag(
                    metrics_data, max_lags=5
                )
            except Exception:
                lag_result = {"correlations": correlations, "cross_correlations": {}}
            logger.info(f"分析了 {len(correlations)} 个指标的相关性")

            root_causes: List[Dict] = []

            context_events: List[Dict] = []
            context_state: Dict = {}
            context_topology: Dict = {}
            context_logs: List[Dict] = []
            context_traces: List[Dict] = []
            try:
                events_collector = K8sEventsCollector(namespace=ns)
                state_collector = K8sStateCollector(namespace=ns)
                context_events = await events_collector.pull(limit=200)
                context_state = await state_collector.snapshot()
                context_topology = build_topology_from_state(context_state).to_dict()
                # 日志与Trace为可选能力，失败不影响主流程
                if should_collect_logs:
                    try:
                        context_logs = await LogsCollector(namespace=ns).pull()
                    except Exception:
                        context_logs = []
                if should_collect_traces:
                    try:
                        context_traces = await TracingCollector().pull(
                            start_time, end_time, service=service_name
                        )
                    except Exception:
                        context_traces = []
            except Exception:
                pass

            # 若指标未发现异常，尝试从 K8s 事件构造“伪异常”，用于生成候选与摘要
            if not anomalies and context_events:
                try:
                    synthesized: Dict[str, Dict] = {}
                    for ev in context_events:
                        ev_type = str(ev.get("type") or "").lower()
                        reason = str(ev.get("reason") or "").lower()
                        first_ts = ev.get("firstTimestamp")
                        last_ts = ev.get("lastTimestamp")
                        # 仅统计 Warning 级别及常见异常信号
                        if ev_type == "warning" or reason in {"unhealthy", "backoff", "failedcreate", "failedscheduling", "crashloopbackoff"}:
                            # 统一映射为稳定的“指标名”
                            if "backoff" in reason or "crash" in reason:
                                key = "pod_restart_backoff"
                            elif "unhealthy" in reason:
                                key = "pod_probe_unhealthy"
                            elif "failedscheduling" in reason:
                                key = "scheduling_failed"
                            elif "failedcreate" in reason:
                                key = "workload_failedcreate"
                            else:
                                key = f"k8s_event_{reason or 'warning'}"
                            entry = synthesized.setdefault(
                                key,
                                {
                                    "count": 0,
                                    "first_occurrence": None,
                                    "last_occurrence": None,
                                    "max_score": 0.9,
                                    "avg_score": 0.8,
                                    "detection_methods": {"k8s_events": 0},
                                },
                            )
                            entry["count"] += 1
                            entry["detection_methods"]["k8s_events"] += 1
                            try:
                                if first_ts and (
                                    not entry["first_occurrence"]
                                    or str(first_ts) < str(entry["first_occurrence"])
                                ):
                                    entry["first_occurrence"] = str(first_ts)
                                if last_ts and (
                                    not entry["last_occurrence"]
                                    or str(last_ts) > str(entry["last_occurrence"])
                                ):
                                    entry["last_occurrence"] = str(last_ts)
                            except Exception:
                                pass
                    if synthesized:
                        anomalies = synthesized
                        logger.info(
                            f"基于K8s事件合成异常 {len(anomalies)} 项，用于生成候选与摘要"
                        )
                except Exception:
                    pass

            rule_evidence: List[Dict] = []
            try:
                engine = RuleEngine()
                engine.load_builtin()
                rule_evidence = engine.evaluate(
                    {
                        "events": context_events,
                        "state": context_state,
                        "metrics": list(metrics_data.keys()),
                        "logs": context_logs,
                        "traces": context_traces,
                    }
                )
            except Exception:
                pass

            # 事件合成异常完成后再生成根因候选
            root_causes = self._generate_root_cause_candidates(anomalies, correlations)

            summary = await self._generate_summary(anomalies, correlations, root_causes)
            analysis_duration = time.time() - analysis_start_ts

            # 计算生效参数（用于回传给前端展示）
            service_name_effective = service_name or getattr(
                config.tracing, "service_name", None
            )

            response = {
                "status": "success",
                "anomalies": {
                    metric: AnomalyInfo(**info).__dict__
                    for metric, info in anomalies.items()
                },
                "correlations": correlations,
                "cross_correlations": lag_result.get("cross_correlations", {}),
                "root_cause_candidates": [
                    RootCauseCandidate(**candidate).__dict__
                    for candidate in root_causes
                ],
                "analysis_time": iso_utc_now(),
                "time_range": {
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat(),
                },
                "metrics_analyzed": list(metrics_data.keys()),
                "summary": summary,
                "statistics": {
                    "total_metrics": len(metrics_data),
                    "anomalous_metrics": len(anomalies),
                    "correlation_pairs": sum(
                        len(corrs) for corrs in correlations.values()
                    ),
                    "analysis_duration": analysis_duration,
                },
                "logs_enabled_effective": bool(should_collect_logs),
                "traces_enabled_effective": bool(should_collect_traces),
                "events": context_events,
                "state": {
                    "namespace": context_state.get("namespace"),
                    "pods": len(context_state.get("pods") or []),
                    "deployments": len(context_state.get("deployments") or []),
                    "services": len(context_state.get("services") or []),
                },
                "topology": context_topology,
                "logs": context_logs[:10] if context_logs else [],
                "traces": context_traces[:20] if context_traces else [],
                "evidence": rule_evidence,
                "timeline": await self.generate_timeline(
                    start_time, end_time, context_events
                ),
                "impact_scope": [],
                "suggestions": self._generate_suggestions_from_causes(root_causes),
                "namespace_effective": ns,
                "service_name_effective": service_name_effective,
            }

            logger.info("根因分析完成")
            return response

        except Exception as e:
            logger.error(f"根因分析失败: {str(e)}")
            return {"error": f"分析失败: {str(e)}"}

    async def detect_anomalies(
        self,
        start_time: datetime,
        end_time: datetime,
        metrics: Optional[List[str]] = None,
        sensitivity: Optional[float] = None,
    ) -> Dict:
        """异常检测包装方法"""
        metrics = metrics or config.rca.default_metrics
        metrics_data = await self._collect_metrics_data(start_time, end_time, metrics)
        if not metrics_data:
            return {}
        return await self.detector.detect_anomalies(metrics_data)

    async def analyze_correlations(
        self,
        start_time: datetime,
        end_time: datetime,
        target_metric: Optional[str] = None,
        metrics: Optional[List[str]] = None,
        namespace: Optional[str] = None,
    ) -> Dict:
        """相关性分析包装方法"""
        metrics = metrics or config.rca.default_metrics
        # 确保目标指标被纳入分析集合
        if target_metric and target_metric not in (metrics or []):
            metrics = list(dict.fromkeys([target_metric] + list(metrics)))
        effective_ns = namespace or config.k8s.namespace
        metrics_data = await self._collect_metrics_data(
            start_time, end_time, metrics, namespace=effective_ns
        )
        # 若按命名空间无数据，回退为全局范围再试一次，避免直接返回空结果
        if not metrics_data and namespace:
            try:
                logger.warning(
                    f"命名空间 {namespace} 无数据，回退为全局相关性分析"
                )
                metrics_data = await self._collect_metrics_data(
                    start_time, end_time, metrics, namespace=None
                )
            except Exception:
                metrics_data = {}
        if not metrics_data:
            return {}
        # 使用新管线优先获取目标基础指标的Top相关对（带多级阈值与视图兜底）
        try:
            if target_metric:
                from app.core.rca.correlator import Correlator as _Correlator
                view_corr = _Correlator(config.rca.correlation_threshold).analyze_target_with_views(
                    metrics_data, target_metric=target_metric, namespace=namespace, thresholds=[
                        float(getattr(config.rca, "correlation_threshold", 0.7) or 0.7),
                        0.5,
                        0.3,
                        0.0,
                    ]
                )
                # 若有结果，直接返回；否则回退到原算法
                if view_corr and list(view_corr.values()) and list(view_corr.values())[0]:
                    return view_corr
        except Exception:
            pass
        all_corr = await self.correlator.analyze_correlations(metrics_data)
        if target_metric:
            # 按基础指标名（去掉标签后缀）聚合该目标的所有序列相关性
            # all_corr 的键为 “metric|k:v,...” 形式，或无后缀
            target_keys = [
                key
                for key in all_corr.keys()
                if key == target_metric or key.startswith(f"{target_metric}|")
            ]
            if not target_keys:
                # 若目标指标在当前过滤条件下完全缺失，则尝试“全局范围+放宽阈值”的兜底回退
                try:
                    # 回退：全局范围重取数据
                    metrics_data_global = await self._collect_metrics_data(
                        start_time, end_time, metrics, namespace=None
                    )
                    if metrics_data_global:
                        try:
                            # 放宽阈值（例如 0.5），以避免过严导致完全空结果
                            relaxed_threshold = min(
                                float(getattr(config.rca, "correlation_threshold", 0.7) or 0.7), 0.5
                            )
                        except Exception:
                            relaxed_threshold = 0.5
                        from app.core.rca.correlator import CorrelationAnalyzer as _CA
                        ca_relaxed = _CA(relaxed_threshold)
                        all_corr_relaxed = await ca_relaxed.analyze_correlations(metrics_data_global)
                        target_keys_relaxed = [
                            key
                            for key in all_corr_relaxed.keys()
                            if key == target_metric or key.startswith(f"{target_metric}|")
                        ]
                        if target_keys_relaxed:
                            aggregated_relaxed: Dict[str, float] = {}
                            for tkey in target_keys_relaxed:
                                for (other_name, corr_val) in all_corr_relaxed.get(tkey, []) or []:
                                    base_other = other_name.split("|")[0]
                                    prev = aggregated_relaxed.get(base_other)
                                    if prev is None or abs(corr_val) > abs(prev):
                                        aggregated_relaxed[base_other] = float(corr_val)
                            filtered_relaxed = {
                                name: val for name, val in aggregated_relaxed.items() if name != target_metric
                            }
                            aggregated_list_relaxed = sorted(
                                [(name, round(val, 3)) for name, val in filtered_relaxed.items()],
                                key=lambda x: abs(x[1]),
                                reverse=True,
                            )
                            if aggregated_list_relaxed:
                                logger.warning(
                                    f"目标 {target_metric} 在命名空间过滤下无相关性，已回退为全局并放宽阈值，返回 {len(aggregated_list_relaxed)} 项"
                                )
                                return {target_metric: aggregated_list_relaxed[:10]}
                except Exception:
                    pass
                return {target_metric: []}
            aggregated: Dict[str, float] = {}
            for tkey in target_keys:
                for (other_name, corr_val) in all_corr.get(tkey, []) or []:
                    base_other = other_name.split("|")[0]
                    prev = aggregated.get(base_other)
                    # 取绝对值更大的相关性（保留符号）
                    if prev is None or abs(corr_val) > abs(prev):
                        aggregated[base_other] = float(corr_val)
            # 排序并限制返回数量
            # 过滤掉与目标基础名相同的条目，避免“自身强相关”干扰
            filtered = {
                name: val for name, val in aggregated.items() if name != target_metric
            }
            aggregated_list = sorted(
                [(name, round(val, 3)) for name, val in filtered.items()],
                key=lambda x: abs(x[1]),
                reverse=True,
            )
            if not aggregated_list and namespace:
                # 若存在目标但结果为空，进一步尝试“全局范围+放宽阈值”兜底（目标序列存在但不满足阈值）
                try:
                    metrics_data_global = await self._collect_metrics_data(
                        start_time, end_time, metrics, namespace=None
                    )
                    if metrics_data_global:
                        try:
                            relaxed_threshold = min(
                                float(getattr(config.rca, "correlation_threshold", 0.7) or 0.7), 0.5
                            )
                        except Exception:
                            relaxed_threshold = 0.5
                        from app.core.rca.correlator import CorrelationAnalyzer as _CA
                        ca_relaxed = _CA(relaxed_threshold)
                        all_corr_relaxed = await ca_relaxed.analyze_correlations(metrics_data_global)
                        target_keys_relaxed = [
                            key
                            for key in all_corr_relaxed.keys()
                            if key == target_metric or key.startswith(f"{target_metric}|")
                        ]
                        aggregated_relaxed: Dict[str, float] = {}
                        for tkey in target_keys_relaxed:
                            for (other_name, corr_val) in all_corr_relaxed.get(tkey, []) or []:
                                base_other = other_name.split("|")[0]
                                prev = aggregated_relaxed.get(base_other)
                                if prev is None or abs(corr_val) > abs(prev):
                                    aggregated_relaxed[base_other] = float(corr_val)
                        filtered_relaxed = {
                            name: val for name, val in aggregated_relaxed.items() if name != target_metric
                        }
                        aggregated_list_relaxed = sorted(
                            [(name, round(val, 3)) for name, val in filtered_relaxed.items()],
                            key=lambda x: abs(x[1]),
                            reverse=True,
                        )
                        if aggregated_list_relaxed:
                            logger.warning(
                                f"目标 {target_metric} 在命名空间过滤下无显著相关性，已回退为全局并放宽阈值，返回 {len(aggregated_list_relaxed)} 项"
                            )
                            return {target_metric: aggregated_list_relaxed[:10]}
                        # 仍为空：最终兜底，阈值降为 0 返回 TOP5，避免空结果
                        try:
                            ca_zero = _CA(0.0)
                            all_corr_zero = await ca_zero.analyze_correlations(metrics_data_global)
                            target_keys_zero = [
                                key
                                for key in all_corr_zero.keys()
                                if key == target_metric or key.startswith(f"{target_metric}|")
                            ]
                            aggregated_zero: Dict[str, float] = {}
                            for tkey in target_keys_zero:
                                for (other_name, corr_val) in all_corr_zero.get(tkey, []) or []:
                                    base_other = other_name.split("|")[0]
                                    prev = aggregated_zero.get(base_other)
                                    if prev is None or abs(corr_val) > abs(prev):
                                        aggregated_zero[base_other] = float(corr_val)
                            filtered_zero = {
                                name: val for name, val in aggregated_zero.items() if name != target_metric
                            }
                            aggregated_list_zero = sorted(
                                [(name, round(val, 3)) for name, val in filtered_zero.items()],
                                key=lambda x: abs(x[1]),
                                reverse=True,
                            )
                            if aggregated_list_zero:
                                logger.warning(
                                    f"目标 {target_metric} 采用最终兜底（阈值=0，全局），返回 {len(aggregated_list_zero)} 项"
                                )
                                return {target_metric: aggregated_list_zero[:5]}
                        except Exception:
                            pass
                except Exception:
                    pass
            return {target_metric: aggregated_list[:10]}
        return all_corr

    async def generate_timeline(
        self,
        start_time: datetime,
        end_time: datetime,
        events: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        """生成时间线"""
        timeline: List[Dict] = []
        try:
            timeline.append(
                {
                    "timestamp": start_time.isoformat(),
                    "type": "start",
                    "message": "分析开始",
                }
            )
            if events:
                for e in events[:50]:
                    ts = e.get("firstTimestamp") or e.get("lastTimestamp")
                    timeline.append(
                        {
                            "timestamp": str(ts) if ts else None,
                            "type": "event",
                            "message": e.get("reason") or e.get("message"),
                        }
                    )
            timeline.append(
                {
                    "timestamp": end_time.isoformat(),
                    "type": "end",
                    "message": "分析结束",
                }
            )
        except Exception:
            pass
        return timeline

    def get_analysis_history(self, limit: int = 50) -> List[Dict]:
        """获取分析历史记录"""
        return []

    def is_healthy(self) -> bool:
        """检查分析器健康状态"""
        try:
            prom_ok = self.prometheus.is_healthy()
            k8s_ok = self.kubernetes.is_healthy()
            return bool(prom_ok and k8s_ok)
        except Exception:
            return False

    async def _collect_metrics_data(
        self,
        start_time: datetime,
        end_time: datetime,
        metrics: List[str],
        namespace: Optional[str] = None,
    ) -> Dict[str, pd.DataFrame]:
        """收集指标数据"""
        metrics_data = {}
        logger.info(
            f"开始收集指标数据: {start_time} - {end_time}, 指标数: {len(metrics)}"
        )

        for metric in metrics:
            try:
                logger.debug(f"查询指标: {metric}")
                data = await self.prometheus.query_range_async(
                    metric, start_time, end_time, "1m"
                )

                if data is not None and not data.empty and len(data) > 0:
                    # 如提供命名空间且数据包含命名空间标签，则先按命名空间过滤
                    if namespace and "label_namespace" in data.columns:
                        data = data[data["label_namespace"] == namespace]
                        if data is None or data.empty:
                            logger.debug(
                                f"指标 {metric} 在命名空间 {namespace} 下无数据，跳过"
                            )
                            continue

                    label_columns = [
                        col
                        for col in data.columns
                        if col.startswith("label_") and col != "label___name__"
                    ]

                    if label_columns:
                        # 以全部标签列进行分组，确保唯一序列不被混合
                        for labels_key, group in data.groupby(label_columns, dropna=False):
                            if len(group) > 0:
                                # 构造稳定的序列名称：metric|k1:v1,k2:v2
                                if not isinstance(labels_key, tuple):
                                    labels_key = (labels_key,)
                                parts = []
                                for col_name, col_value in zip(label_columns, labels_key):
                                    label_name = col_name.replace("label_", "")
                                    if pd.notna(col_value) and str(col_value).strip():
                                        parts.append(f"{label_name}:{col_value}")
                                label_suffix = ",".join(parts) if parts else "series"
                                metric_name = f"{metric}|{label_suffix}"
                                metrics_data[metric_name] = group[["value"]].copy()
                    else:
                        metrics_data[metric] = data[["value"]].copy()
                else:
                    logger.warning(f"指标 {metric} 无数据")

            except Exception as e:
                logger.error(f"获取指标 {metric} 失败: {str(e)}")
                continue

        metrics_data = {
            k: v for k, v in metrics_data.items() if not v.empty and len(v) > 0
        }
        logger.info(f"成功收集 {len(metrics_data)} 个时间序列")
        return metrics_data

    def _generate_root_cause_candidates(
        self, anomalies: Dict, correlations: Dict
    ) -> List[Dict]:
        """生成根因候选列表"""
        candidates = []
        try:
            for metric, anomaly_info in anomalies.items():
                if anomaly_info.get("count", 0) > 0:
                    confidence = self._calculate_confidence(
                        anomaly_info, correlations.get(metric, [])
                    )
                    description = self._generate_description(metric, anomaly_info)

                    candidate = {
                        "metric": metric,
                        "confidence": confidence,
                        "first_occurrence": anomaly_info.get("first_occurrence"),
                        "anomaly_count": anomaly_info.get("count"),
                        "related_metrics": correlations.get(metric, []),
                        "description": description,
                    }
                    candidates.append(candidate)

            candidates.sort(key=lambda x: x["confidence"], reverse=True)
            return candidates[:5]

        except Exception as e:
            logger.error(f"生成根因候选失败: {str(e)}")
            return []

    def _calculate_confidence(self, anomaly_info: Dict, related_metrics: List) -> float:
        """计算根因候选的置信度"""
        try:
            base_confidence = min(anomaly_info.get("max_score", 0), 1.0)
            count_factor = min(anomaly_info.get("count", 0) / 20, 0.3)
            correlation_factor = min(len(related_metrics) * 0.05, 0.2)

            detection_methods = anomaly_info.get("detection_methods", {})
            method_consistency = sum(
                1
                for v in detection_methods.values()
                if isinstance(v, (int, float)) and v > 0
            )
            consistency_factor = min(method_consistency * 0.05, 0.15)

            confidence = (
                base_confidence + count_factor + correlation_factor + consistency_factor
            )
            return min(confidence, 1.0)

        except Exception:
            return 0.0

    def _generate_description(self, metric: str, anomaly_info: Dict) -> str:
        """生成根因描述"""
        try:
            count = anomaly_info.get("count", 0)
            max_score = anomaly_info.get("max_score", 0)
            avg_score = anomaly_info.get("avg_score", 0)
            metric_lower = metric.lower()

            base_desc = f"检测到 {count} 个异常点，最高分数 {max_score:.2f}，平均分数 {avg_score:.2f}"

            if "cpu" in metric_lower:
                return f"CPU使用率异常，{base_desc}"
            elif "memory" in metric_lower:
                return f"内存使用异常，{base_desc}"
            elif "restart" in metric_lower:
                return f"容器重启异常，{base_desc}"
            elif any(kw in metric_lower for kw in ["network", "http", "request"]):
                return f"网络/HTTP请求异常，{base_desc}"
            elif "disk" in metric_lower or "storage" in metric_lower:
                return f"磁盘/存储异常，{base_desc}"
            elif "node" in metric_lower:
                return f"节点指标异常，{base_desc}"
            elif "pod" in metric_lower:
                return f"Pod状态异常，{base_desc}"
            else:
                return f"指标 {metric} 异常，{base_desc}"

        except Exception:
            return f"指标 {metric} 存在异常"

    async def _generate_summary(
        self, anomalies: Dict, correlations: Dict, candidates: List[Dict]
    ) -> Optional[str]:
        """生成AI摘要"""
        try:
            # 若没有候选，但存在异常，则返回基于异常的兜底摘要
            if not candidates:
                if anomalies:
                    try:
                        total_types = len(anomalies)
                        total_events = sum(int(v.get("count", 0)) for v in anomalies.values())
                        # 选取前3个最频繁的异常类型
                        top_items = sorted(
                            anomalies.items(), key=lambda kv: int(kv[1].get("count", 0)), reverse=True
                        )[:3]
                        top_desc = ", ".join(
                            f"{k}×{int(v.get('count', 0))}"
                            for k, v in top_items
                        )
                        return (
                            f"检测到 {total_types} 类异常，共 {total_events} 次；主要包括：{top_desc}。"
                        )
                    except Exception:
                        return "检测到异常，但无法生成详细摘要。"
                # 无候选且无异常
                return "未发现异常模式，系统运行正常。"

            summary = await self.llm.generate_rca_summary(
                anomalies, correlations, candidates
            )
            return summary or "无法生成分析摘要，但检测到异常。"

        except Exception as e:
            logger.error(f"生成摘要失败: {str(e)}")
            return None

    async def analyze_specific_incident(
        self,
        start_time: datetime,
        end_time: datetime,
        affected_services: List[str],
        symptoms: List[str],
    ) -> Dict:
        """分析特定事件的根因"""
        try:
            logger.info(f"分析事件: 服务={affected_services}, 症状={symptoms}")

            relevant_metrics = self._select_relevant_metrics(
                affected_services, symptoms
            )

            result = await self.analyze(start_time, end_time, relevant_metrics)

            if "error" not in result:
                result["incident_analysis"] = {
                    "affected_services": affected_services,
                    "reported_symptoms": symptoms,
                    "relevant_metrics": relevant_metrics,
                    "recommendation": self._generate_incident_recommendation(
                        result.get("root_cause_candidates", []),
                        affected_services,
                        symptoms,
                    ),
                }

            return result

        except Exception as e:
            logger.error(f"事件分析失败: {str(e)}")
            return {"error": f"事件分析失败: {str(e)}"}

    def _select_relevant_metrics(
        self, services: List[str], symptoms: List[str]
    ) -> List[str]:
        """选择相关指标"""
        relevant_metrics = set(config.rca.default_metrics)

        for symptom in symptoms:
            symptom_lower = symptom.lower()
            if "slow" in symptom_lower or "latency" in symptom_lower:
                relevant_metrics.update(
                    [
                        "kubelet_http_requests_duration_seconds_sum",
                        "kubelet_http_requests_duration_seconds_count",
                    ]
                )
            elif "error" in symptom_lower or "fail" in symptom_lower:
                relevant_metrics.update(["kube_pod_container_status_restarts_total"])
            elif "cpu" in symptom_lower:
                relevant_metrics.update(
                    ["container_cpu_usage_seconds_total", "node_cpu_seconds_total"]
                )
            elif "memory" in symptom_lower:
                relevant_metrics.update(
                    ["container_memory_working_set_bytes", "node_memory_MemFree_bytes"]
                )

        return list(relevant_metrics)

    def _generate_incident_recommendation(
        self, root_causes: List[Dict], services: List[str], symptoms: List[str]
    ) -> str:
        """
        生成事件处理建议 - 基于根因分析结果的智能建议系统
        """
        # 如果没有识别出根因，提供通用建议
        if not root_causes:
            return "建议检查服务配置和资源分配，监控系统负载变化。"

        # 获取置信度最高的根因候选
        top_cause = root_causes[0]
        metric = top_cause.get("metric", "")
        confidence = top_cause.get("confidence", 0)

        recommendations = []

        # 根据根因类型生成具体的修复建议
        if "cpu" in metric.lower():
            recommendations.append("检查CPU使用率，考虑扩容或优化应用性能")
        elif "memory" in metric.lower():
            recommendations.append(
                "检查内存使用情况，可能需要增加内存限制或优化内存使用"
            )
        elif "restart" in metric.lower():
            recommendations.append("检查容器重启原因，查看相关日志和健康检查配置")
        elif "network" in metric.lower() or "http" in metric.lower():
            recommendations.append("检查网络连接和服务间通信，查看负载均衡配置")

        # 根据置信度水平调整建议的紧迫性
        if confidence > 0.8:
            recommendations.append(
                f"根因分析置信度较高({confidence:.2f})，建议优先处理该问题"
            )
        elif confidence < 0.5:
            recommendations.append("根因分析置信度较低，建议进行更详细的调查")

        # 如果没有生成特定建议，提供通用建议
        return (
            "; ".join(recommendations)
            if recommendations
            else "建议进行详细的系统检查和日志分析。"
        )

    def _generate_suggestions_from_causes(self, root_causes: List[Dict]) -> List[str]:
        """根据根因候选生成简要建议清单（占位）。"""
        if not root_causes:
            return ["检查资源限额、就绪/存活探针与重启原因，留意K8s事件和容器日志。"]
        top = root_causes[0].get("metric", "").lower()
        if "cpu" in top:
            return ["提升CPU配额或副本数，或优化CPU热点逻辑。"]
        if "memory" in top:
            return ["提升内存配额，排查内存泄漏，关注OOMKill事件。"]
        if "restart" in top:
            return ["查看Pod重启原因，核验镜像/探针/配置。"]
        return ["根据异常指标对应的组件进行定向检查。"]
