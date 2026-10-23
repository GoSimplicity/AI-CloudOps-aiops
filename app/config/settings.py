#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 配置管理模块
"""

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from app.config.base import BaseAppSettings

ROOT_DIR = Path(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
load_dotenv()
ENV = os.getenv("ENV", "development")


def load_config(strict_bool: bool = False) -> Dict[str, Any]:
    """加载配置文件（通过 BaseAppSettings 提供的统一加载器）

    strict_bool=True 时使用 StrictBoolSafeLoader：YAML 1.1 布尔别名（yes/on 等）保留为字符串。
    """
    try:
        data = BaseAppSettings.load_yaml_config_strict_bool() if strict_bool else BaseAppSettings.load_yaml_config()
        return data or {}
    except Exception as e:
        logger = logging.getLogger("aiops.config.settings")
        logger.exception("加载配置文件出错: %s", e)
        return {}


CONFIG = load_config()
# SEC-26 第二层审查 P2-2：autofix 开关专用视图。与 CONFIG 是同一份 YAML，但 yes/on 等不会被转成 True；
# 只有 _parse_autofix_bool 读取它，其它配置仍按 CONFIG（YAML 1.1 safe_load）解析，结果不变。
AUTOFIX_SWITCH_CONFIG = load_config(strict_bool=True)


def get_env_or_config(
    env_key: str, config_path: str, default: Any = None, transform: Any = None
) -> Any:
    """
    从环境变量或配置文件获取值

    Args:
        env_key: 环境变量键名
        config_path: 配置文件中的路径(使用点表示法)
        default: 默认值
        transform: 类型转换函数

    Returns:
        Any: 获取的配置值
    """
    parts = config_path.split(".")
    config_value = CONFIG
    for part in parts:
        config_value = (
            config_value.get(part, {}) if isinstance(config_value, dict) else {}
        )

    # An explicit false, zero or empty secret is a value, not a missing setting.
    env_value = os.getenv(env_key)
    value = env_value if env_value is not None else config_value
    if value == {} or value is None:
        value = default

    if transform and value is not None:
        if transform is bool and isinstance(value, str):
            return value.lower() == "true"
        return transform(value)
    return value


@dataclass
class PrometheusConfig:
    """Prometheus 监控系统配置"""

    host: str = field(
        default_factory=lambda: get_env_or_config(
            "PROMETHEUS_HOST", "prometheus.host", "127.0.0.1:9090"
        )
    )
    timeout: int = field(
        default_factory=lambda: get_env_or_config(
            "PROMETHEUS_TIMEOUT", "prometheus.timeout", 30, int
        )
    )

    @property
    def url(self) -> str:
        return f"http://{self.host}"


@dataclass
class LLMConfig:
    """大语言模型配置"""

    provider: str = field(
        default_factory=lambda: get_env_or_config(
            "LLM_PROVIDER", "llm.provider", "openai"
        )
        .split("#")[0]
        .strip()
    )
    model: str = field(
        default_factory=lambda: get_env_or_config(
            "LLM_MODEL", "llm.model", "Qwen/Qwen3-14B"
        )
    )
    task_model: str = field(
        default_factory=lambda: get_env_or_config(
            "LLM_TASK_MODEL", "llm.task_model", "Qwen/Qwen2.5-14B-Instruct"
        )
    )
    api_key: str = field(
        default_factory=lambda: get_env_or_config(
            "LLM_API_KEY", "llm.api_key", "sk-xxx"
        )
    )
    base_url: str = field(
        default_factory=lambda: get_env_or_config(
            "LLM_BASE_URL", "llm.base_url", "https://api.siliconflow.cn/v1"
        )
    )
    temperature: float = field(
        default_factory=lambda: get_env_or_config(
            "LLM_TEMPERATURE", "llm.temperature", 0.7, float
        )
    )
    max_tokens: int = field(
        default_factory=lambda: get_env_or_config(
            "LLM_MAX_TOKENS", "llm.max_tokens", 2048, int
        )
    )
    request_timeout: int = field(
        default_factory=lambda: get_env_or_config(
            "LLM_REQUEST_TIMEOUT", "llm.request_timeout", 360, int
        )
    )

    ollama_model: str = field(
        default_factory=lambda: get_env_or_config(
            "OLLAMA_MODEL", "llm.ollama_model", "qwen2.5:3b"
        )
    )
    ollama_base_url: str = field(
        default_factory=lambda: get_env_or_config(
            "OLLAMA_BASE_URL", "llm.ollama_base_url", "http://127.0.0.1:11434/v1"
        )
    )

    embedding_model: str = field(
        default_factory=lambda: get_env_or_config(
            "LLM_EMBEDDING_MODEL", "llm.embedding_model", "Pro/BAAI/bge-m3"
        )
    )

    @property
    def effective_model(self) -> str:
        if self.provider.lower() == "ollama":
            return self.ollama_model.split("#")[0].strip() if self.ollama_model else ""
        return self.model.split("#")[0].strip() if self.model else ""

    @property
    def effective_base_url(self) -> str:
        if self.provider.lower() == "ollama":
            return (
                self.ollama_base_url.split("#")[0].strip()
                if self.ollama_base_url
                else ""
            )
        return self.base_url.split("#")[0].strip() if self.base_url else ""

    @property
    def effective_api_key(self) -> str:
        return "ollama" if self.provider.lower() == "ollama" else self.api_key


@dataclass
class K8sConfig:
    """Kubernetes 集群配置"""

    in_cluster: bool = field(
        default_factory=lambda: get_env_or_config(
            "K8S_IN_CLUSTER", "kubernetes.in_cluster", False, bool
        )
    )
    config_path: Optional[str] = field(
        default_factory=lambda: get_env_or_config(
            "K8S_CONFIG_PATH", "kubernetes.config_path"
        )
        or str(ROOT_DIR / "deploy/kubernetes/config")
    )
    namespace: str = field(
        default_factory=lambda: get_env_or_config(
            "K8S_NAMESPACE", "kubernetes.namespace", "default"
        )
    )


@dataclass
class RCAConfig:
    """根因分析配置"""

    default_time_range: int = field(
        default_factory=lambda: get_env_or_config(
            "RCA_DEFAULT_TIME_RANGE", "rca.default_time_range", 30, int
        )
    )
    max_time_range: int = field(
        default_factory=lambda: get_env_or_config(
            "RCA_MAX_TIME_RANGE", "rca.max_time_range", 1440, int
        )
    )
    anomaly_threshold: float = field(
        default_factory=lambda: get_env_or_config(
            "RCA_ANOMALY_THRESHOLD", "rca.anomaly_threshold", 0.65, float
        )
    )
    correlation_threshold: float = field(
        default_factory=lambda: get_env_or_config(
            "RCA_CORRELATION_THRESHOLD", "rca.correlation_threshold", 0.7, float
        )
    )

    max_retries: int = field(
        default_factory=lambda: get_env_or_config(
            "RCA_MAX_RETRIES", "rca.max_retries", 3, int
        )
    )

    timeout: int = field(
        default_factory=lambda: get_env_or_config("RCA_TIMEOUT", "rca.timeout", 30, int)
    )

    default_metrics: List[str] = field(
        default_factory=lambda: CONFIG.get("rca", {}).get(
            "default_metrics",
            [
                "container_cpu_usage_seconds_total",
                "container_memory_working_set_bytes",
                "kube_pod_container_status_restarts_total",
                "kube_pod_status_phase",
                "node_cpu_seconds_total",
                "node_memory_MemFree_bytes",
                "kubelet_http_requests_duration_seconds_count",
                "kubelet_http_requests_duration_seconds_sum",
            ],
        )
    )


@dataclass
class PredictionConfig:
    """预测模型配置"""

    model_base_path: str = field(
        default_factory=lambda: get_env_or_config(
            "PREDICTION_MODEL_BASE_PATH", "prediction.model_base_path", "data/models"
        )
    )

    max_instances: int = field(
        default_factory=lambda: get_env_or_config(
            "PREDICTION_MAX_INSTANCES", "prediction.max_instances", 20, int
        )
    )
    min_instances: int = field(
        default_factory=lambda: get_env_or_config(
            "PREDICTION_MIN_INSTANCES", "prediction.min_instances", 1, int
        )
    )

    prometheus_query: str = field(
        default_factory=lambda: get_env_or_config(
            "PREDICTION_PROMETHEUS_QUERY",
            "prediction.prometheus_query",
            'rate(node_network_receive_bytes_total{device!="lo"}[10m])',
        )
    )

    default_prediction_hours: int = field(
        default_factory=lambda: get_env_or_config(
            "PREDICTION_DEFAULT_HOURS", "prediction.default_prediction_hours", 24, int
        )
    )
    max_prediction_hours: int = field(
        default_factory=lambda: get_env_or_config(
            "PREDICTION_MAX_HOURS", "prediction.max_prediction_hours", 168, int
        )
    )
    min_prediction_hours: int = field(
        default_factory=lambda: get_env_or_config(
            "PREDICTION_MIN_HOURS", "prediction.min_prediction_hours", 1, int
        )
    )
    default_granularity: str = field(
        default_factory=lambda: get_env_or_config(
            "PREDICTION_DEFAULT_GRANULARITY", "prediction.default_granularity", "hour"
        )
    )
    default_target_utilization: float = field(
        default_factory=lambda: get_env_or_config(
            "PREDICTION_DEFAULT_TARGET_UTILIZATION",
            "prediction.default_target_utilization",
            0.7,
            float,
        )
    )
    default_sensitivity: float = field(
        default_factory=lambda: get_env_or_config(
            "PREDICTION_DEFAULT_SENSITIVITY",
            "prediction.default_sensitivity",
            0.8,
            float,
        )
    )

    @property
    def model_paths(self) -> Dict[str, Dict[str, str]]:
        """获取模型路径配置"""
        return CONFIG.get("prediction", {}).get("model_paths", {})

    @property
    def scaling_thresholds(self) -> Dict[str, Dict[str, float]]:
        """获取扩缩容阈值配置"""
        return CONFIG.get("prediction", {}).get("scaling_thresholds", {})

    @property
    def cooldown_periods(self) -> Dict[str, int]:
        """获取冷却时间配置"""
        return CONFIG.get("prediction", {}).get("cooldown_periods", {})

    @property
    def cost_analysis_config(self) -> Dict[str, Any]:
        """获取成本分析配置"""
        return CONFIG.get("prediction", {}).get("cost_analysis", {})

    @property
    def anomaly_detection_config(self) -> Dict[str, Any]:
        """获取异常检测配置"""
        return CONFIG.get("prediction", {}).get("anomaly_detection", {})


@dataclass
class InspectionConfig:
    """Kubernetes 智能巡检配置"""

    enabled: bool = field(
        default_factory=lambda: get_env_or_config(
            "INSPECTION_ENABLED", "inspection.enabled", True, bool
        )
    )
    default_profile: str = field(
        default_factory=lambda: get_env_or_config(
            "INSPECTION_DEFAULT_PROFILE", "inspection.default_profile", "basic"
        )
    )
    severity_threshold: float = field(
        default_factory=lambda: get_env_or_config(
            "INSPECTION_SEVERITY_THRESHOLD",
            "inspection.severity_threshold",
            0.5,
            float,
        )
    )
    time_window_minutes: int = field(
        default_factory=lambda: get_env_or_config(
            "INSPECTION_TIME_WINDOW_MINUTES", "inspection.time_window_minutes", 60, int
        )
    )
    # K8s 采集相关
    include_events: bool = field(
        default_factory=lambda: get_env_or_config(
            "INSPECTION_INCLUDE_EVENTS", "inspection.k8s.include_events", True, bool
        )
    )
    include_logs: bool = field(
        default_factory=lambda: get_env_or_config(
            "INSPECTION_INCLUDE_LOGS", "inspection.k8s.include_logs", False, bool
        )
    )
    log_tail_lines: int = field(
        default_factory=lambda: get_env_or_config(
            "INSPECTION_LOG_TAIL_LINES", "inspection.k8s.log_tail_lines", 200, int
        )
    )
    # Prometheus 查询相关
    prometheus_step: str = field(
        default_factory=lambda: get_env_or_config(
            "INSPECTION_PROMETHEUS_STEP", "inspection.prometheus.step", "1m"
        )
    )
    prometheus_queries_timeout: int = field(
        default_factory=lambda: get_env_or_config(
            "INSPECTION_PROMETHEUS_QUERIES_TIMEOUT",
            "inspection.prometheus.queries_timeout",
            10,
            int,
        )
    )
    # 历史保留
    retention_enabled: bool = field(
        default_factory=lambda: get_env_or_config(
            "INSPECTION_RETENTION_ENABLED", "inspection.retention.enabled", True, bool
        )
    )
    max_reports: int = field(
        default_factory=lambda: get_env_or_config(
            "INSPECTION_MAX_REPORTS", "inspection.retention.max_reports", 200, int
        )
    )
    # 简单调度开关（本实现不内置调度，仅暴露配置以便外部调用）
    scheduler_enabled: bool = field(
        default_factory=lambda: get_env_or_config(
            "INSPECTION_SCHEDULER_ENABLED", "inspection.scheduler.enabled", False, bool
        )
    )
    scheduler_cron: str = field(
        default_factory=lambda: get_env_or_config(
            "INSPECTION_SCHEDULER_CRON", "inspection.scheduler.cron", "*/30 * * * *"
        )
    )


@dataclass
class NotificationConfig:
    """通知配置"""

    feishu_webhook: str = field(
        repr=False,
        default_factory=lambda: get_env_or_config(
            "FEISHU_WEBHOOK", "notification.feishu_webhook", ""
        )
    )
    enabled: bool = field(
        default_factory=lambda: get_env_or_config(
            "NOTIFICATION_ENABLED", "notification.enabled", True, bool
        )
    )


@dataclass
class TavilyConfig:
    """Tavily 搜索引擎配置"""

    api_key: str = field(
        default_factory=lambda: get_env_or_config(
            "TAVILY_API_KEY", "tavily.api_key", ""
        )
    )
    max_results: int = field(
        default_factory=lambda: get_env_or_config(
            "TAVILY_MAX_RESULTS", "tavily.max_results", 5, int
        )
    )


@dataclass
class RedisConfig:
    """Redis 向量存储配置"""

    host: str = field(
        default_factory=lambda: get_env_or_config(
            "REDIS_HOST", "redis.host", "localhost"
        )
    )
    port: int = field(
        default_factory=lambda: get_env_or_config("REDIS_PORT", "redis.port", 6379, int)
    )
    db: int = field(
        default_factory=lambda: get_env_or_config("REDIS_DB", "redis.db", 0, int)
    )
    password: str = field(
        default_factory=lambda: get_env_or_config(
            "REDIS_PASSWORD", "redis.password", ""
        )
    )
    connection_timeout: int = field(
        default_factory=lambda: get_env_or_config(
            "REDIS_CONNECTION_TIMEOUT", "redis.connection_timeout", 5, int
        )
    )
    socket_timeout: int = field(
        default_factory=lambda: get_env_or_config(
            "REDIS_SOCKET_TIMEOUT", "redis.socket_timeout", 5, int
        )
    )
    max_connections: int = field(
        default_factory=lambda: get_env_or_config(
            "REDIS_MAX_CONNECTIONS", "redis.max_connections", 10, int
        )
    )
    decode_responses: bool = field(
        default_factory=lambda: get_env_or_config(
            "REDIS_DECODE_RESPONSES", "redis.decode_responses", True, bool
        )
    )


@dataclass
class RAGConfig:
    """RAG 智能助手配置"""

    vector_db_path: str = field(
        default_factory=lambda: get_env_or_config(
            "RAG_VECTOR_DB_PATH", "rag.vector_db_path", "data/vector_db"
        )
    )
    collection_name: str = field(
        default_factory=lambda: get_env_or_config(
            "RAG_COLLECTION_NAME", "rag.collection_name", "aiops-assistant"
        )
    )
    knowledge_base_path: str = field(
        default_factory=lambda: get_env_or_config(
            "RAG_KNOWLEDGE_BASE_PATH", "rag.knowledge_base_path", "data/knowledge_base"
        )
    )
    chunk_size: int = field(
        default_factory=lambda: get_env_or_config(
            "RAG_CHUNK_SIZE", "rag.chunk_size", 1000, int
        )
    )
    chunk_overlap: int = field(
        default_factory=lambda: get_env_or_config(
            "RAG_CHUNK_OVERLAP", "rag.chunk_overlap", 200, int
        )
    )
    top_k: int = field(
        default_factory=lambda: get_env_or_config("RAG_TOP_K", "rag.top_k", 8, int)
    )
    similarity_threshold: float = field(
        default_factory=lambda: get_env_or_config(
            "RAG_SIMILARITY_THRESHOLD", "rag.similarity_threshold", 0.3, float
        )
    )
    openai_embedding_model: str = field(
        default_factory=lambda: get_env_or_config(
            "RAG_OPENAI_EMBEDDING_MODEL",
            "rag.openai_embedding_model",
            "Pro/BAAI/bge-m3",
        )
    )
    ollama_embedding_model: str = field(
        default_factory=lambda: get_env_or_config(
            "RAG_OLLAMA_EMBEDDING_MODEL",
            "rag.ollama_embedding_model",
            "nomic-embed-text",
        )
    )
    max_context_length: int = field(
        default_factory=lambda: get_env_or_config(
            "RAG_MAX_CONTEXT_LENGTH", "rag.max_context_length", 4000, int
        )
    )
    temperature: float = field(
        default_factory=lambda: get_env_or_config(
            "RAG_TEMPERATURE", "rag.temperature", 0.1, float
        )
    )
    timeout: int = field(
        default_factory=lambda: get_env_or_config(
            "RAG_TIMEOUT", "rag.timeout", 360, int
        )
    )
    max_docs_per_query: int = field(
        default_factory=lambda: get_env_or_config(
            "RAG_MAX_DOCS_PER_QUERY", "rag.max_docs_per_query", 10, int
        )
    )
    cache_expiry: int = field(
        default_factory=lambda: get_env_or_config(
            "RAG_CACHE_EXPIRY", "rag.cache_expiry", 3600, int
        )
    )

    @property
    def effective_embedding_model(self) -> str:
        llm_provider = get_env_or_config(
            "LLM_PROVIDER", "llm.provider", "openai"
        ).lower()
        return (
            self.ollama_embedding_model
            if llm_provider == "ollama"
            else self.openai_embedding_model
        )


@dataclass
class MCPConfig:
    """MCP配置"""

    server_url: str = field(
        default_factory=lambda: get_env_or_config(
            "MCP_SERVER_URL", "mcp.server_url", "http://localhost:9000"
        )
    )
    timeout: int = field(
        default_factory=lambda: get_env_or_config("MCP_TIMEOUT", "mcp.timeout", 30, int)
    )
    max_retries: int = field(
        default_factory=lambda: get_env_or_config(
            "MCP_MAX_RETRIES", "mcp.max_retries", 3, int
        )
    )
    health_check_interval: int = field(
        default_factory=lambda: get_env_or_config(
            "MCP_HEALTH_CHECK_INTERVAL", "mcp.health_check_interval", 30, int
        )
    )
    # SEC-33：MCP 写工具总开关，默认关闭；解析规则同 autofix 开关（strip+lower，仅 "true" 开启）
    write_tools_enabled: bool = field(
        default_factory=lambda: _parse_autofix_bool(
            "MCP_WRITE_TOOLS_ENABLED", "mcp.write_tools_enabled"
        )
    )
    # SEC-33：list_secrets/get_secret 开关，默认关闭；开启后仍受 autofix.allowed_namespaces 约束且不返回 data
    secret_read_enabled: bool = field(
        default_factory=lambda: _parse_autofix_bool(
            "MCP_SECRET_READ_ENABLED", "mcp.secret_read_enabled"
        )
    )


@dataclass
class TimeConfig:
    """时间配置"""

    holidays: List[str] = field(
        default_factory=lambda: CONFIG.get("time", {}).get(
            "holidays",
            [
                "0101",
                "0102",
                "0103",  # 元旦
                "0210",
                "0211",
                "0212",
                "0213",
                "0214",
                "0215",
                "0216",
                "0217",  # 春节
                "0404",
                "0405",
                "0406",  # 清明节
                "0501",
                "0502",
                "0503",
                "0504",
                "0505",  # 劳动节
                "0610",
                "0611",
                "0612",  # 端午节
                "0917",
                "0918",
                "0919",  # 中秋节
                "1001",
                "1002",
                "1003",
                "1004",
                "1005",
                "1006",
                "1007",  # 国庆节
            ],
        )
    )


@dataclass
class TestingConfig:
    """测试配置"""

    skip_llm_tests: bool = field(
        default_factory=lambda: get_env_or_config(
            "TESTING_SKIP_LLM_TESTS", "testing.skip_llm_tests", False, bool
        )
    )


@dataclass
class SecurityConfig:
    """安全配置"""

    api_key: str = field(default_factory=lambda: os.getenv("AIOPS_API_KEY", ""))

    jwt_secret: str = field(
        default_factory=lambda: get_env_or_config(
            "JWT_SECRET", "security.jwt_secret", ""
        )
    )
    api_rate_limit: int = field(
        default_factory=lambda: get_env_or_config(
            "API_RATE_LIMIT", "security.api_rate_limit", 100, int
        )
    )
    cors_origins: str = field(
        default_factory=lambda: get_env_or_config(
            "CORS_ORIGINS",
            "security.cors_origins",
            "http://localhost:3000,http://localhost:8080",
        )
    )


@dataclass
class DevConfig:
    """开发配置"""

    auto_reload: bool = field(
        default_factory=lambda: get_env_or_config(
            "AUTO_RELOAD", "dev.auto_reload", False, bool
        )
    )
    enable_debug_endpoints: bool = field(
        default_factory=lambda: get_env_or_config(
            "ENABLE_DEBUG_ENDPOINTS", "dev.enable_debug_endpoints", False, bool
        )
    )


def _parse_namespace_list(value: Any) -> List[str]:
    """把逗号分隔字符串或 YAML 列表规范化为去重后的命名空间列表"""
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value) if isinstance(value, (list, tuple, set)) else []
    result: List[str] = []
    for item in items:
        name = str(item).strip()
        if name and name not in result:
            result.append(name)
    return result


_autofix_config_logger = logging.getLogger("aiops.config.autofix")


# YAML 1.1 会把这些不加引号的写法当成布尔；autofix 开关不认它们（StrictBoolSafeLoader 保留为字符串）
_YAML11_BOOL_ALIASES = frozenset({"yes", "no", "on", "off", "y", "n"})


def _get_autofix_switch_value(env_key: str, config_path: str) -> Any:
    """环境变量优先；否则从 AUTOFIX_SWITCH_CONFIG（严格布尔 YAML 视图）取值；未设置返回 None"""
    env_value = os.getenv(env_key)
    if env_value is not None:
        return env_value
    value: Any = AUTOFIX_SWITCH_CONFIG
    for part in config_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _parse_autofix_bool(env_key: str, config_path: str) -> bool:
    """autofix 开关解析规则：先 strip，再 lower，等于 true 才开启；false 为关闭。

    其它任何值（含空串、0/1、yes/no/on/off、YAML 数字 1/0）按关闭处理并告警。
    YAML 中只有原生布尔 true/True/TRUE 为开（SEC-26 第二层审查 P2-2）；不加引号的 yes/on/On 不再按
    YAML 1.1 视为 true。SEC-33 的 mcp.write_tools_enabled / mcp.secret_read_enabled 沿用同一规则。
    """
    value = _get_autofix_switch_value(env_key, config_path)
    if value is None:
        return False
    if value is True or value is False:
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    hint = "（yes/on 等 YAML 1.1 布尔写法不被接受，请写 true）" if isinstance(value, str) and value.strip().lower() in _YAML11_BOOL_ALIASES else ""
    # 只输出字段名与值的类型，不输出值本身（SEC-26 P3-c：配置值可能被误填为敏感内容）
    _autofix_config_logger.warning(
        "开关 %s / %s 的值（类型 %s）不是 true/false，已按 false（关闭）处理；只有 true 会开启%s",
        env_key,
        config_path,
        type(value).__name__,
        hint,
    )
    return False


def _warn_suspicious_namespaces(namespaces: List[str]) -> None:
    """白名单大小写敏感且不支持通配符：出现 * 或含大写字母的条目时告警（不改写，按字面匹配）"""
    # 只输出条目序号（从 1 开始），不输出条目值本身（SEC-26 P3-c）
    for index, name in enumerate(namespaces, start=1):
        if "*" in name:
            _autofix_config_logger.warning(
                "autofix.allowed_namespaces / AUTOFIX_ALLOWED_NAMESPACES 第 %d 项含 *：* 不是通配符，只会按字面匹配（实际不会匹配任何命名空间）",
                index,
            )
        if any(ch.isupper() for ch in name):
            _autofix_config_logger.warning(
                "autofix.allowed_namespaces / AUTOFIX_ALLOWED_NAMESPACES 第 %d 项含大写字母：白名单大小写敏感，Kubernetes 命名空间均为小写，该条目不会匹配",
                index,
            )


@dataclass
class AutoFixConfig:
    """自动修复执行边界配置；默认全部关闭（fail-closed）"""

    auto_execute_enabled: bool = field(
        default_factory=lambda: _parse_autofix_bool("AUTOFIX_AUTO_EXECUTE_ENABLED", "autofix.auto_execute_enabled")
    )
    allowed_namespaces: List[str] = field(
        default_factory=lambda: _parse_namespace_list(
            get_env_or_config("AUTOFIX_ALLOWED_NAMESPACES", "autofix.allowed_namespaces", [])
        )
    )
    repair_enabled: bool = field(
        default_factory=lambda: _parse_autofix_bool("AUTOFIX_REPAIR_ENABLED", "autofix.repair_enabled")
    )
    bootstrap_enabled: bool = field(
        default_factory=lambda: _parse_autofix_bool("AUTOFIX_BOOTSTRAP_ENABLED", "autofix.bootstrap_enabled")
    )

    def __post_init__(self) -> None:
        _warn_suspicious_namespaces(list(self.allowed_namespaces or []))


@dataclass
class AppConfig:
    """应用程序主配置类"""

    env: str = field(
        default_factory=lambda: get_env_or_config("ENV", "app.env", "development")
    )
    debug: bool = field(
        default_factory=lambda: get_env_or_config("DEBUG", "app.debug", False, bool)
    )
    host: str = field(
        default_factory=lambda: get_env_or_config("HOST", "app.host", "0.0.0.0")
    )
    port: int = field(
        default_factory=lambda: get_env_or_config("PORT", "app.port", 8080, int)
    )
    log_level: str = field(
        default_factory=lambda: get_env_or_config("LOG_LEVEL", "app.log_level", "INFO")
    )

    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    k8s: K8sConfig = field(default_factory=K8sConfig)
    rca: RCAConfig = field(default_factory=RCAConfig)
    prediction: PredictionConfig = field(default_factory=PredictionConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    tavily: TavilyConfig = field(default_factory=TavilyConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    rag: RAGConfig = field(default_factory=RAGConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    time: TimeConfig = field(default_factory=TimeConfig)
    testing: TestingConfig = field(default_factory=TestingConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    dev: DevConfig = field(default_factory=DevConfig)
    inspection: InspectionConfig = field(default_factory=InspectionConfig)
    autofix: AutoFixConfig = field(default_factory=AutoFixConfig)


config = AppConfig()


class Settings(BaseAppSettings):
    """Pydantic Settings（顶层示例字段），用于提供标准的 ENV 管理入口。
    实际业务仍通过 `config` （dataclass 聚合）访问，以保证向后兼容。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: str = Field(default="development", alias="ENV")
    debug: bool = Field(default=False, alias="DEBUG")
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8080, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


# 保留一个标准 Settings 实例（可供后续迁移逐步使用）
settings = Settings()
