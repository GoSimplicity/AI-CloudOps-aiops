#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 根因分析器 - 核心故障诊断引擎
"""

import logging
import time
from datetime import datetime, timedelta, timezone
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

logger = logging.getLogger("aiops.rca")

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))


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

            if not metrics:
                metrics = config.rca.default_metrics

            # 解析可选采集开关（支持请求级覆盖：rca.request_override=true）
            ns = namespace or config.k8s.namespace
            if config.rca.request_override:
                # 请求级优先：只要请求显式为 True 即启用（前提是全局功能开启）
                should_collect_logs = bool(include_logs) and config.logs.enabled
                should_collect_traces = bool(include_traces) and config.tracing.enabled
            else:
                # 全局优先：仅当全局开启时生效，请求仅在为 None 时跟随全局
                should_collect_logs = (
                    config.logs.enabled if include_logs is None else (include_logs and config.logs.enabled)
                )
                should_collect_traces = (
                    config.tracing.enabled if include_traces is None else (include_traces and config.tracing.enabled)
                )

            metrics_data = await self._collect_metrics_data(
                start_time, end_time, metrics
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

            root_causes = self._generate_root_cause_candidates(anomalies, correlations)

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

            summary = await self._generate_summary(anomalies, correlations, root_causes)
            analysis_duration = time.time() - analysis_start_ts

            # 计算生效参数（用于回传给前端展示）
            service_name_effective = service_name or getattr(config.tracing, "service_name", None)

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
                "analysis_time": datetime.now(BEIJING_TZ).isoformat(),
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
    ) -> Dict:
        """相关性分析包装方法"""
        metrics = metrics or config.rca.default_metrics
        metrics_data = await self._collect_metrics_data(start_time, end_time, metrics)
        if not metrics_data:
            return {}
        all_corr = await self.correlator.analyze_correlations(metrics_data)
        if target_metric:
            return {target_metric: all_corr.get(target_metric, [])}
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
        self, start_time: datetime, end_time: datetime, metrics: List[str]
    ) -> Dict[str, pd.DataFrame]:
        """收集指标数据"""
        metrics_data = {}
        logger.info(
            f"开始收集指标数据: {start_time} - {end_time}, 指标数: {len(metrics)}"
        )

        for metric in metrics:
            try:
                logger.debug(f"查询指标: {metric}")
                data = await self.prometheus.query_range(
                    metric, start_time, end_time, "1m"
                )

                if data is not None and not data.empty and len(data) > 0:
                    label_columns = [
                        col for col in data.columns if col.startswith("label_")
                    ]

                    if label_columns:
                        for _, group in data.groupby(label_columns[0]):
                            if len(group) > 0:
                                label_value = group[label_columns[0]].iloc[0]
                                if (
                                    pd.notna(label_value)
                                    and str(label_value).strip()
                                ):
                                    metric_name = f"{metric}|{label_columns[0].replace('label_', '')}:{label_value}"
                                    metrics_data[metric_name] = group[
                                        ["value"]
                                    ].copy()
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
            if not candidates:
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
