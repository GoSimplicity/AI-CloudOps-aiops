#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps智能根因分析引擎
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.config.settings import CONFIG, config
from app.core.interfaces.llm_client import LLMClient, NullLLMClient
from app.models.rca_models import (
    CorrelationResult,
    EventData,
    LogData,
    MetricData,
    RootCause,
    RootCauseAnalysis,
    SeverityLevel,
)

from .base_collector import CollectionBatch
from .events_collector import EventsCollector
from .logs_collector import LogsCollector
from .metrics_collector import MetricsCollector


class RCAAnalysisEngine:
    """根因分析引擎"""

    # 根因模式定义
    ROOT_CAUSE_PATTERNS = {
        "OOM": {
            "metrics": [
                "container_memory_usage_bytes",
                "container_memory_working_set_bytes",
            ],
            "events": ["OOMKilled", "Killing"],
            "logs": ["out of memory", "oom", "memory exhausted"],
            "confidence_weight": 0.9,
        },
        "CPU_THROTTLING": {
            "metrics": [
                "container_cpu_cfs_throttled_periods_total",
                "container_cpu_usage_seconds_total",
            ],
            "events": ["CPUThrottling", "HighCPU"],
            "logs": ["cpu throttled", "high cpu usage"],
            "confidence_weight": 0.85,
        },
        "CRASH_LOOP": {
            "metrics": ["kube_pod_container_status_restarts_total"],
            "events": ["CrashLoopBackOff", "BackOff", "Failed"],
            "logs": [
                "panic",
                "fatal error",
                "segmentation fault",
                "restarting failed container",
            ],
            "confidence_weight": 0.95,
        },
        "NETWORK_ISSUE": {
            "metrics": [
                "container_network_receive_errors_total",
                "container_network_transmit_errors_total",
            ],
            "events": ["NetworkNotReady", "NetworkPluginNotReady"],
            "logs": ["connection refused", "timeout", "network unreachable"],
            "confidence_weight": 0.8,
        },
        "IMAGE_PULL": {
            "metrics": [],
            "events": ["ImagePullBackOff", "ErrImagePull"],
            "logs": ["pull access denied", "image not found"],
            "confidence_weight": 0.95,
        },
        "RESOURCE_QUOTA": {
            "metrics": ["kube_resourcequota"],
            "events": [
                "FailedScheduling",
                "InsufficientCPU",
                "InsufficientMemory",
                "FailedCreate",
            ],
            "logs": ["exceeded quota", "insufficient resources", "forbidden", "quota"],
            "confidence_weight": 0.9,
        },
        "DISK_PRESSURE": {
            "metrics": ["node_filesystem_avail_bytes", "node_filesystem_size_bytes"],
            "events": ["DiskPressure", "EvictedByNodeCondition"],
            "logs": ["no space left", "disk full"],
            "confidence_weight": 0.85,
        },
    }

    def __init__(
        self,
        config_dict: Optional[Dict[str, Any]] = None,
        llm_client: Optional[LLMClient] = None,
        metrics_collector: Optional[MetricsCollector] = None,
        events_collector: Optional[EventsCollector] = None,
        logs_collector: Optional[LogsCollector] = None,
    ):
        self.config = config_dict or {}
        self.logger = logging.getLogger("aiops.rca.engine")

        # 从配置文件读取RCA配置
        self.rca_config = config.rca
        self.anomaly_threshold = self.rca_config.anomaly_threshold
        self.correlation_threshold = self.rca_config.correlation_threshold
        # 从原始配置字典获取新增配置项
        rca_config_dict = CONFIG.get("rca", {})
        self.max_retries = rca_config_dict.get("max_retries", 3)
        self.timeout = rca_config_dict.get("timeout", 30)

        # 初始化收集器（支持依赖注入）
        self.metrics_collector = metrics_collector or MetricsCollector(config_dict)
        self.events_collector = events_collector or EventsCollector(config_dict)
        self.logs_collector = logs_collector or LogsCollector(config_dict)

        # 注入分析客户端（可为空实现）
        self.llm_service: LLMClient = llm_client or NullLLMClient()

        # 分析缓存
        self._analysis_cache = {}

    async def initialize(self) -> None:
        """初始化所有收集器"""
        results = await asyncio.gather(
            self.metrics_collector.initialize(),
            self.events_collector.initialize(),
            self.logs_collector.initialize(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                self.logger.warning("数据源初始化失败: %s", type(result).__name__)
        self.logger.info("RCA分析引擎初始化完成")

    async def analyze(
        self,
        namespace: str,
        time_window: timedelta = timedelta(hours=1),
        metrics: Optional[List[str]] = None,
    ) -> RootCauseAnalysis:
        """
        执行根因分析
        """
        # 确定时间范围
        end_time = datetime.now(timezone.utc)
        start_time = end_time - time_window

        self.logger.info(
            f"开始根因分析: namespace={namespace}, "
            f"time_range={start_time} to {end_time}"
        )

        # 并行收集三种数据
        metrics_data, events_data, logs_data, collection = await self._collect_all_data(
            namespace, start_time, end_time, metrics
        )

        if collection["status"] == "failed":
            return RootCauseAnalysis(
                timestamp=datetime.now(timezone.utc), namespace=namespace, root_causes=[],
                analysis_metadata={"collection": collection},
            )

        # 记录数据收集情况
        self._log_data_collection_summary(metrics_data, events_data, logs_data)

        # 执行多维度分析
        analysis_results = await self._perform_multi_dimensional_analysis(
            metrics_data, events_data, logs_data
        )

        # 关联分析
        correlations = self._correlate_data(
            analysis_results["metrics"],
            analysis_results["events"],
            analysis_results["logs"],
            start_time,
            end_time,
        )

        # 识别根因
        root_causes = self._identify_root_causes(
            analysis_results["metrics"],
            analysis_results["events"],
            analysis_results["logs"],
            correlations,
        )

        # 记录根因识别结果
        self._log_root_cause_analysis_results(root_causes)

        # 生成时间线
        timeline = self._build_timeline(metrics_data, events_data, logs_data)

        # 生成建议（异步）
        recommendations = await self._generate_recommendations(root_causes)

        # 计算数据完整性
        data_completeness = self._calculate_data_completeness(
            metrics_data, events_data, logs_data
        )

        # 生成分析报告（调用外部服务）
        analysis_report = await self._generate_analysis_report(
            metrics_data, events_data, logs_data, root_causes, data_completeness
        )

        # 整合异常数据
        anomalies = {
            "metrics": analysis_results["metrics"],
            "events": analysis_results["events"],
            "logs": analysis_results["logs"],
        }

        return RootCauseAnalysis(
            timestamp=datetime.now(timezone.utc),
            namespace=namespace,
            root_causes=root_causes,
            anomalies=anomalies,
            correlations=correlations,
            timeline=timeline,
            recommendations=recommendations,
            confidence_score=self._calculate_confidence(root_causes),
            analysis_metadata={
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "metrics_analyzed": len(metrics_data),
                "events_analyzed": len(events_data),
                "logs_analyzed": len(logs_data),
                "data_completeness": data_completeness,
                "collection": collection,
                "analysis_report": analysis_report,
            },
        )

    def _log_data_collection_summary(
        self, metrics: List[MetricData], events: List[EventData], logs: List[LogData]
    ) -> None:
        """
        记录数据收集的摘要信息

        Args:
            metrics: 收集到的指标数据
            events: 收集到的事件数据
            logs: 收集到的日志数据
        """
        # 记录收集到的数据量
        self.logger.info(
            f"数据收集完成: 指标数={len(metrics)}, 事件数={len(events)}, 日志数={len(logs)}"
        )

        # 详细记录收集到的指标
        if metrics:
            metric_names = [m.name for m in metrics]
            self.logger.info(f"收集到的指标: {metric_names}")
            for metric in metrics[:3]:  # 记录前3个指标的详细信息
                self.logger.debug(
                    f"指标 {metric.name}: 值数量={len(metric.values)}, "
                    f"异常分数={metric.anomaly_score:.2f}, 趋势={metric.trend}"
                )
        else:
            self.logger.warning("未收集到任何指标数据")

        # 记录关键事件
        if events:
            critical_events = [
                e
                for e in events
                if e.severity in [SeverityLevel.CRITICAL, SeverityLevel.HIGH]
            ]
            self.logger.info(f"关键事件数: {len(critical_events)}/{len(events)}")
        else:
            self.logger.warning("未收集到任何事件数据")

        # 记录错误日志
        if logs:
            error_logs = [log for log in logs if log.level in ["ERROR", "FATAL"]]
            self.logger.info(f"错误日志数: {len(error_logs)}/{len(logs)}")
        else:
            self.logger.warning("未收集到任何日志数据")

    async def _perform_multi_dimensional_analysis(
        self, metrics: List[MetricData], events: List[EventData], logs: List[LogData]
    ) -> Dict[str, Dict[str, Any]]:
        """
        执行多维度分析的统一方法

        Args:
            metrics: 指标数据
            events: 事件数据
            logs: 日志数据

        Returns:
            包含各维度分析结果的字典
        """
        # 执行多维度分析
        analysis_results = await asyncio.gather(
            self._analyze_metrics_anomalies(metrics),
            self._analyze_event_patterns(events),
            self._analyze_log_errors(logs),
            return_exceptions=True,
        )

        # 处理分析结果和异常
        metric_anomalies = self._handle_analysis_result(analysis_results[0], "指标分析")
        event_patterns = self._handle_analysis_result(analysis_results[1], "事件分析")
        log_patterns = self._handle_analysis_result(analysis_results[2], "日志分析")

        # 记录分析结果摘要
        self.logger.info(
            f"分析结果: 指标异常={len(metric_anomalies.get('high_anomaly_metrics', []))}, "
            f"关键事件={len(event_patterns.get('critical_events', []))}, "
            f"错误类型={len(log_patterns.get('error_types', {}))}"
        )

        return {
            "metrics": metric_anomalies,
            "events": event_patterns,
            "logs": log_patterns,
        }

    def _handle_analysis_result(
        self, result: Any, analysis_name: str
    ) -> Dict[str, Any]:
        """
        处理分析结果，统一异常处理
        """
        if isinstance(result, Exception):
            self.logger.error("%s失败: %s", analysis_name, type(result).__name__)
            return {}
        return result

    def _log_root_cause_analysis_results(self, root_causes: List[RootCause]) -> None:
        """
        记录根因分析结果的摘要
        """
        # 记录根因识别结果
        self.logger.info(f"根因识别完成: 发现 {len(root_causes)} 个根因")
        for i, cause in enumerate(root_causes):
            self.logger.info(
                f"根因 {i + 1}: {cause.cause_type} "
                f"(置信度: {cause.confidence:.2f}) - {cause.description}"
            )

        if not root_causes:
            self.logger.warning(
                "未识别到任何根因，可能原因：1)数据不足 2)没有明显异常 3)异常模式未知"
            )

    async def _collect_all_data(
        self,
        namespace: str,
        start_time: datetime,
        end_time: datetime,
        metrics: Optional[List[str]] = None,
    ) -> Tuple[List[MetricData], List[EventData], List[LogData], Dict[str, Any]]:
        """并行收集所有数据"""
        collect_tasks = [
            self.metrics_collector.collect(
                namespace, start_time, end_time, metrics=metrics
            ),
            self.events_collector.collect(namespace, start_time, end_time),
            self.logs_collector.collect(
                namespace,
                start_time,
                end_time,
                error_only=True,
                max_lines=self.logs_collector.error_lines,
            ),
        ]

        results = await asyncio.gather(*collect_tasks, return_exceptions=True)

        values = []
        sources = {}
        succeeded = failed = 0
        for name, result in zip(("metrics", "events", "logs"), results):
            if isinstance(result, Exception):
                batch = CollectionBatch(succeeded=0, failed=1)
                self.logger.warning("数据源 %s 采集失败: %s", name, type(result).__name__)
            elif isinstance(result, CollectionBatch):
                batch = result
            elif isinstance(result, list):
                batch = CollectionBatch(result)
            else:
                batch = CollectionBatch(succeeded=0, failed=1)
            succeeded += batch.succeeded
            failed += batch.failed
            status = ("partial" if batch.succeeded and batch.failed else
                      "failed" if batch.failed else "success" if batch else "empty")
            sources[name] = {
                "status": status, "count": len(batch),
                "succeeded": batch.succeeded, "failed": batch.failed,
                "error_category": batch.error_category if batch.failed else None,
            }
            values.append(batch)
        total = succeeded + failed
        collection = {
            "status": "failed" if succeeded == 0 else "partial" if failed else "complete",
            "coverage": None if any(source["error_category"] in ("truncated", "collection_incomplete") for source in sources.values()) else succeeded / total if total else 0.0,
            "coverage_basis": "completed_collection_units",
            "succeeded": succeeded, "failed": failed, "sources": sources,
        }
        return *values, collection

    async def _analyze_metrics_anomalies(
        self, metrics: List[MetricData]
    ) -> Dict[str, Any]:
        """分析指标异常"""
        anomalies = {
            "high_anomaly_metrics": [],
            "trending_metrics": [],
            "threshold_violations": [],
        }

        for metric in metrics:
            # 高异常分数的指标 - 降低阈值以提高敏感度
            if metric.anomaly_score > 0.5:  # 从0.7降低到0.5
                anomalies["high_anomaly_metrics"].append(
                    {
                        "name": metric.name,
                        "score": metric.anomaly_score,
                        "trend": metric.trend,
                        "labels": metric.labels,
                    }
                )

            # 趋势异常
            if metric.trend in ["increasing", "decreasing"]:
                anomalies["trending_metrics"].append(
                    {
                        "name": metric.name,
                        "trend": metric.trend,
                        "labels": metric.labels,
                    }
                )

            # 检查阈值违规
            violations = self._check_threshold_violations(metric)
            if violations:
                anomalies["threshold_violations"].extend(violations)

            # 即使没有超过高阈值，也记录中等异常
            elif metric.anomaly_score > 0.3 and metric.anomaly_score <= 0.5:
                anomalies["trending_metrics"].append(
                    {
                        "name": metric.name,
                        "score": metric.anomaly_score,
                        "trend": metric.trend,
                        "labels": metric.labels,
                    }
                )

        return anomalies

    async def _analyze_event_patterns(self, events: List[EventData]) -> Dict[str, Any]:
        """分析事件模式"""
        patterns = {
            "critical_events": [],
            "event_clusters": defaultdict(list),
            "frequency_spikes": [],
        }

        # 关键事件
        for event in events:
            if event.severity in [SeverityLevel.CRITICAL, SeverityLevel.HIGH]:
                patterns["critical_events"].append(
                    {
                        "timestamp": event.timestamp.isoformat(),
                        "reason": event.reason,
                        "message": event.message,
                        "object": event.involved_object,
                        "count": event.count,
                    }
                )

            # 事件聚类
            cluster_key = f"{event.reason}_{event.involved_object.get('kind')}"
            patterns["event_clusters"][cluster_key].append(event)

        # 检测频率异常
        patterns["frequency_spikes"] = self._detect_frequency_spikes(
            patterns["event_clusters"]
        )

        return patterns

    async def _analyze_log_errors(self, logs: List[LogData]) -> Dict[str, Any]:
        """分析日志错误"""
        patterns = {
            "error_types": defaultdict(list),
            "stack_traces": [],
            "error_frequency": defaultdict(int),
        }

        for log in logs:
            if log.level in ["ERROR", "FATAL"]:
                # 错误类型分组
                if log.error_type:
                    patterns["error_types"][log.error_type].append(
                        {
                            "timestamp": log.timestamp.isoformat(),
                            "pod": log.pod_name,
                            "message": log.message[:200],  # 截断消息
                        }
                    )

                # 收集堆栈跟踪
                if log.stack_trace:
                    patterns["stack_traces"].append(
                        {
                            "timestamp": log.timestamp.isoformat(),
                            "pod": log.pod_name,
                            "trace": log.stack_trace[
                                : self.logs_collector.max_message_length
                            ],  # 截断堆栈
                        }
                    )

                # 错误频率统计
                error_key = f"{log.pod_name}_{log.error_type or 'unknown'}"
                patterns["error_frequency"][error_key] += 1

        return patterns

    def _correlate_data(
        self,
        metric_anomalies: Dict[str, Any],
        event_patterns: Dict[str, Any],
        log_patterns: Dict[str, Any],
        start_time: datetime,
        end_time: datetime,
    ) -> List[CorrelationResult]:
        """关联三种数据源"""
        correlations = []

        # 时间关联 - 查找同一时间窗口内的异常
        time_correlated = self._find_temporal_correlations(
            metric_anomalies, event_patterns, log_patterns, start_time, end_time
        )
        if time_correlated:
            correlations.append(time_correlated)

        # 组件关联 - 查找影响同一组件的问题
        component_correlated = self._find_component_correlations(
            metric_anomalies, event_patterns, log_patterns
        )
        if component_correlated:
            correlations.append(component_correlated)

        # 因果关联 - 识别因果链
        causal_chain = self._find_causal_chain(
            metric_anomalies, event_patterns, log_patterns
        )
        if causal_chain:
            correlations.append(causal_chain)

        # 如果没有找到任何关联，添加数据不足的关联信息
        if not correlations:
            data_insufficient_correlation = self._create_data_insufficient_correlation(
                metric_anomalies, event_patterns, log_patterns, start_time, end_time
            )
            if data_insufficient_correlation:
                correlations.append(data_insufficient_correlation)

        return correlations

    def _identify_root_causes(
        self,
        metric_anomalies: Dict[str, Any],
        event_patterns: Dict[str, Any],
        log_patterns: Dict[str, Any],
        correlations: List[CorrelationResult],
    ) -> List[RootCause]:
        """识别根因"""
        root_causes = []

        # 基于模式匹配的根因识别
        self.logger.info("开始根因模式匹配...")
        for cause_type, pattern in self.ROOT_CAUSE_PATTERNS.items():
            confidence = self._match_pattern(
                pattern, metric_anomalies, event_patterns, log_patterns
            )

            self.logger.debug(f"模式匹配: {cause_type} - 置信度: {confidence:.2f}")

            if confidence > 0.2:  # 降低置信度阈值，从0.3降低到0.2
                self.logger.info(
                    f"模式匹配成功: {cause_type} (置信度: {confidence:.2f})"
                )
                root_cause = self._create_root_cause(
                    cause_type,
                    confidence,
                    metric_anomalies,
                    event_patterns,
                    log_patterns,
                )
                root_causes.append(root_cause)
            elif confidence > 0.1:  # 记录低置信度的匹配
                self.logger.debug(
                    f"低置信度匹配: {cause_type} (置信度: {confidence:.2f}) - 未达到阈值"
                )

        # 基于关联分析的根因识别
        for correlation in correlations:
            if correlation.confidence > 0.7:
                root_cause = self._derive_root_cause_from_correlation(correlation)
                if root_cause and root_cause not in root_causes:
                    root_causes.append(root_cause)

        # 按置信度排序
        root_causes.sort(key=lambda x: x.confidence, reverse=True)

        # 只返回前3个最可能的根因
        return root_causes[:3]

    def _match_pattern(
        self,
        pattern: Dict[str, Any],
        metric_anomalies: Dict[str, Any],
        event_patterns: Dict[str, Any],
        log_patterns: Dict[str, Any],
    ) -> float:
        """匹配根因模式"""
        matches = []

        # 检查指标匹配
        if pattern["metrics"]:
            metric_match = any(
                any(m in anomaly["name"] for m in pattern["metrics"])
                for anomaly in metric_anomalies.get("high_anomaly_metrics", [])
            )
            matches.append(metric_match)

        # 检查事件匹配
        if pattern["events"]:
            event_match = any(
                any(e in event["reason"] for e in pattern["events"])
                for event in event_patterns.get("critical_events", [])
            )
            matches.append(event_match)

        # 检查日志匹配
        if pattern["logs"]:
            log_match = any(
                any(keyword in log_type.lower() for keyword in pattern["logs"])
                for log_type in log_patterns.get("error_types", {}).keys()
            )
            matches.append(log_match)

        # 计算置信度
        if not matches:
            return 0.0

        match_ratio = sum(matches) / len(matches)
        return match_ratio * pattern["confidence_weight"]

    def _create_root_cause(
        self,
        cause_type: str,
        confidence: float,
        metric_anomalies: Dict[str, Any],
        event_patterns: Dict[str, Any],
        log_patterns: Dict[str, Any],
    ) -> RootCause:
        """创建根因对象"""
        descriptions = {
            "OOM": "内存不足导致容器被终止",
            "CPU_THROTTLING": "CPU限制导致性能下降",
            "CRASH_LOOP": "应用程序持续崩溃重启",
            "NETWORK_ISSUE": "网络连接问题",
            "IMAGE_PULL": "镜像拉取失败",
            "RESOURCE_QUOTA": "资源配额不足",
            "DISK_PRESSURE": "磁盘空间不足",
        }

        # 收集受影响的组件
        affected_components = set()
        for event in event_patterns.get("critical_events", []):
            obj = event.get("object", {})
            if obj.get("name"):
                affected_components.add(
                    f"{obj.get('kind', 'unknown')}/{obj.get('name')}"
                )

        return RootCause(
            cause_type=cause_type,
            description=descriptions.get(cause_type, "未知问题"),
            confidence=confidence,
            affected_components=list(affected_components)[:5],
            evidence={
                "metrics": metric_anomalies.get("high_anomaly_metrics", [])[:3],
                "events": event_patterns.get("critical_events", [])[:3],
                "logs": list(log_patterns.get("error_types", {}).keys())[:3],
            },
            recommendations=[],  # 将由LLM动态生成，不再硬编码
        )

    def _build_timeline(
        self, metrics: List[MetricData], events: List[EventData], logs: List[LogData]
    ) -> List[Dict[str, Any]]:
        """构建时间线"""
        timeline = []

        # 添加关键事件
        for event in events[:20]:  # 限制数量
            if event.severity in [SeverityLevel.CRITICAL, SeverityLevel.HIGH]:
                timeline.append(
                    {
                        "timestamp": event.timestamp.isoformat(),
                        "type": "event",
                        "severity": event.severity.value,
                        "description": f"{event.reason}: {event.message[:100]}",
                    }
                )

        # 添加错误日志
        for log in logs[:10]:  # 限制数量
            if log.level in ["ERROR", "FATAL"]:
                timeline.append(
                    {
                        "timestamp": log.timestamp.isoformat(),
                        "type": "log",
                        "severity": "high",
                        "description": f"[{log.pod_name}] {log.message[:100]}",
                    }
                )

        # 添加指标异常
        for metric in metrics:
            if metric.anomaly_score > 0.6:  # 降低阈值从0.8到0.6
                # 取最新的值
                if metric.values:
                    latest_value = metric.values[-1]
                    timeline.append(
                        {
                            "timestamp": latest_value["timestamp"],
                            "type": "metric",
                            "severity": "medium",
                            "description": f"指标异常 {metric.name}: score={metric.anomaly_score:.2f}",
                        }
                    )

        # 按时间排序
        timeline.sort(key=lambda x: x["timestamp"])

        return timeline[:50]  # 返回最多50个事件

    async def _generate_recommendations(
        self, root_causes: List[RootCause]
    ) -> List[str]:
        """生成综合建议"""
        if not self.llm_service:
            # 如果LLM服务不可用，返回基础建议
            if not root_causes:
                return [
                    "启用更详细的日志记录",
                    "增加监控覆盖范围",
                    "检查最近的配置变更",
                ]
            else:
                recommendations = []
                for cause in root_causes:
                    recommendations.extend(cause.recommendations)
                return list(set(recommendations))[:5]

        try:
            # 准备根因数据
            root_causes_data = []
            for cause in root_causes:
                root_causes_data.append(
                    {
                        "type": cause.cause_type,
                        "description": cause.description,
                        "confidence": cause.confidence,
                        "affected_components": cause.affected_components,
                        "evidence": cause.evidence,
                    }
                )

            # 构造请求
            system_prompt = """你是一个Kubernetes和云运维专家。基于根因分析结果，生成5个简洁实用的解决建议。
要求：
1. 每个建议不超过30个字
2. 按优先级排序，最重要的在前
3. 建议要具体可执行
4. 如果没有根因，提供通用的运维建议
5. 只返回建议文本列表，不要额外说明"""

            if root_causes_data:
                user_prompt = f"根据以下根因分析结果生成解决建议：\n{json.dumps(root_causes_data, ensure_ascii=False, indent=2)}"
            else:
                user_prompt = "当前没有发现明确的根因，请生成通用的系统运维和监控建议"

            # 生成建议
            response = await self.llm_service.generate_response(
                messages=[{"role": "user", "content": user_prompt}],
                system_prompt=system_prompt,
                temperature=0.7,
                max_tokens=300,
                use_task_model=True,  # 简单操作：生成建议列表，使用task_model
            )

            if response:
                # 解析响应为建议列表
                recommendations = self._parse_recommendations_from_llm_response(
                    response
                )
                if recommendations:
                    self.logger.info(f"LLM生成了 {len(recommendations)} 个建议")
                    return recommendations[:5]

        except Exception as e:
            self.logger.error("生成建议失败: %s", type(e).__name__)

        # 备用方案：返回基础建议
        if not root_causes:
            return ["启用更详细的日志记录", "增加监控覆盖范围", "检查最近的配置变更"]
        else:
            recommendations = []
            for cause in root_causes:
                recommendations.extend(cause.recommendations)
            return list(set(recommendations))[:5]

    def _parse_recommendations_from_llm_response(self, response: str) -> List[str]:
        """解析响应为建议列表"""
        try:
            # 首先尝试解析为JSON数组
            import json

            try:
                # 如果响应是JSON格式的列表
                recommendations = json.loads(response)
                if isinstance(recommendations, list):
                    # 确保每个建议都是字符串
                    return [str(r) for r in recommendations[:5]]
            except json.JSONDecodeError:
                pass  # 不是JSON格式，继续尝试其他解析方式

            # 尝试按行分割
            lines = response.strip().split("\n")
            recommendations = []

            for line in lines:
                # 清理行内容
                clean_line = line.strip()
                if not clean_line:
                    continue

                # 移除编号、破折号、星号等前缀
                import re

                clean_line = re.sub(r"^[\d\.\-\*\•\s]+", "", clean_line)
                clean_line = clean_line.strip()

                if clean_line and len(clean_line) > 5:  # 确保建议有实际内容
                    recommendations.append(clean_line)

            return recommendations[:5]  # 最多返回5个建议

        except Exception as e:
            self.logger.warning("解析建议响应失败: %s", type(e).__name__)
            # 尝试直接使用响应文本
            if response and len(response.strip()) > 10:
                return [response.strip()[:100]]  # 截断过长的响应
            return []

    def _calculate_confidence(self, root_causes: List[RootCause]) -> float:
        """计算总体置信度"""
        if not root_causes:
            return 0.0

        # 使用最高置信度
        return max(cause.confidence for cause in root_causes)

    def _check_threshold_violations(self, metric: MetricData) -> List[Dict[str, Any]]:
        """检查阈值违规"""
        violations = []

        # 定义常见指标的阈值
        thresholds = {
            "cpu": 0.8,  # 80% CPU使用率
            "memory": 0.9,  # 90% 内存使用率
            "disk": 0.85,  # 85% 磁盘使用率
            "error_rate": 0.01,  # 1% 错误率
        }

        metric_name_lower = metric.name.lower()

        for key, threshold in thresholds.items():
            if key in metric_name_lower:
                # 检查最新值
                if metric.values:
                    latest = metric.values[-1]
                    if latest["value"] > threshold:
                        violations.append(
                            {
                                "metric": metric.name,
                                "value": latest["value"],
                                "threshold": threshold,
                                "timestamp": latest["timestamp"],
                            }
                        )

        return violations

    def _detect_frequency_spikes(
        self, event_clusters: Dict[str, List[EventData]]
    ) -> List[Dict[str, Any]]:
        """检测事件频率峰值"""
        spikes = []

        for cluster_key, events in event_clusters.items():
            if len(events) > 5:  # 事件数量阈值
                # 计算事件频率
                timestamps = [e.timestamp for e in events]
                if len(timestamps) > 1:
                    time_diffs = [
                        (timestamps[i + 1] - timestamps[i]).total_seconds()
                        for i in range(len(timestamps) - 1)
                    ]
                    avg_interval = np.mean(time_diffs)

                    if avg_interval < 60:  # 平均间隔小于1分钟
                        spikes.append(
                            {
                                "cluster": cluster_key,
                                "count": len(events),
                                "avg_interval_seconds": avg_interval,
                                "first_event": timestamps[0].isoformat(),
                                "last_event": timestamps[-1].isoformat(),
                            }
                        )

        return spikes

    def _find_temporal_correlations(
        self,
        metric_anomalies: Dict[str, Any],
        event_patterns: Dict[str, Any],
        log_patterns: Dict[str, Any],
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> Optional[CorrelationResult]:
        """
        查找时间相关性
        """
        # 简化实现 - 实际应该使用更复杂的时间序列分析
        evidence = []

        if metric_anomalies.get("high_anomaly_metrics"):
            evidence.append("发现异常指标")
        if event_patterns.get("critical_events"):
            evidence.append("发现关键事件")
        if log_patterns.get("error_types"):
            evidence.append("发现错误日志")

        if len(evidence) >= 2:
            # 构建时间线
            timeline = []

            # 添加指标异常到时间线
            for metric in metric_anomalies.get("high_anomaly_metrics", []):
                # 使用传入的时间范围或当前时间
                timestamp = end_time if end_time else datetime.now(timezone.utc)
                timeline.append(
                    {
                        "timestamp": timestamp.isoformat(),
                        "type": "metric_anomaly",
                        "description": f"指标 {metric.get('name')} 异常 (异常分数: {metric.get('score', 0):.2f})",
                        "severity": "high"
                        if metric.get("score", 0) > 0.8
                        else "medium",
                    }
                )

            # 添加关键事件到时间线
            for event in event_patterns.get("critical_events", [])[:5]:  # 限制数量
                event_time = event.get(
                    "timestamp",
                    (end_time if end_time else datetime.now(timezone.utc)).isoformat(),
                )
                timeline.append(
                    {
                        "timestamp": event_time,
                        "type": "critical_event",
                        "description": f"{event.get('reason', '')}: {event.get('message', '')[:100]}",
                        "severity": event.get("severity", "high"),
                    }
                )

            # 按时间排序
            timeline.sort(key=lambda x: x["timestamp"])

            return CorrelationResult(
                confidence=0.7 + 0.1 * len(evidence),
                correlation_type="temporal",
                evidence=evidence,
                timeline=timeline[:10],  # 限制返回最多10条
            )

        return None

    def _find_component_correlations(
        self,
        metric_anomalies: Dict[str, Any],
        event_patterns: Dict[str, Any],
        log_patterns: Dict[str, Any],
    ) -> Optional[CorrelationResult]:
        """
        查找组件相关性
        """
        # 提取所有涉及的组件
        components = defaultdict(list)

        # 从事件中提取
        for event in event_patterns.get("critical_events", []):
            obj = event.get("object", {})
            if obj.get("name"):
                component_key = f"{obj.get('kind')}/{obj.get('name')}"
                components[component_key].append("event")

        # 从日志中提取
        for error_list in log_patterns.get("error_types", {}).values():
            for error in error_list:
                if error.get("pod"):
                    components[f"Pod/{error['pod']}"].append("log")

        # 从指标异常中提取（使用参数）
        for metric in metric_anomalies.get("high_anomaly_metrics", []):
            labels = metric.get("labels", {})
            if labels.get("pod"):
                components[f"Pod/{labels['pod']}"].append("metric")

        # 查找多数据源都涉及的组件
        multi_source_components = [
            comp for comp, sources in components.items() if len(set(sources)) > 1
        ]

        if multi_source_components:
            # 构建组件相关的时间线
            timeline = []
            for comp in multi_source_components[:3]:
                timeline.append(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "component_anomaly",
                        "description": f"组件 {comp} 在多个数据源中出现异常",
                        "component": comp,
                        "severity": "high",
                    }
                )

            return CorrelationResult(
                confidence=0.75,
                correlation_type="component",
                evidence=[
                    f"组件 {comp} 在多个数据源中出现异常"
                    for comp in multi_source_components[:3]
                ],
                timeline=timeline,
            )

        return None

    def _find_causal_chain(
        self,
        metric_anomalies: Dict[str, Any],
        event_patterns: Dict[str, Any],
        log_patterns: Dict[str, Any],
    ) -> Optional[CorrelationResult]:
        """
        识别因果链
        """
        # 简化的因果链检测
        chain = []

        # 检查常见的因果模式
        # 例如：资源不足 -> 调度失败 -> 服务不可用

        # 1. 检查资源不足（从事件和指标中）
        resource_insufficient = False

        # 从事件检查资源不足
        if any(
            "InsufficientMemory" in e.get("reason", "")
            or "InsufficientCPU" in e.get("reason", "")
            for e in event_patterns.get("critical_events", [])
        ):
            resource_insufficient = True
            chain.append("资源不足")

        # 从指标检查资源不足
        elif any(
            "memory" in metric.get("name", "").lower() and metric.get("score", 0) > 0.8
            for metric in metric_anomalies.get("high_anomaly_metrics", [])
        ):
            resource_insufficient = True
            chain.append("内存资源紧张")

        # 2. 检查调度失败
        if resource_insufficient and any(
            "FailedScheduling" in e.get("reason", "")
            for e in event_patterns.get("critical_events", [])
        ):
            chain.append("调度失败")

            # 3. 检查服务异常
            if log_patterns.get("error_frequency"):
                chain.append("服务异常")

        if len(chain) >= 2:
            # 构建因果链时间线
            timeline = []
            for i, step in enumerate(chain):
                timeline.append(
                    {
                        "timestamp": (
                            datetime.now(timezone.utc)
                            - timedelta(minutes=len(chain) - i)
                        ).isoformat(),
                        "type": "causal_step",
                        "description": step,
                        "step_number": i + 1,
                        "severity": "high" if i == 0 else "medium",
                    }
                )

            return CorrelationResult(
                confidence=0.8,
                correlation_type="causal",
                evidence=[f"因果链: {' -> '.join(chain)}"],
                timeline=timeline,
            )

        return None

    def _derive_root_cause_from_correlation(
        self, correlation: CorrelationResult
    ) -> Optional[RootCause]:
        """从关联结果推导根因"""
        if correlation.correlation_type == "causal" and correlation.confidence > 0.7:
            # 基于因果链创建根因
            return RootCause(
                cause_type="CORRELATION_BASED",
                description=f"基于关联分析: {correlation.evidence[0] if correlation.evidence else '复杂问题'}",
                confidence=correlation.confidence,
                affected_components=[],
                evidence={"correlation": correlation.evidence},
                recommendations=["基于关联分析，建议进一步调查相关组件"],
            )

        return None

    def _create_data_insufficient_correlation(
        self,
        metric_anomalies: Dict[str, Any],
        event_patterns: Dict[str, Any],
        log_patterns: Dict[str, Any],
        start_time: datetime,
        end_time: datetime,
    ) -> Optional[CorrelationResult]:
        """创建数据不足的关联信息"""
        # 检查数据可用性
        has_metrics = bool(metric_anomalies.get("high_anomaly_metrics"))
        has_events = bool(event_patterns.get("critical_events"))
        has_logs = bool(log_patterns.get("error_types"))

        # 如果完全没有数据，返回数据不足的关联
        if not any([has_metrics, has_events, has_logs]):
            timeline = [
                {
                    "timestamp": end_time.isoformat(),
                    "type": "data_insufficient",
                    "description": "分析时间窗口内未发现足够的异常数据",
                    "severity": "low",
                }
            ]

            return CorrelationResult(
                confidence=0.1,  # 低置信度，因为数据不足
                correlation_type="data_insufficient",
                evidence=["数据不足 - 未发现足够的异常数据进行分析"],
                timeline=timeline,
            )

        # 如果只有单一数据源有数据，返回单一数据源关联
        data_sources = []
        if has_metrics:
            data_sources.append("指标数据")
        if has_events:
            data_sources.append("事件数据")
        if has_logs:
            data_sources.append("日志数据")

        if len(data_sources) == 1:
            timeline = [
                {
                    "timestamp": end_time.isoformat(),
                    "type": "single_data_source",
                    "description": f"仅发现{data_sources[0]}，建议收集更多数据源进行分析",
                    "severity": "medium",
                }
            ]

            return CorrelationResult(
                confidence=0.3,  # 中等置信度，因为只有单一数据源
                correlation_type="single_data_source",
                evidence=[f"单一数据源 - 仅发现{data_sources[0]}"],
                timeline=timeline,
            )

        return None

    def _calculate_data_completeness(
        self, metrics: List[MetricData], events: List[EventData], logs: List[LogData]
    ) -> Dict[str, Any]:
        """计算数据完整性分数"""
        completeness = {
            "metrics": {
                "available": len(metrics) > 0,
                "count": len(metrics),
                "quality_score": 0.0,
            },
            "events": {
                "available": len(events) > 0,
                "count": len(events),
                "critical_events": len(
                    [e for e in events if e.severity.value in ["critical", "high"]]
                ),
            },
            "logs": {
                "available": len(logs) > 0,
                "count": len(logs),
                "error_logs": len([log for log in logs if log.level in ["ERROR", "FATAL"]]),
            },
        }

        # 计算指标质量分数
        if metrics:
            quality_scores = []
            for metric in metrics:
                if metric.values:
                    # 有数据的指标得分高
                    quality_scores.append(min(len(metric.values) / 10, 1.0))
                else:
                    quality_scores.append(0.0)
            completeness["metrics"]["quality_score"] = (
                sum(quality_scores) / len(quality_scores) if quality_scores else 0.0
            )

        # 计算总体完整性分数
        data_sources_available = sum(
            [
                completeness["metrics"]["available"],
                completeness["events"]["available"],
                completeness["logs"]["available"],
            ]
        )

        completeness["overall_score"] = data_sources_available / 3.0
        completeness["data_sources_available"] = data_sources_available

        return completeness

    async def _generate_analysis_report(
        self,
        metrics: List[MetricData],
        events: List[EventData],
        logs: List[LogData],
        root_causes: List[RootCause],
        data_completeness: Dict[str, Any],
    ) -> Dict[str, Any]:
        """生成详细的分析报告"""
        # 基础报告结构
        base_report = {
            "summary": {
                "total_root_causes": len(root_causes),
                "data_sources_used": data_completeness["data_sources_available"],
                "analysis_confidence": (
                    "high"
                    if data_completeness["overall_score"] > 0.7
                    else "medium"
                    if data_completeness["overall_score"] > 0.3
                    else "low"
                ),
            },
            "data_quality": {
                "metrics_quality": (
                    "good"
                    if data_completeness["metrics"]["quality_score"] > 0.5
                    else "poor"
                ),
                "events_available": data_completeness["events"]["available"],
                "logs_available": data_completeness["logs"]["available"],
            },
        }

        # 如果外部服务不可用，返回基础报告
        if not self.llm_service:
            base_report["llm_summary"] = "外部分析不可用，使用基础分析报告"
            return base_report

        try:
            # 准备分析数据摘要
            analysis_summary = {
                "metrics_count": len(metrics),
                "events_count": len(events),
                "logs_count": len(logs),
                "root_causes": [
                    {
                        "type": rc.cause_type,
                        "description": rc.description,
                        "confidence": rc.confidence,
                        "affected_components": rc.affected_components,
                    }
                    for rc in root_causes
                ],
                "data_completeness": data_completeness,
            }

            # 构造请求
            system_prompt = """你是一个专业的云运维分析师。基于根因分析结果，生成一份简洁的分析总结。
要求：
1. 总结不超过200字
2. 重点突出主要发现和关键问题
3. 语言专业但易懂
4. 如果没有根因，说明系统状态和数据质量
5. 提供最核心的洞察，避免冗余信息"""

            user_prompt = f"""分析结果摘要：
{json.dumps(analysis_summary, ensure_ascii=False, indent=2)}

请生成一份专业的根因分析总结。"""

            # 生成报告
            llm_summary = await self.llm_service.generate_response(
                messages=[{"role": "user", "content": user_prompt}],
                system_prompt=system_prompt,
                temperature=0.3,
                max_tokens=300,
                use_task_model=False,  # 复杂操作：生成分析总结，使用主模型
            )

            if llm_summary:
                base_report["llm_summary"] = llm_summary.strip()
                self.logger.info("生成分析报告成功")
            else:
                base_report["llm_summary"] = "分析暂时不可用，请查看基础分析数据"

        except Exception as e:
            self.logger.error("生成分析报告失败: %s", type(e).__name__)
            base_report["llm_summary"] = "分析生成失败"

        return base_report
