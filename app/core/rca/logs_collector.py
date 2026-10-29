#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 日志数据收集器
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import re
from typing import Any, Dict, List, Optional

from app.config.settings import CONFIG, config
from app.core.interfaces.k8s_client import K8sClient, NullK8sClient
from app.models.rca_models import LogData

from .base_collector import BaseDataCollector, CollectionBatch


class LogsCollector(BaseDataCollector):
    """优化的Pod日志数据收集器"""

    # 预编译的错误模式
    ERROR_PATTERNS = {
        "java_exception": r"(?:Exception|Error)(?:\s+in\s+thread\s+\"[^\"]*\")?\s*:\s*(.+)",
        "python_exception": r"(\w+Error|\w+Exception):\s*(.+)",
        "go_panic": r"panic:\s*(.+)",
        "nodejs_error": r"(?:Error|TypeError|ReferenceError):\s*(.+)",
        "http_error": r"HTTP\s+(\d{3,})\s*[:\-]?\s*(.+)",
        "database_error": r"(?:SQL|Database|Connection)\s*(?:Error|Exception):\s*(.+)",
        "timeout_error": r"(?:timeout|timed?\s+out)(?:\s*:?\s*(.+))?",
        "connection_error": r"(?:connection|connect)(?:\s+(?:failed|refused|reset|timeout))(?:\s*:?\s*(.+))?",
        "memory_error": r"(?:out of memory|oom|memory exhausted)",
        "disk_error": r"(?:no space left|disk full|filesystem full)",
    }

    # 堆栈跟踪模式
    STACK_TRACE_PATTERNS = [
        r"^\s*at\s+",  # Java/Scala
        r"^\s*File\s+\"",  # Python
        r"^\s*Traceback",  # Python
        r"^\s*goroutine\s+",  # Go
        r"^\s*\w+\.\w+\(",  # General function call
        r"^\s+\^",  # Node.js
    ]

    # 时间戳格式 - 支持更多微秒精度格式
    TIMESTAMP_FORMATS = [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%b %d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ]

    def __init__(
        self,
        config_dict: Optional[Dict[str, Any]] = None,
        k8s_client: Optional[K8sClient] = None,
    ):
        super().__init__("logs", config_dict)
        self.k8s: K8sClient = k8s_client or NullK8sClient()

        # 从配置文件读取日志收集器配置
        self.rca_config = config.rca
        self.logs_config = CONFIG.get("rca", {}).get("logs", {})

        # 日志配置
        self.max_lines = self.logs_config.get("max_lines", 500)
        self.error_lines = self.logs_config.get("error_lines", 200)
        self.concurrent_limit = self.logs_config.get("concurrent_limit", 5)
        self.cache_size = self.logs_config.get("cache_size", 1000)
        self.dedup_cache_size = self.logs_config.get("dedup_cache_size", 10000)
        self.max_message_length = self.logs_config.get("max_message_length", 1000)
        self.max_stack_trace_lines = self.logs_config.get("max_stack_trace_lines", 20)
        self.default_error_only = self.logs_config.get("default_error_only", True)

        # 编译正则表达式
        self._compile_patterns()

        # 缓存
        self._error_cache = {}  # 缓存错误模式匹配结果
        self._log_dedup = set()  # 用于去重
        self._dedup_cleanup_counter = 0  # 计数器，用于定期清理

    def _compile_patterns(self) -> None:
        """编译所有正则表达式模式"""
        self.compiled_error_patterns = {
            name: re.compile(pattern, re.IGNORECASE)
            for name, pattern in self.ERROR_PATTERNS.items()
        }

        self.compiled_stack_patterns = [
            re.compile(pattern) for pattern in self.STACK_TRACE_PATTERNS
        ]

        self.log_level_pattern = re.compile(
            r"\b(FATAL|ERROR|WARN|WARNING|INFO|DEBUG|TRACE)\b", re.IGNORECASE
        )

        # 优化的时间戳模式 - 按常见程度排序，修复微秒精度问题
        self.timestamp_pattern = re.compile(
            r"(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d{3,9})?(?:Z|[+-]\d{2}:?\d{2})?)"
            r"|(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})"
            r"|(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
        )

    async def _do_initialize(self) -> None:
        """初始化Kubernetes客户端（通过依赖注入）"""
        try:
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
    ) -> List[LogData]:
        """
        收集Pod日志数据（优化版本）

        Args:
            namespace: Kubernetes命名空间
            start_time: 开始时间
            end_time: 结束时间
            **kwargs: 其他参数

        Returns:
            List[LogData]: 日志数据列表
        """
        await self.initialize()
        self._ensure_initialized()

        # 确保时间有时区
        start_time = self._ensure_timezone(start_time)
        end_time = self._ensure_timezone(end_time)

        pod_names = kwargs.get("pod_names", [])
        max_lines = kwargs.get("max_lines", self.max_lines)
        error_only = kwargs.get("error_only", self.default_error_only)

        # 如果没有指定Pod，获取所有Pod
        if not pod_names:
            pods = await self.k8s.get_pods(namespace, raise_errors=True)
            pod_names = [
                pod.get("metadata", {}).get("name")
                for pod in pods
                if pod.get("metadata", {}).get("name")
            ]

        # 并发收集日志
        collected_logs = await self._collect_logs_concurrent(
            namespace, pod_names, start_time, end_time, max_lines, error_only
        )

        # 按时间和严重程度排序
        collected_logs.sort(
            key=lambda x: (self._get_severity_order(x.level), x.timestamp), reverse=True
        )

        self.logger.info(f"成功收集 {len(collected_logs)} 条日志")
        return collected_logs

    async def _collect_logs_concurrent(
        self,
        namespace: str,
        pod_names: List[str],
        start_time: datetime,
        end_time: datetime,
        max_lines: int,
        error_only: bool,
    ) -> List[LogData]:
        """并发收集多个Pod的日志"""
        tasks = []

        # 限制并发数
        semaphore = asyncio.Semaphore(self.concurrent_limit)

        async def collect_with_limit(pod_name):
            async with semaphore:
                return await self._collect_pod_logs_optimized(
                    namespace, pod_name, start_time, end_time, max_lines, error_only
                )

        for pod_name in pod_names:
            tasks.append(collect_with_limit(pod_name))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 合并结果
        all_logs = CollectionBatch(succeeded=0)
        for result in results:
            if isinstance(result, CollectionBatch):
                all_logs.extend(result)
                all_logs.succeeded += result.succeeded
                all_logs.failed += result.failed
                if result.failed:
                    if all_logs.error_category != result.error_category and all_logs.failed > result.failed:
                        all_logs.error_category = "collection_incomplete"
                    else:
                        all_logs.error_category = result.error_category
            elif isinstance(result, list):
                all_logs.extend(result)
                all_logs.succeeded += 1
            elif isinstance(result, Exception):
                if all_logs.failed and all_logs.error_category == "truncated":
                    all_logs.error_category = "collection_incomplete"
                all_logs.failed += 1
                self.logger.warning("收集日志失败: %s", type(result).__name__)

        if not pod_names:
            all_logs.succeeded = 1
        return all_logs

    async def _collect_pod_logs_optimized(
        self,
        namespace: str,
        pod_name: str,
        start_time: datetime,
        end_time: datetime,
        max_lines: int,
        error_only: bool,
    ) -> List[LogData]:
        """优化的单Pod日志收集"""
        try:
            # 获取Pod信息
            pod_info = await self.k8s.get_pod(namespace, pod_name, raise_errors=True)
            if not pod_info:
                raise RuntimeError("Pod information unavailable")

            containers = pod_info.get("spec", {}).get("containers", [])
            all_logs = CollectionBatch(succeeded=0)

            # 并发收集各容器日志
            container_tasks = []
            container_names = []
            for container in containers:
                container_name = container.get("name")
                if container_name:
                    container_names.append(container_name)
                    container_tasks.append(
                        self._get_container_logs(
                            namespace, pod_name, container_name, start_time, max_lines
                        )
                    )

            container_logs_list = await asyncio.gather(
                *container_tasks, return_exceptions=True
            )

            # 处理每个容器的日志
            for i, container_logs in enumerate(container_logs_list):
                if isinstance(container_logs, Exception):
                    if all_logs.failed and all_logs.error_category == "truncated":
                        all_logs.error_category = "collection_incomplete"
                    all_logs.failed += 1
                    continue
                all_logs.succeeded += 1
                if isinstance(container_logs, str) and container_logs:
                    if max_lines > 0 and len(container_logs.splitlines()) >= max_lines:
                        all_logs.error_category = "collection_incomplete" if all_logs.failed else "truncated"
                        all_logs.failed += 1
                    container_name = container_names[i]
                    parsed = self._parse_logs_optimized(
                        container_logs,
                        pod_name,
                        container_name,
                        start_time,
                        end_time,
                        error_only,
                    )
                    all_logs.extend(parsed)

            if not container_tasks:
                all_logs.succeeded = 1
            return all_logs

        except Exception as e:
            self.logger.warning("处理Pod失败: %s", type(e).__name__)
            raise

    async def _get_container_logs(
        self,
        namespace: str,
        pod_name: str,
        container_name: str,
        since_time: datetime,
        tail_lines: int,
    ) -> str:
        """获取容器日志"""
        try:
            logs = await self.k8s.get_pod_logs(
                namespace=namespace,
                pod_name=pod_name,
                container_name=container_name,
                since_time=since_time,
                tail_lines=tail_lines,
                raise_errors=True,
            )
            if logs is None:
                raise RuntimeError("Pod logs unavailable")
            return logs
        except Exception as e:
            # 只记录debug级别日志，避免正常错误（如容器未启动）产生过多日志
            self.logger.debug("获取容器日志失败: %s", type(e).__name__)
            raise

    def _parse_logs_optimized(
        self,
        log_content: str,
        pod_name: str,
        container_name: str,
        start_time: datetime,
        end_time: datetime,
        error_only: bool,
    ) -> List[LogData]:
        """优化的日志解析"""
        parsed_logs = []
        lines = log_content.split("\n")

        # 批处理优化
        current_entry = None
        stack_trace_lines = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 检查是否是堆栈跟踪的延续
            if self._is_stack_trace(line) and current_entry:
                stack_trace_lines.append(line)
                # 限制堆栈跟踪的行数
                if len(stack_trace_lines) <= self.max_stack_trace_lines:
                    continue
                else:
                    # 超过最大行数，停止收集
                    current_entry.stack_trace = "\n".join(
                        stack_trace_lines[: self.max_stack_trace_lines]
                    )
                    stack_trace_lines = []
                    continue

            # 如果有未处理的堆栈，添加到上一个日志条目
            if stack_trace_lines and current_entry:
                # 将堆栈跟踪添加到当前条目
                if not current_entry.stack_trace:
                    current_entry.stack_trace = "\n".join(
                        stack_trace_lines[: self.max_stack_trace_lines]
                    )
                stack_trace_lines = []

            # 解析新的日志条目
            timestamp = self._extract_timestamp_fast(line)
            if not timestamp:
                timestamp = datetime.now(timezone.utc)

            # 时间范围检查
            if not (start_time <= timestamp <= end_time):
                continue

            # 提取日志级别
            level = self._extract_log_level_fast(line)

            # 错误过滤
            if error_only:
                if level not in ["ERROR", "FATAL", "WARN", "WARNING"]:
                    if not self._contains_error_pattern_fast(line):
                        continue

            # 创建日志条目
            log_hash = self._get_log_hash(pod_name, container_name, line)
            if log_hash not in self._log_dedup:
                self._log_dedup.add(log_hash)

                # 定期清理去重缓存，防止内存泄漏
                self._dedup_cleanup_counter += 1
                if self._dedup_cleanup_counter >= 1000:
                    if len(self._log_dedup) > self.dedup_cache_size:
                        # 保留最近50%的缓存
                        keep_size = self.dedup_cache_size // 2
                        self._log_dedup = set(list(self._log_dedup)[-keep_size:])
                    self._dedup_cleanup_counter = 0

                current_entry = LogData(
                    timestamp=timestamp,
                    pod_name=pod_name,
                    container_name=container_name,
                    level=level,
                    message=line[: self.max_message_length],  # 限制消息长度
                    error_type=self._detect_error_type_fast(line),
                    stack_trace=None,  # 初始化为None，后续会填充
                )

                parsed_logs.append(current_entry)

        # 处理最后的堆栈跟踪
        if stack_trace_lines and current_entry:
            if not current_entry.stack_trace:
                current_entry.stack_trace = "\n".join(
                    stack_trace_lines[: self.max_stack_trace_lines]
                )  # 限制堆栈行数

        return parsed_logs

    def _extract_timestamp_fast(self, line: str) -> Optional[datetime]:
        """快速提取时间戳"""
        match = self.timestamp_pattern.search(line[:100])  # 只搜索前100字符
        if not match:
            return None

        timestamp_str = next((g for g in match.groups() if g), None)
        if not timestamp_str:
            return None

        # 处理超高精度微秒（如纳秒）- 截断到微秒精度
        if "." in timestamp_str and timestamp_str.endswith("Z"):
            # 分离时间部分和微秒部分
            time_part, microsec_part = timestamp_str[:-1].split(".")
            if len(microsec_part) > 6:
                # 截断到6位微秒
                microsec_part = microsec_part[:6]
            timestamp_str = f"{time_part}.{microsec_part}Z"

        # 尝试解析
        for fmt in self.TIMESTAMP_FORMATS:
            try:
                return datetime.strptime(timestamp_str, fmt).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue

        # 如果所有格式都失败，尝试简化的ISO格式解析
        try:
            # 移除时区信息重新尝试
            simplified = timestamp_str.replace("Z", "").replace("+00:00", "")
            return datetime.fromisoformat(simplified).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

        return None

    def _extract_log_level_fast(self, line: str) -> str:
        """快速提取日志级别"""
        # 先检查前100个字符（大多数日志级别在开头）
        match = self.log_level_pattern.search(line[:100])
        if match:
            return match.group(1).upper()

        # 基于关键词的快速判断（支持中英文）
        line_lower = line[:200].lower()  # 只检查前200字符

        # 错误级别关键词（中英文）
        error_keywords = [
            "error",
            "exception",
            "failed",
            "fatal",
            "crash",
            "panic",
            "died",
            "错误",
            "异常",
            "失败",
            "崩溃",
            "死机",
            "宕机",
            "故障",
            "中断",
        ]

        # 警告级别关键词（中英文）
        warn_keywords = [
            "warn",
            "warning",
            "alert",
            "caution",
            "警告",
            "注意",
            "提醒",
            "告警",
        ]

        if any(kw in line_lower for kw in error_keywords):
            return "ERROR"
        elif any(kw in line_lower for kw in warn_keywords):
            return "WARN"

        return "INFO"

    def _contains_error_pattern_fast(self, line: str) -> bool:
        """快速检查错误模式"""
        # 使用缓存
        line_hash = hash(line)
        if line_hash in self._error_cache:
            return self._error_cache[line_hash]

        # 检查正则模式
        regex_result = any(
            pattern.search(line)
            for pattern in list(self.compiled_error_patterns.values())[
                :5
            ]  # 只检查最常见的模式
        )

        # 检查中英文错误关键词
        line_lower = line.lower()
        keyword_result = any(
            kw in line_lower
            for kw in [
                "error",
                "exception",
                "failed",
                "fatal",
                "crash",
                "panic",
                "died",
                "timeout",
                "错误",
                "异常",
                "失败",
                "崩溃",
                "死机",
                "宕机",
                "故障",
                "中断",
                "超时",
            ]
        )

        result = regex_result or keyword_result

        # 缓存结果（限制缓存大小）
        if len(self._error_cache) < self.cache_size:
            self._error_cache[line_hash] = result

        return result

    def _detect_error_type_fast(self, line: str) -> Optional[str]:
        """快速检测错误类型"""
        # 优先检查最常见的错误类型
        priority_patterns = [
            "java_exception",
            "python_exception",
            "http_error",
            "timeout_error",
        ]

        for error_type in priority_patterns:
            if self.compiled_error_patterns[error_type].search(line):
                return error_type.replace("_", " ").title()

        # 检查其他模式
        for error_type, pattern in self.compiled_error_patterns.items():
            if error_type not in priority_patterns and pattern.search(line):
                return error_type.replace("_", " ").title()

        return None

    def _is_stack_trace(self, line: str) -> bool:
        """快速判断是否为堆栈跟踪"""
        # 快速检查常见的堆栈跟踪特征
        if line.startswith(("    at ", "  File ", "Traceback", "goroutine", "\tat")):
            return True

        # 检查是否以空格或制表符开头（常见的堆栈跟踪缩进）
        if line and (line[0] == " " or line[0] == "\t") and len(line.strip()) > 10:
            # 检查是否包含函数调用或文件路径的特征
            # 更严格的判断，避免误判
            if any(
                indicator in line
                for indicator in [
                    ".java:",
                    ".py:",
                    ".go:",
                    "()",
                    ".js:",
                    "line ",
                    "Line ",
                    "at ",
                    "File ",
                ]
            ):
                return True

        # 使用编译的模式
        return any(pattern.search(line) for pattern in self.compiled_stack_patterns[:3])

    def _ensure_timezone(self, dt: datetime) -> datetime:
        """确保datetime有时区信息"""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def _get_log_hash(self, pod_name: str, container_name: str, message: str) -> str:
        """生成日志哈希用于去重"""
        content = f"{pod_name}:{container_name}:{message[:100]}"
        return hashlib.md5(content.encode()).hexdigest()

    def _get_severity_order(self, level: str) -> int:
        """获取日志级别的排序权重"""
        return {
            "FATAL": 5,
            "ERROR": 4,
            "WARN": 3,
            "WARNING": 3,
            "INFO": 2,
            "DEBUG": 1,
            "TRACE": 0,
        }.get(level, 1)

    async def health_check(self) -> bool:
        """健康检查"""
        try:
            if not self.k8s:
                return False
            return await self.k8s.health_check()
        except Exception as e:
            self.logger.error(f"日志收集器健康检查失败: {e}")
            return False

    async def get_error_summary(
        self, namespace: str, time_window: timedelta = timedelta(hours=1)
    ) -> Dict[str, Any]:
        """获取错误摘要"""
        end_time = datetime.now(timezone.utc)
        start_time = end_time - time_window

        logs = await self.collect(
            namespace, start_time, end_time, error_only=True, max_lines=self.error_lines
        )

        summary = {
            "total_errors": len(logs),
            "error_types": defaultdict(int),
            "affected_pods": set(),
            "top_errors": [],
        }

        error_messages = defaultdict(int)

        for log in logs:
            if log.error_type:
                summary["error_types"][log.error_type] += 1
            summary["affected_pods"].add(log.pod_name)

            # 统计错误消息
            msg_key = log.message[:100] if len(log.message) > 100 else log.message
            error_messages[msg_key] += 1

        # 获取Top错误
        summary["top_errors"] = [
            {"message": msg, "count": count}
            for msg, count in sorted(
                error_messages.items(), key=lambda x: x[1], reverse=True
            )[:5]
        ]

        summary["affected_pods"] = list(summary["affected_pods"])

        return summary
