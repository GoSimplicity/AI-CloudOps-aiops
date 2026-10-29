#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps事件数据收集器
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from app.config.settings import CONFIG, config
from app.core.interfaces.k8s_client import K8sClient, NullK8sClient
from app.models.rca_models import EventData, SeverityLevel

from .base_collector import BaseDataCollector, CollectionBatch
from .recovering_client import RecoveringClient


class EventsCollector(BaseDataCollector):
    """优化的Kubernetes事件数据收集器"""

    # 严重程度映射 - 简化配置
    SEVERITY_MAPPING = {
        SeverityLevel.CRITICAL: {
            "OOMKilled",
            "Killing",
            "Failed",
            "FailedScheduling",
            "FailedMount",
            "FailedCreatePodSandBox",
            "NetworkNotReady",
            "FailedCreate",
        },
        SeverityLevel.HIGH: {
            "Unhealthy",
            "BackOff",
            "ImagePullBackOff",
            "ErrImagePull",
            "CrashLoopBackOff",
            "FailedSync",
            "NodeNotReady",
        },
        SeverityLevel.MEDIUM: {
            "Pulled",
            "Created",
            "Started",
            "Scheduled",
            "SuccessfulCreate",
            "ScalingReplicaSet",
        },
    }

    def __init__(
        self,
        config_dict: Optional[Dict[str, Any]] = None,
        k8s_client: Optional[K8sClient] = None,
    ):
        super().__init__("events", config_dict)
        self.k8s: K8sClient = k8s_client or NullK8sClient()

        # 从配置文件读取事件收集器配置
        self.rca_config = config.rca
        self.events_config = CONFIG.get("rca", {}).get("events", {})

        # 事件配置
        self.default_event_types = self.events_config.get(
            "default_event_types", ["Warning", "Normal"]
        )
        self.batch_size = self.events_config.get("batch_size", 100)
        self.max_events_limit = self.events_config.get("max_events_limit", 1000)
        self.concurrent_limit = self.events_config.get("concurrent_limit", 5)

        self._severity_cache = self._build_severity_cache()

    def _build_severity_cache(self) -> Dict[str, SeverityLevel]:
        """构建严重程度缓存以提高查找性能"""
        cache = {}
        for severity, reasons in self.SEVERITY_MAPPING.items():
            for reason in reasons:
                cache[reason] = severity
        return cache

    async def _do_initialize(self) -> None:
        """初始化Kubernetes客户端（通过依赖注入）"""
        try:
            if isinstance(self.k8s, RecoveringClient):
                return
            if isinstance(self.k8s, NullK8sClient):
                self.logger.warning("未注入K8s客户端，将以降级模式运行")
                return

            for attempt in range(3):
                try:
                    if await self.k8s.health_check():
                        self.logger.info("Kubernetes客户端健康检查通过")
                        return
                    else:
                        self.logger.warning(
                            f"Kubernetes健康检查失败，尝试 {attempt + 1}/3"
                        )
                        if attempt < 2:
                            await asyncio.sleep(2**attempt)
                except Exception as e:
                    self.logger.warning(
                        f"Kubernetes客户端连接尝试 {attempt + 1}/3 失败: {type(e).__name__}"
                    )
                    if attempt < 2:
                        await asyncio.sleep(2**attempt)

            raise RuntimeError("无法连接到Kubernetes客户端，已尝试3次")

        except Exception as e:
            self.logger.error("初始化Kubernetes客户端时发生错误: %s", type(e).__name__)
            raise

    async def collect(
        self, namespace: str, start_time: datetime, end_time: datetime, **kwargs
    ) -> List[EventData]:
        """
        收集K8s事件数据（优化版本）

        Args:
            namespace: Kubernetes命名空间
            start_time: 开始时间
            end_time: 结束时间
            **kwargs: 其他参数

        Returns:
            List[EventData]: 事件数据列表
        """
        await self.initialize()
        self._ensure_initialized()

        # 确保时间有时区信息
        start_time = self._ensure_timezone(start_time)
        end_time = self._ensure_timezone(end_time)

        event_types = set(kwargs.get("event_types", self.default_event_types))
        object_names = set(kwargs.get("object_names", []))

        try:
            # 获取事件
            raw_events = await self.k8s.get_events(
                namespace=namespace, limit=self.max_events_limit, raise_errors=True
            )
            self.logger.info(
                f"获取到原始事件数量: {len(raw_events)}, 时间范围: {start_time} 到 {end_time}"
            )

            # 批量处理事件
            processed_events = await self._batch_process_events(
                raw_events, start_time, end_time, event_types, object_names
            )

            # 按严重程度和时间排序
            processed_events.sort(
                key=lambda x: (self._severity_order(x.severity), x.timestamp),
                reverse=True,
            )

            self.logger.info(
                f"成功收集 {len(processed_events)}/{len(raw_events)} 个事件"
            )
            if len(raw_events) >= self.max_events_limit:
                processed_events.error_category = "collection_incomplete" if processed_events.failed else "truncated"
                processed_events.failed += 1
            return processed_events

        except Exception as e:
            self.logger.error("收集K8s事件失败: %s", type(e).__name__)
            raise

    async def _batch_process_events(
        self,
        raw_events: List[Dict],
        start_time: datetime,
        end_time: datetime,
        event_types: Set[str],
        object_names: Set[str],
    ) -> List[EventData]:
        """批量处理事件以提高性能"""
        processed_events = CollectionBatch(succeeded=0)

        # 使用异步批处理
        for i in range(0, len(raw_events), self.batch_size):
            batch = raw_events[i : i + self.batch_size]
            batch_results = await asyncio.gather(
                *[
                    self._process_single_event(
                        event, start_time, end_time, event_types, object_names
                    )
                    for event in batch
                ],
                return_exceptions=True,
            )

            for result in batch_results:
                if isinstance(result, EventData):
                    processed_events.append(result)
                    processed_events.succeeded += 1
                elif isinstance(result, Exception):
                    processed_events.failed += 1
                    self.logger.warning("处理事件失败: %s", type(result).__name__)
                else:
                    processed_events.succeeded += 1

        if not raw_events:
            processed_events.succeeded = 1
        return processed_events

    async def _process_single_event(
        self,
        event: Dict,
        start_time: datetime,
        end_time: datetime,
        event_types: Set[str],
        object_names: Set[str],
    ) -> Optional[EventData]:
        """处理单个事件"""
        try:
            # 解析时间
            event_time = self._parse_event_time(event)

            # 时间过滤
            if not (start_time <= event_time <= end_time):
                return None

            # 类型过滤
            if event_types and event.get("type") not in event_types:
                return None

            # 对象名称过滤
            if object_names:
                involved_object = event.get("involvedObject", {})
                if involved_object.get("name") not in object_names:
                    return None

            return self._convert_to_event_data(event)
        except Exception as e:
            self.logger.debug("处理单个事件失败: %s", type(e).__name__)
            raise

    def _parse_event_time(self, event: Dict[str, Any]) -> datetime:
        """优化的时间解析"""
        # 按优先级尝试不同的时间字段
        for field in ["last_timestamp", "first_timestamp", "event_time"]:
            timestamp = event.get(field)
            if timestamp:
                try:
                    if isinstance(timestamp, datetime):
                        result = self._ensure_timezone(timestamp)
                        return result
                    elif isinstance(timestamp, str):
                        # 处理ISO格式
                        if timestamp.endswith("Z"):
                            timestamp = timestamp[:-1] + "+00:00"
                        dt = datetime.fromisoformat(timestamp)
                        result = self._ensure_timezone(dt)
                        return result
                except (ValueError, AttributeError):
                    continue

        # 尝试从metadata获取
        creation_timestamp = event.get("metadata", {}).get("creationTimestamp")
        if creation_timestamp:
            try:
                if creation_timestamp.endswith("Z"):
                    creation_timestamp = creation_timestamp[:-1] + "+00:00"
                dt = datetime.fromisoformat(creation_timestamp)
                return self._ensure_timezone(dt)
            except (ValueError, AttributeError):
                pass

        # 返回当前UTC时间
        return datetime.now(timezone.utc)

    def _ensure_timezone(self, dt: datetime) -> datetime:
        """确保datetime对象有时区信息"""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def _convert_to_event_data(self, event: Dict[str, Any]) -> EventData:
        """优化的事件转换"""
        timestamp = self._parse_event_time(event)
        event_type = event.get("type", "Unknown")
        reason = event.get("reason", "Unknown")
        message = str(event.get("message", ""))

        # 安全地处理count字段，确保是有效的整数
        count_value = event.get("count", 1)
        if count_value is None or count_value == "":
            count = 1
        else:
            try:
                count = int(count_value)
                if count <= 0:
                    count = 1
            except (ValueError, TypeError):
                count = 1

        # 注意：Kubernetes API返回的是involvedObject（驼峰命名）
        involved_object = event.get("involvedObject", {})

        # 确保所有字段都有值，避免空字段
        object_info = {
            "kind": involved_object.get("kind", "Unknown"),
            "name": involved_object.get("name", "Unknown"),
            "namespace": involved_object.get("namespace", "default"),
            "uid": involved_object.get("uid", ""),
        }

        severity = self._determine_severity(event_type, reason, message)

        return EventData(
            timestamp=timestamp,
            type=event_type,
            reason=reason,
            message=message,
            involved_object=object_info,
            severity=severity,
            count=count,
        )

    def _determine_severity(
        self, event_type: str, reason: str, message: str
    ) -> SeverityLevel:
        """优化的严重程度判断"""
        # 使用缓存快速查找
        if reason in self._severity_cache:
            return self._severity_cache[reason]

        # 基于事件类型和消息内容的启发式判断
        if event_type == "Warning":
            message_lower = message.lower()
            if any(
                kw in message_lower for kw in ["failed", "error", "timeout", "killed"]
            ):
                return SeverityLevel.CRITICAL
            elif any(kw in message_lower for kw in ["unhealthy", "backoff", "pending"]):
                return SeverityLevel.HIGH
            return SeverityLevel.MEDIUM

        return SeverityLevel.LOW

    def _severity_order(self, severity: SeverityLevel) -> int:
        """获取严重程度排序权重"""
        return {
            SeverityLevel.CRITICAL: 4,
            SeverityLevel.HIGH: 3,
            SeverityLevel.MEDIUM: 2,
            SeverityLevel.LOW: 1,
        }.get(severity, 0)

    async def health_check(self) -> bool:
        """健康检查"""
        try:
            if not self.k8s:
                return False
            return await self.k8s.health_check()
        except Exception as e:
            self.logger.error("事件收集器健康检查失败: %s", type(e).__name__)
            return False

    async def get_event_patterns(
        self, namespace: str, start_time: datetime, end_time: datetime
    ) -> Dict[str, Any]:
        """分析事件模式"""
        events = await self.collect(namespace, start_time, end_time)

        patterns = {
            "total_events": len(events),
            "severity_distribution": defaultdict(int),
            "reason_frequency": defaultdict(int),
            "affected_resources": defaultdict(set),
            "temporal_patterns": [],
        }

        for event in events:
            patterns["severity_distribution"][event.severity.value] += 1
            patterns["reason_frequency"][event.reason] += event.count

            obj_kind = event.involved_object.get("kind", "Unknown")
            obj_name = event.involved_object.get("name", "Unknown")
            patterns["affected_resources"][obj_kind].add(obj_name)

        # 转换set为list以便序列化
        patterns["affected_resources"] = {
            k: list(v) for k, v in patterns["affected_resources"].items()
        }

        return patterns
