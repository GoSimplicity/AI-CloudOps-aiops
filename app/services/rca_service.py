#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps智能根因分析服务
"""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import time
from typing import Any, Dict, List, Optional

from app.common.exceptions import AIOpsException, ValidationError

from ..common.exceptions import RCAError
from ..core.rca.events_collector import EventsCollector
from ..core.rca.logs_collector import LogsCollector
from ..core.rca.metrics_collector import MetricsCollector
from ..core.rca.rca_engine import RCAAnalysisEngine
from .base import BaseService, HealthCheckMixin
from .prometheus import PrometheusService

logger = logging.getLogger("aiops.services.rca")


class RCAService(BaseService, HealthCheckMixin):
  """根因分析服务"""

  def __init__(self) -> None:
    super().__init__("rca")
    self._engine: Optional[RCAAnalysisEngine] = None
    self._metrics_collector: Optional[MetricsCollector] = None
    self._events_collector: Optional[EventsCollector] = None
    self._logs_collector: Optional[LogsCollector] = None

    # Redis缓存管理器
    self._cache_manager = None

  async def _do_initialize(self) -> None:
    try:
      # 初始化Redis缓存管理器
      try:
        from ..config.settings import config
        from ..core.cache.redis_cache_manager import RedisCacheManager

        redis_config = {
          "host": config.redis.host,
          "port": config.redis.port,
          "db": config.redis.db + 3,  # 使用单独的db用于RCA缓存
          "password": config.redis.password
          if hasattr(config.redis, "password")
          else "",
        }
        self._cache_manager = RedisCacheManager(
          redis_config=redis_config,
          cache_prefix="rca_cache:",
          default_ttl=1800,  # 30分钟缓存
          max_cache_size=3000,
          enable_compression=True,
        )
        self.logger.info("RCA服务Redis缓存管理器初始化成功")
      except Exception as cache_e:
        self.logger.warning(
          f"Redis缓存管理器初始化失败: {str(cache_e)}，将在无缓存模式下运行"
        )
        self._cache_manager = None

      # 初始化依赖（LLM、Prometheus、K8s）
      from ..core.interfaces.k8s_client import NullK8sClient
      from ..core.interfaces.prometheus_client import NullPrometheusClient
      from ..services.kubernetes import KubernetesService
      from ..services.llm import LLMService
      from ..services.prometheus import PrometheusService

      llm_service = LLMService()

      # Prometheus 客户端，失败时降级为空实现
      prometheus_client = None
      try:
        prometheus_client = PrometheusService()
        await prometheus_client.initialize()
      except Exception as e:
        self.logger.warning(f"Prometheus初始化失败，启用降级模式: {e}")
        prometheus_client = NullPrometheusClient()

      # Kubernetes 客户端，失败或不健康时降级为空实现
      try:
        k8s_client = KubernetesService()
        # 明确健康性检查，不健康则使用空实现
        if not k8s_client.is_healthy():
          self.logger.warning("Kubernetes不健康，启用降级模式")
          k8s_client = NullK8sClient()
      except Exception as e:
        self.logger.warning(f"Kubernetes初始化失败，启用降级模式: {e}")
        k8s_client = NullK8sClient()

      # 初始化收集器（依赖注入）
      self._metrics_collector = MetricsCollector(
        prometheus_client=prometheus_client
      )
      self._events_collector = EventsCollector(k8s_client=k8s_client)
      self._logs_collector = LogsCollector(k8s_client=k8s_client)

      # 初始化引擎并注入收集器与LLM
      self._engine = RCAAnalysisEngine(
        llm_client=llm_service,
        metrics_collector=self._metrics_collector,
        events_collector=self._events_collector,
        logs_collector=self._logs_collector,
      )

      # 并发初始化所有组件（收集器允许降级空实现通过自身健康检查返回False）
      await asyncio.gather(
        self._metrics_collector.initialize(),
        self._events_collector.initialize(),
        self._logs_collector.initialize(),
        self._engine.initialize(),
      )

      self.logger.info("RCA服务组件初始化完成")
    except Exception as e:
      self.logger.error(f"RCA服务初始化失败: {str(e)}")
      raise RCAError(f"初始化失败: {str(e)}")

  async def _do_health_check(self) -> bool:
    try:
      if (
        not self._metrics_collector
        or not self._events_collector
        or not self._logs_collector
      ):
        return False

      # 检查各组件健康状态
      checks = await self._gather_health_checks()
      return any(checks.values())  # 至少一个收集器正常
    except Exception:
      return False

  async def analyze_root_cause(
    self,
    namespace: str,
    time_window_hours: float = 1.0,
    metrics: Optional[List[str]] = None,
  ) -> Dict[str, Any]:
    """执行根因分析"""
    start_time = time.time()

    try:
      self._ensure_initialized()

      # 尝试从缓存获取结果
      cached_result = await self._try_get_cached_result(
        namespace, time_window_hours, metrics
      )
      if cached_result:
        return cached_result

      # 执行分析
      analysis_result = await self._perform_analysis(
        namespace, time_window_hours, metrics
      )

      # 构建响应
      result = self._build_analysis_response(
        analysis_result, time.time() - start_time
      )

      # 保存到缓存（30分钟缓存）
      cache_key = self._generate_rca_cache_key(
        operation="analyze",
        namespace=namespace,
        time_window_hours=time_window_hours,
        metrics=metrics,
      )
      await self._save_to_cache(cache_key, result, ttl=1800)

      self.logger.info(
        f"RCA分析完成: 发现 {len(result['root_causes'])} 个根因, "
        f"耗时 {result['analysis_duration_seconds']:.2f}秒"
      )
      return result

    except Exception as e:
      self.logger.error(f"RCA分析失败: {str(e)}", exc_info=True)
      raise RCAError(f"分析失败: {str(e)}")

  async def _try_get_cached_result(
    self,
    namespace: str,
    time_window_hours: float,
    metrics: Optional[List[str]],
  ) -> Optional[Dict[str, Any]]:
    """尝试从缓存获取分析结果"""
    if not self._cache_manager:
      return None

    try:
      cache_key = self._generate_rca_cache_key(
        operation="analyze",
        namespace=namespace,
        time_window_hours=time_window_hours,
        metrics=metrics,
      )

      cached_result = await self._get_from_cache(cache_key)
      if cached_result:
        self.logger.info(
          f"RCA分析缓存命中，直接返回结果: namespace={namespace}"
        )
        # 添加缓存命中标识
        cached_result["cache_hit"] = True
        cached_result["cache_key"] = cache_key
        return cached_result
      return None
    except Exception as e:
      self.logger.warning(f"尝试获取缓存结果失败: {str(e)}")
      return None

  async def _perform_analysis(
    self,
    namespace: str,
    time_window_hours: float,
    metrics: Optional[List[str]],
  ) -> Any:
    """执行实际的根因分析"""
    time_window = timedelta(hours=time_window_hours)

    self.logger.info(
      f"开始RCA分析: namespace={namespace}, time_window={time_window_hours}小时"
    )

    return await self._engine.analyze(
      namespace=namespace, time_window=time_window, metrics=metrics
    )

  def _build_analysis_response(
    self,
    analysis_result: Any,
    duration: float,
  ) -> Dict[str, Any]:
    """构建分析响应"""
    # 转换根因数据
    root_causes = self._convert_root_causes(analysis_result.root_causes)

    # 转换关联数据
    correlations = self._convert_correlations(analysis_result.correlations)

    return {
      "success": True,
      "timestamp": analysis_result.timestamp,
      "namespace": analysis_result.namespace,
      "root_causes": root_causes,
      "anomalies": analysis_result.anomalies,
      "correlations": correlations,
      "confidence_score": analysis_result.confidence_score,
      "recommendations": analysis_result.recommendations,
      "timeline_events": len(analysis_result.timeline),
      "analysis_duration_seconds": duration,
    }

  def _convert_root_causes(self, root_causes: List[Any]) -> List[Dict[str, Any]]:
    """转换根因数据格式"""
    converted = []
    for cause in root_causes:
      converted.append(
        {
          "cause_type": cause.cause_type,
          "description": cause.description,
          "confidence": cause.confidence,
          "affected_components": cause.affected_components,
          "evidence": cause.evidence,
          "recommendations": cause.recommendations,
        }
      )
    return converted

  def _convert_correlations(self, correlations: List[Any]) -> List[Dict[str, Any]]:
    """转换关联数据格式"""
    converted = []
    for correlation in correlations:
      if hasattr(correlation, "__dict__"):
        # 如果是对象，转换为字典
        converted.append(
          {
            "confidence": correlation.confidence,
            "correlation_type": correlation.correlation_type,
            "evidence": correlation.evidence,
            "timeline": correlation.timeline,
          }
        )
      else:
        # 如果已经是字典，直接使用
        converted.append(correlation)
    return converted

  async def get_metrics(
    self,
    namespace: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    metrics: Optional[str] = None,
  ) -> Dict[str, Any]:
    """获取指标数据"""
    try:
      self._ensure_initialized()

      # 默认时间范围
      if not end_time:
        end_time = datetime.now(timezone.utc)
      if not start_time:
        start_time = end_time - timedelta(hours=1)

      # 准备指标列表
      metric_list = metrics.split(",") if metrics else []

      # 收集数据
      metric_data = await self._metrics_collector.collect(
        namespace=namespace,
        start_time=start_time,
        end_time=end_time,
        metrics=metric_list,
      )

      # 转换为响应格式
      items = []
      if metric_data:
        for data in metric_data:
          items.append(
            {
              "name": data.name,
              "values": data.values or [],
              "labels": data.labels or {},
              "anomaly_score": data.anomaly_score,
              "trend": data.trend,
            }
          )

      return {"items": items, "total": len(items)}

    except Exception as e:
      self.logger.error(f"获取指标失败: {str(e)}", exc_info=True)
      return {"items": [], "total": 0}

  async def get_events(
    self,
    namespace: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    severity: Optional[str] = None,
  ) -> Dict[str, Any]:
    """获取事件数据"""
    try:
      self._ensure_initialized()

      # 默认时间范围
      if not end_time:
        end_time = datetime.now(timezone.utc)
      if not start_time:
        start_time = end_time - timedelta(hours=1)

      # 收集数据
      event_data = await self._events_collector.collect(
        namespace=namespace, start_time=start_time, end_time=end_time
      )

      # 过滤严重程度
      if severity:
        event_data = [
          e for e in event_data if e.severity.value == severity.lower()
        ]

      # 转换为响应格式
      items = []
      for event in event_data[:100]:  # 限制返回数量
        items.append(
          {
            "timestamp": event.timestamp,
            "type": event.type,
            "reason": event.reason,
            "message": event.message,
            "involved_object": event.involved_object,
            "severity": event.severity,
            "count": event.count,
          }
        )

      return {"items": items, "total": len(items)}

    except Exception as e:
      self.logger.error(f"获取事件失败: {str(e)}")
      raise RCAError(f"获取事件失败: {str(e)}")

  async def get_logs(
    self,
    namespace: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    pod_name: Optional[str] = None,
    error_only: bool = True,
    max_lines: int = 100,
  ) -> Dict[str, Any]:
    """获取日志数据"""
    try:
      self._ensure_initialized()

      # 默认时间范围
      if not end_time:
        end_time = datetime.now(timezone.utc)
      if not start_time:
        start_time = end_time - timedelta(hours=1)

      # 准备Pod列表
      pod_names = [pod_name] if pod_name else []

      # 收集数据
      log_data = await self._logs_collector.collect(
        namespace=namespace,
        start_time=start_time,
        end_time=end_time,
        pod_names=pod_names,
        error_only=error_only,
        max_lines=max_lines,
      )

      # 转换为响应格式
      items = []
      for log in log_data[:max_lines]:  # 限制返回数量
        items.append(
          {
            "timestamp": log.timestamp,
            "pod_name": log.pod_name,
            "container_name": log.container_name,
            "level": log.level,
            "message": log.message,
            "error_type": log.error_type,
            "stack_trace": log.stack_trace,
          }
        )

      return {"items": items, "total": len(items)}

    except Exception as e:
      self.logger.error(f"获取日志失败: {str(e)}")
      raise RCAError(f"获取日志失败: {str(e)}")

  async def get_health_status(self) -> Dict[str, Any]:
    """获取健康状态"""
    try:
      self._ensure_initialized()

      # 检查各组件健康状态
      collectors_health = await self._gather_health_checks()

      # 映射到API期望的字段名称
      prometheus_connected = collectors_health.get("metrics", False)
      kubernetes_connected = collectors_health.get(
        "events", False
      ) or collectors_health.get("logs", False)

      # 检查Redis连接
      redis_connected = False
      if self._cache_manager:
        try:
          # 使用缓存管理器的health_check方法
          cache_health = self._cache_manager.health_check()
          redis_connected = (
            cache_health.get("status") == "healthy"
            if isinstance(cache_health, dict)
            else cache_health
          )
        except Exception as e:
          self.logger.warning(f"Redis健康检查失败: {str(e)}")
          redis_connected = False

      # 判断总体状态
      all_healthy = (
        prometheus_connected and kubernetes_connected and redis_connected
      )
      any_collector = prometheus_connected or kubernetes_connected
      status = "healthy" if all_healthy else ("degraded" if any_collector else "unhealthy")

      return {
        "status": status,
        "prometheus_connected": prometheus_connected,
        "kubernetes_connected": kubernetes_connected,
        "redis_connected": redis_connected,
        "collectors": collectors_health,  # 保留原始信息用于调试
        "timestamp": datetime.now(timezone.utc),
      }

    except Exception as e:
      self.logger.error(f"健康检查失败: {str(e)}")
      return {
        "status": "unhealthy",
        "prometheus_connected": False,
        "kubernetes_connected": False,
        "redis_connected": False,
        "collectors": {"metrics": False, "events": False, "logs": False},
        "timestamp": datetime.now(timezone.utc),
      }

  async def _get_cached_result_with_fallback(
    self, cache_key: str, fallback_func, ttl: int = 1800, **kwargs
  ) -> Dict[str, Any]:
    """
    带回退策略的缓存获取统一方法
    """
    # 尝试从缓存获取
    cached_result = await self._get_from_cache(cache_key)
    if cached_result:
      self.logger.debug(f"缓存命中: {cache_key}")
      return cached_result

    # 缓存未命中，执行回退函数
    self.logger.debug(f"缓存未命中，执行计算: {cache_key}")
    result = await fallback_func(**kwargs)

    # 保存到缓存
    if result:
      await self._save_to_cache(cache_key, result, ttl)

    return result

  async def quick_diagnosis(self, namespace: str) -> Dict[str, Any]:
    """快速诊断 - 优化版本"""
    try:
      self._ensure_initialized()

      # 生成缓存键
      cache_key = self._generate_rca_cache_key(
        operation="quick_diagnosis",
        namespace=namespace,
        time_window_hours=1.0,
      )

      # 使用统一的缓存处理方法
      return await self._get_cached_result_with_fallback(
        cache_key=cache_key,
        fallback_func=self._perform_quick_diagnosis,
        ttl=900,  # 15分钟缓存
        namespace=namespace,
      )

    except Exception as e:
      self.logger.error(f"快速诊断失败: {str(e)}")
      # 返回基础错误信息而不是抛出异常
      return self._build_error_diagnosis_result(namespace, str(e))

  async def _perform_quick_diagnosis(self, namespace: str) -> Dict[str, Any]:
    """
    执行快速诊断的具体逻辑（从原quick_diagnosis方法提取）
    """
    # 检查依赖服务状态
    health_checks = await self._gather_health_checks()
    services_available = any(health_checks.values())

    if not services_available:
      self.logger.warning("所有依赖服务不可用，返回降级的诊断结果")
      return self._build_degraded_diagnosis_result(namespace)

    # 执行快速分析（最近1小时）
    try:
      analysis_result = await self._engine.analyze(
        namespace=namespace, time_window=timedelta(hours=1)
      )
    except Exception as analysis_error:
      self.logger.warning(
        f"分析引擎执行失败: {str(analysis_error)}, 返回基础诊断结果"
      )
      return self._build_analysis_error_diagnosis_result(
        namespace, str(analysis_error)
      )

    return self._build_diagnosis_result_from_analysis(namespace, analysis_result)

  def _build_error_diagnosis_result(
    self, namespace: str, error_message: str
  ) -> Dict[str, Any]:
    """构建错误诊断结果"""
    return {
      "namespace": namespace,
      "diagnosis_time": datetime.now(timezone.utc).isoformat(),
      "critical_issues": [
        {
          "type": "system_error",
          "severity": "high",
          "description": f"快速诊断系统遇到未预期的错误: {error_message[:100]}",
          "confidence": 0.0,
        }
      ],
      "recommendations": [
        "联系系统管理员",
        "检查服务日志获取详细信息",
        "稍后重试",
      ],
      "confidence_score": 0.0,
    }

  def _build_degraded_diagnosis_result(self, namespace: str) -> Dict[str, Any]:
    """构建服务降级时的诊断结果"""
    return {
      "namespace": namespace,
      "diagnosis_time": datetime.now(timezone.utc).isoformat(),
      "critical_issues": [
        {
          "type": "service_unavailable",
          "severity": "high",
          "description": "监控数据服务不可用，无法获取实时状态",
          "confidence": 1.0,
        }
      ],
      "recommendations": [
        "检查Prometheus服务状态",
        "验证Kubernetes集群连接",
        "检查Redis缓存服务",
      ],
      "confidence_score": 0.0,
    }

  def _build_analysis_error_diagnosis_result(
    self, namespace: str, error_message: str
  ) -> Dict[str, Any]:
    """构建分析错误时的诊断结果"""
    return {
      "namespace": namespace,
      "diagnosis_time": datetime.now(timezone.utc).isoformat(),
      "critical_issues": [
        {
          "type": "analysis_error",
          "severity": "medium",
          "description": f"分析过程遇到问题: {error_message[:100]}",
          "confidence": 0.5,
        }
      ],
      "recommendations": [
        "检查目标命名空间是否存在",
        "验证监控数据是否可用",
        "稍后重试诊断",
      ],
      "confidence_score": 0.3,
    }

  def _build_diagnosis_result_from_analysis(
    self, namespace: str, analysis_result
  ) -> Dict[str, Any]:
    """从分析结果构建诊断结果"""
    # 提取关键信息
    critical_issues = []

    # 获取最严重的根因
    if analysis_result.root_causes:
      top_cause = analysis_result.root_causes[0]
      critical_issues.append(
        {
          "type": "root_cause",
          "severity": "critical",
          "description": top_cause.description,
          "confidence": top_cause.confidence,
        }
      )

    # 获取关键事件
    for event in analysis_result.timeline[:5]:
      if event.get("severity") in ["critical", "high"]:
        critical_issues.append(
          {
            "type": "event",
            "severity": event["severity"],
            "description": event["description"],
            "timestamp": event["timestamp"],
          }
        )

    return {
      "namespace": namespace,
      "diagnosis_time": datetime.now(timezone.utc).isoformat(),
      "critical_issues": critical_issues,
      "recommendations": analysis_result.recommendations[:3]
      if hasattr(analysis_result, "recommendations")
      else [],
      "confidence_score": analysis_result.confidence_score
      if hasattr(analysis_result, "confidence_score")
      else 0.8,
    }

  async def get_event_patterns(
    self, namespace: str, hours: float = 1.0
  ) -> Dict[str, Any]:
    """获取事件模式分析 - 优化版本"""
    try:
      self._ensure_initialized()

      # 生成缓存键
      cache_key = self._generate_rca_cache_key(
        operation="event_patterns",
        namespace=namespace,
        time_window_hours=hours,
      )

      # 使用统一的缓存处理方法
      return await self._get_cached_result_with_fallback(
        cache_key=cache_key,
        fallback_func=self._perform_event_patterns_analysis,
        ttl=1200,  # 20分钟缓存
        namespace=namespace,
        hours=hours,
      )

    except Exception as e:
      self.logger.error(f"获取事件模式失败: {str(e)}")
      raise RCAError(f"获取事件模式失败: {str(e)}")

  async def _perform_event_patterns_analysis(
    self, namespace: str, hours: float
  ) -> Dict[str, Any]:
    """执行事件模式分析的具体逻辑"""
    # 时间范围
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours)

    # 获取事件模式
    return await self._events_collector.get_event_patterns(
      namespace=namespace, start_time=start_time, end_time=end_time
    )

  async def get_error_summary(
    self, namespace: str, hours: float = 1.0
  ) -> Dict[str, Any]:
    """获取错误摘要"""
    try:
      self._ensure_initialized()

      # 生成缓存键
      cache_key = self._generate_rca_cache_key(
        operation="error_summary",
        namespace=namespace,
        time_window_hours=hours,
      )

      # 使用统一的缓存处理方法
      return await self._get_cached_result_with_fallback(
        cache_key=cache_key,
        fallback_func=self._perform_error_summary_analysis,
        ttl=1200,  # 20分钟缓存
        namespace=namespace,
        hours=hours,
      )

    except Exception as e:
      self.logger.error(f"获取错误摘要失败: {str(e)}")
      raise RCAError(f"获取错误摘要失败: {str(e)}")

  async def _perform_error_summary_analysis(
    self, namespace: str, hours: float
  ) -> Dict[str, Any]:
    """执行错误摘要分析的具体逻辑"""
    # 获取错误摘要
    return await self._logs_collector.get_error_summary(
      namespace=namespace, time_window=timedelta(hours=hours)
    )

  async def cache_analysis_result(self, result: Any) -> None:
    """缓存分析结果（后台任务）"""
    try:
      # 记录分析结果已缓存（背景任务无需实际处理结果）
      self.logger.info(f"分析结果已记录: {type(result).__name__}")
    except Exception as e:
      self.logger.error(f"缓存分析结果失败: {str(e)}")

  async def _gather_health_checks(self) -> Dict[str, bool]:
    """收集所有组件的健康状态"""
    return {
      "metrics": await self._metrics_collector.health_check(),
      "events": await self._events_collector.health_check(),
      "logs": await self._logs_collector.health_check(),
    }

  async def get_service_health_info(self) -> Dict[str, Any]:
    """获取详细的服务健康信息"""
    try:
      health_status = await self.get_health_status()

      return {
        "service": "rca",
        "version": "1.0.0",
        "status": health_status["status"],
        "collectors": health_status["collectors"],
        "timestamp": health_status["timestamp"],
        "capabilities": [
          "根因分析",
          "指标收集",
          "事件分析",
          "日志分析",
          "快速诊断",
          "模式识别",
        ],
      }
    except Exception as e:
      self.logger.error(f"获取服务健康信息失败: {str(e)}")
      return {
        "service": "rca",
        "status": "unhealthy",
        "error": str(e),
        "timestamp": datetime.now(timezone.utc),
      }

  async def get_all_available_metrics(self) -> List[str]:
    """获取所有可用的Prometheus指标列表"""
    try:
      if self._metrics_collector and self._metrics_collector.prometheus:
        return await self._metrics_collector.prometheus.get_available_metrics()
      else:
        # 如果没有初始化，创建临时prometheus服务
        prometheus_service = PrometheusService()
        return await prometheus_service.get_available_metrics()
    except Exception as e:
      self.logger.error(f"获取可用指标失败: {str(e)}")
      # 返回默认的常见指标列表作为回退
      return [
        "up",
        "node_cpu_seconds_total",
        "node_memory_MemAvailable_bytes",
        "node_load1",
        "kubernetes_pod_cpu_usage_seconds_total",
        "kubernetes_pod_memory_usage_bytes",
        "container_cpu_usage_seconds_total",
        "container_memory_usage_bytes",
      ]

  def _generate_rca_cache_key(
    self,
    operation: str,
    namespace: str,
    time_window_hours: Optional[float] = None,
    metrics: Optional[List[str]] = None,
    **kwargs,
  ) -> str:
    """生成RCA缓存键 - 优化版本"""
    try:
      # 使用更简洁的缓存键生成策略
      key_parts = ["rca", operation, namespace]

      # 添加时间窗口
      if time_window_hours is not None:
        key_parts.append(f"tw{int(time_window_hours * 10)}")

      # 添加指标哈希（如果有）
      if metrics:
        metrics_str = "|".join(sorted(metrics))
        metrics_hash = hashlib.md5(metrics_str.encode()).hexdigest()[:8]
        key_parts.append(f"m{metrics_hash}")

      # 添加其他参数的哈希
      if kwargs:
        important_params = {
          k: v
          for k, v in kwargs.items()
          if k in ["pod_name", "severity", "error_only", "max_lines"]
        }
        if important_params:
          params_str = "|".join(
            [f"{k}:{v}" for k, v in sorted(important_params.items())]
          )
          params_hash = hashlib.md5(params_str.encode()).hexdigest()[:6]
          key_parts.append(f"p{params_hash}")

      return ":".join(key_parts)

    except Exception as e:
      self.logger.warning(f"生成优化缓存键失败，使用简单键: {str(e)}")
      # 降级到更简单的键
      return f"rca:{operation}:{namespace}:{int(time_window_hours or 1)}"

  async def _get_from_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
    """从缓存获取RCA结果 - 增强错误处理"""
    if not self._cache_manager:
      return None

    try:
      cached_result = self._cache_manager.get(cache_key)
      if cached_result:
        self.logger.debug(f"RCA缓存命中: {cache_key}")

        # 验证缓存数据完整性
        if not isinstance(cached_result, dict):
          self.logger.warning(f"缓存数据格式无效: {cache_key}")
          return None

        # 添加缓存标识
        cached_result["from_cache"] = True
        cached_result["cache_timestamp"] = datetime.now()
        return cached_result
    except ConnectionError as e:
      self.logger.warning(f"Redis连接错误: {str(e)}")
      # 连接错误时返回None，允许继续执行
      return None
    except Exception as e:
      self.logger.warning(f"从缓存获取RCA数据失败: {str(e)}")
      # 其他错误也返回None，不影响主流程
      return None

    return None

  async def _save_to_cache(
    self, cache_key: str, result: Dict[str, Any], ttl: int = 1800
  ) -> None:
    """保存RCA结果到缓存 - 增强错误处理"""
    if not self._cache_manager:
      return

    try:
      # 移除不需要缓存的临时数据
      cache_result = result.copy()
      cache_result.pop("from_cache", None)
      cache_result.pop("cache_timestamp", None)

      # 验证数据大小（避免缓存过大的数据）
      import sys

      data_size = sys.getsizeof(str(cache_result))
      if data_size > 1024 * 1024:  # 1MB限制
        self.logger.warning(
          f"数据过大({data_size} bytes)，跳过缓存: {cache_key}"
        )
        return

      # 添加缓存元数据
      cache_result["cached_at"] = datetime.now().isoformat()
      cache_result["cache_ttl"] = ttl

      self._cache_manager.set(
        question=cache_key, response_data=cache_result, ttl=ttl
      )
      self.logger.debug(f"RCA结果已缓存: {cache_key}")
    except ConnectionError as e:
      self.logger.warning(f"Redis连接错误，无法保存缓存: {str(e)}")
      # 连接错误不影响主流程
    except Exception as e:
      self.logger.warning(f"保存RCA结果到缓存失败: {str(e)}")
      # 缓存失败不影响主流程

  async def get_config_info(self) -> Dict[str, Any]:
    """获取RCA服务配置信息"""
    try:
      self._ensure_initialized()

      config_info = {
        "service_name": self.service_name,
        "status": "initialized" if self._initialized else "not_initialized",
        "components": {
          "engine": self._engine is not None,
          "metrics_collector": self._metrics_collector is not None,
          "events_collector": self._events_collector is not None,
          "logs_collector": self._logs_collector is not None,
          "cache_manager": self._cache_manager is not None,
        },
        "cache_config": {
          "enabled": self._cache_manager is not None,
          "prefix": "rca_cache:" if self._cache_manager else None,
          "default_ttl": 1800,
        }
        if self._cache_manager
        else {"enabled": False},
        "analysis_limits": {
          "max_time_window_hours": 24,
          "min_time_window_hours": 0.1,
          "max_log_lines": 1000,
          "default_log_lines": 100,
        },
        "capabilities": [
          "root_cause_analysis",
          "metrics_analysis",
          "events_analysis",
          "logs_analysis",
          "quick_diagnosis",
          "event_patterns",
          "error_summary",
          "caching",
        ],
      }

      return config_info

    except Exception as e:
      self.logger.error(f"获取RCA配置信息失败: {str(e)}")
      raise RCAError(f"获取配置信息失败: {str(e)}")

  async def clear_all_cache(self) -> Dict[str, Any]:
    """清理所有RCA缓存"""
    try:
      if not self._cache_manager:
        return {
          "success": False,
          "message": "缓存管理器不可用",
          "cleared_count": 0,
        }

      # 获取所有RCA相关的缓存键
      cleared_count = await self._clear_cache_by_pattern("rca_cache:*")

      self.logger.info(f"已清理所有RCA缓存，清理数量: {cleared_count}")
      return {
        "success": True,
        "message": f"成功清理 {cleared_count} 个缓存项",
        "cleared_count": cleared_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
      }

    except Exception as e:
      self.logger.error(f"清理所有缓存失败: {str(e)}")
      return {
        "success": False,
        "message": f"清理缓存失败: {str(e)}",
        "cleared_count": 0,
      }

  async def clear_namespace_cache(self, namespace: str) -> Dict[str, Any]:
    """清理特定命名空间的缓存"""
    try:
      if not self._cache_manager:
        return {
          "success": False,
          "message": "缓存管理器不可用",
          "cleared_count": 0,
        }

      # 清理该命名空间相关的所有缓存
      pattern = f"rca_cache:*:{namespace}:*"
      cleared_count = await self._clear_cache_by_pattern(pattern)

      self.logger.info(
        f"已清理命名空间 {namespace} 的缓存，清理数量: {cleared_count}"
      )
      return {
        "success": True,
        "message": f"成功清理命名空间 {namespace} 的 {cleared_count} 个缓存项",
        "namespace": namespace,
        "cleared_count": cleared_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
      }

    except Exception as e:
      self.logger.error(f"清理命名空间 {namespace} 缓存失败: {str(e)}")
      return {
        "success": False,
        "message": f"清理缓存失败: {str(e)}",
        "namespace": namespace,
        "cleared_count": 0,
      }

  async def clear_operation_cache(self, operation: str) -> Dict[str, Any]:
    """清理特定操作类型的缓存"""
    try:
      if not self._cache_manager:
        return {
          "success": False,
          "message": "缓存管理器不可用",
          "cleared_count": 0,
        }

      # 清理该操作相关的所有缓存
      pattern = f"rca_cache:{operation}:*"
      cleared_count = await self._clear_cache_by_pattern(pattern)

      self.logger.info(
        f"已清理操作 {operation} 的缓存，清理数量: {cleared_count}"
      )
      return {
        "success": True,
        "message": f"成功清理操作 {operation} 的 {cleared_count} 个缓存项",
        "operation": operation,
        "cleared_count": cleared_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
      }

    except Exception as e:
      self.logger.error(f"清理操作 {operation} 缓存失败: {str(e)}")
      return {
        "success": False,
        "message": f"清理缓存失败: {str(e)}",
        "operation": operation,
        "cleared_count": 0,
      }

  async def get_cache_stats(self) -> Dict[str, Any]:
    """获取缓存统计信息"""
    try:
      if not self._cache_manager:
        return {
          "available": False,
          "message": "缓存管理器不可用",
        }

      # 获取缓存管理器的健康状态
      health_status = self._cache_manager.health_check()

      # 尝试获取Redis信息（如果可用）
      cache_info = {
        "available": True,
        "healthy": health_status.get("status") == "healthy"
        if isinstance(health_status, dict)
        else health_status,
        "cache_prefix": "rca_cache:",
        "default_ttl": 1800,
        "timestamp": datetime.now(timezone.utc).isoformat(),
      }

      # 如果缓存管理器有统计方法，调用它
      if hasattr(self._cache_manager, "get_stats"):
        try:
          stats = self._cache_manager.get_stats()
          cache_info.update(stats)
        except Exception as e:
          self.logger.debug(f"获取缓存统计信息失败: {str(e)}")

      return cache_info

    except Exception as e:
      self.logger.error(f"获取缓存统计失败: {str(e)}")
      return {
        "available": False,
        "message": f"获取缓存统计失败: {str(e)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
      }

  async def _clear_cache_by_pattern(self, pattern: str) -> int:
    """根据模式清理缓存（内部方法）"""
    try:
      if not self._cache_manager:
        return 0

      cleared_count = 0

      # 如果缓存管理器有按模式清理的方法
      if hasattr(self._cache_manager, "clear_by_pattern"):
        try:
          cleared_count = self._cache_manager.clear_by_pattern(pattern)
        except Exception as e:
          self.logger.warning(f"使用模式清理缓存失败: {str(e)}")

      # 备用方案：如果有批量删除功能
      elif hasattr(self._cache_manager, "delete_batch"):
        try:
          # 获取匹配的键（如果支持）
          if hasattr(self._cache_manager, "get_keys"):
            keys = self._cache_manager.get_keys(pattern)
            if keys:
              self._cache_manager.delete_batch(keys)
              cleared_count = len(keys)
        except Exception as e:
          self.logger.warning(f"使用批量删除清理缓存失败: {str(e)}")

      # 最基础的方案：清理所有缓存（谨慎使用）
      elif pattern == "rca_cache:*":
        try:
          if hasattr(self._cache_manager, "clear_all"):
            self._cache_manager.clear_all()
            cleared_count = 1  # 表示执行了清理操作
        except Exception as e:
          self.logger.warning(f"清理所有缓存失败: {str(e)}")

      return cleared_count

    except Exception as e:
      self.logger.error(f"按模式清理缓存失败: {str(e)}")
      return 0

  async def cleanup(self) -> None:
    """清理RCA服务资源"""
    try:
      self.logger.info("开始清理RCA服务资源...")

      # 清理缓存管理器
      if self._cache_manager:
        try:
          self._cache_manager.shutdown()
        except Exception as e:
          self.logger.warning(f"关闭RCA缓存管理器失败: {str(e)}")
        self._cache_manager = None

      # 清理收集器
      if self._metrics_collector:
        try:
          if hasattr(self._metrics_collector, "cleanup"):
            await self._metrics_collector.cleanup()
        except Exception as e:
          self.logger.warning(f"清理metrics_collector失败: {e}")
        self._metrics_collector = None

      if self._events_collector:
        try:
          if hasattr(self._events_collector, "cleanup"):
            await self._events_collector.cleanup()
        except Exception as e:
          self.logger.warning(f"清理events_collector失败: {e}")
        self._events_collector = None

      if self._logs_collector:
        try:
          if hasattr(self._logs_collector, "cleanup"):
            await self._logs_collector.cleanup()
        except Exception as e:
          self.logger.warning(f"清理logs_collector失败: {e}")
        self._logs_collector = None

      # 清理分析引擎
      if self._engine:
        try:
          if hasattr(self._engine, "cleanup"):
            await self._engine.cleanup()
        except Exception as e:
          self.logger.warning(f"清理RCA引擎失败: {e}")
        self._engine = None

      # 调用父类清理方法
      await super().cleanup()

      self.logger.info("RCA服务资源清理完成")

    except Exception as e:
      self.logger.error(f"RCA服务资源清理失败: {str(e)}")
      raise

  # 业务逻辑工具方法（从API层迁移）
  def parse_iso_timestamp(
    self, timestamp_str: Optional[str], field_name: str
  ) -> Optional[datetime]:
    """
    解析ISO格式时间戳的统一工具方法

    Args:
        timestamp_str: 时间戳字符串
        field_name: 字段名称用于错误提示

    Returns:
        解析后的datetime对象或None

    Raises:
        ValidationError: 时间格式错误时抛出
    """
    if not timestamp_str:
      return None

    try:
      return datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except ValueError as e:
      raise ValidationError(
        field_name, "无效的时间格式，请使用ISO格式", {"value": timestamp_str}
      ) from e

  def handle_service_error(self, operation: str, error: Exception) -> None:
    """
    统一的服务异常处理方法

    Args:
        operation: 操作名称
        error: 异常对象

    Raises:
        AIOpsException: 处理后的领域异常
    """
    error_message = f"{operation}失败: {str(error)}"
    self.logger.error(error_message, exc_info=True)

    # 将不同类型异常映射为领域异常，交由上层中间件统一处理
    if isinstance(error, (ValidationError,)):
      raise error
    if isinstance(error, ValueError):
      raise ValidationError("parameters", str(error))
    if isinstance(error, ConnectionError):
      raise AIOpsException(
        message=error_message,
        error_code="SERVICE_UNAVAILABLE",
        details={"operation": operation},
      )
    if isinstance(error, TimeoutError):
      raise AIOpsException(
        message=error_message,
        error_code="REQUEST_TIMEOUT",
        details={"operation": operation},
      )

    # 未知异常统一上抛
    raise AIOpsException(
      message=error_message,
      error_code="INTERNAL_ERROR",
      details={"operation": operation},
    )
