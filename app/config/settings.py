#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 应用程序配置管理模块
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
load_dotenv()
ENV = os.getenv("ENV", "development")


def load_config() -> Dict[str, Any]:
    """加载配置文件"""
    config_file = (
        ROOT_DIR / "config" / f"config{'.' + ENV if ENV != 'development' else ''}.yaml"
    )
    default_config_file = ROOT_DIR / "config" / "config.yaml"

    try:
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        elif default_config_file.exists():
            with open(default_config_file, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        else:
            print("警告: 未找到配置文件，将使用环境变量默认值")
            return {}
    except Exception as e:
        print(f"加载配置文件出错: {e}")
        return {}


_config_data = load_config()


def _cast_value(value: Any, cast_type):
    """将原始值按类型安全转换。

    - bool: 接受 true/false/yes/no/on/off/1/0（大小写不敏感）
    - list: 若为字符串，优先尝试 JSON 解析，否则按逗号分隔并 strip；若为列表则原样返回
    - 其它: 直接使用传入的 cast_type 转换
    """
    if value is None:
        return None

    # 布尔解析
    if cast_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "f", "no", "n", "off"}:
            return False
        # 无法判定时返回 False 以避免将 "false" 识别为 True
        return False

    # 列表解析
    if cast_type is list:
        if isinstance(value, list):
            return value
        text = str(value).strip()
        # 优先尝试 JSON 数组
        if (text.startswith("[") and text.endswith("]")) or (
            text.startswith("\"") and text.endswith("\"")
        ):
            try:
                loaded = json.loads(text)
                if isinstance(loaded, list):
                    return loaded
                # 若是逗号字符串，继续走分割逻辑
            except Exception:
                pass
        # 按逗号分割
        parts = [p.strip() for p in text.split(",") if p.strip()]
        return parts

    # 其它基础类型
    try:
        return cast_type(value)
    except Exception:
        return None


def get_env_or_config(env_key: str, config_key: str, default: Any = None, cast_type=str):
    """从环境变量或配置文件中获取值（优先环境变量），并进行安全类型转换。"""
    # 1) 环境变量优先
    env_value = os.getenv(env_key)
    if env_value is not None:
        converted = _cast_value(env_value, cast_type)
        if converted is not None:
            return converted

    # 2) 配置文件读取（支持 a.b.c 嵌套）
    keys = config_key.split('.')
    cursor: Any = _config_data
    for key in keys:
        if isinstance(cursor, dict) and key in cursor:
            cursor = cursor[key]
        else:
            return default

    converted = _cast_value(cursor, cast_type)
    return converted if converted is not None else default


@dataclass
class PrometheusConfig:
    """Prometheus配置"""
    host: str = field(
        default_factory=lambda: get_env_or_config("PROMETHEUS_HOST", "prometheus.host", "127.0.0.1:9090")
    )
    timeout: int = field(
        default_factory=lambda: get_env_or_config("PROMETHEUS_TIMEOUT", "prometheus.timeout", 30, int)
    )

    @property
    def url(self) -> str:
        return f"http://{self.host}"


@dataclass
class LLMConfig:
    """LLM配置"""
    provider: str = field(
        default_factory=lambda: get_env_or_config("LLM_PROVIDER", "llm.provider", "openai")
    )
    model: str = field(
        default_factory=lambda: get_env_or_config("LLM_MODEL", "llm.model", "Qwen/Qwen3-14B")
    )
    task_model: str = field(
        default_factory=lambda: get_env_or_config("LLM_TASK_MODEL", "llm.task_model", "Qwen/Qwen2.5-14B-Instruct")
    )
    api_key: str = field(
        default_factory=lambda: get_env_or_config("LLM_API_KEY", "llm.api_key", "sk-xxx")
    )
    base_url: str = field(
        default_factory=lambda: get_env_or_config("LLM_BASE_URL", "llm.base_url", "https://api.siliconflow.cn/v1")
    )
    temperature: float = field(
        default_factory=lambda: get_env_or_config("LLM_TEMPERATURE", "llm.temperature", 0.7, float)
    )
    max_tokens: int = field(
        default_factory=lambda: get_env_or_config("LLM_MAX_TOKENS", "llm.max_tokens", 2048, int)
    )
    request_timeout: int = field(
        default_factory=lambda: get_env_or_config("LLM_REQUEST_TIMEOUT", "llm.request_timeout", 15, int)
    )
    ollama_model: str = field(
        default_factory=lambda: get_env_or_config("OLLAMA_MODEL", "llm.ollama_model", "qwen2.5:3b")
    )
    ollama_base_url: str = field(
        default_factory=lambda: get_env_or_config("OLLAMA_BASE_URL", "llm.ollama_base_url", "http://127.0.0.1:11434/v1")
    )

    @property
    def effective_model(self) -> str:
        return self.ollama_model if self.provider.lower() == "ollama" else self.model

    @property
    def effective_base_url(self) -> str:
        return self.ollama_base_url if self.provider.lower() == "ollama" else self.base_url

    @property
    def effective_api_key(self) -> str:
        return "" if self.provider.lower() == "ollama" else self.api_key

    @property
    def embedding_model(self) -> str:
        if self.provider.lower() == "ollama":
            return get_env_or_config("", "rag.ollama_embedding_model", "nomic-embed-text")
        else:
            return get_env_or_config("", "rag.openai_embedding_model", "Pro/BAAI/bge-m3")


@dataclass
class K8sConfig:
    """Kubernetes配置"""
    in_cluster: bool = field(
        default_factory=lambda: get_env_or_config("K8S_IN_CLUSTER", "kubernetes.in_cluster", False, bool)
    )
    config_path: str = field(
        default_factory=lambda: get_env_or_config("K8S_CONFIG_PATH", "kubernetes.config_path", "deploy/kubernetes/config")
    )
    namespace: str = field(
        default_factory=lambda: get_env_or_config("K8S_NAMESPACE", "kubernetes.namespace", "default")
    )


@dataclass
class RemediationConfig:
    """自动修复/多Agent配置"""
    enabled: bool = field(
        default_factory=lambda: get_env_or_config("REMEDIATION_ENABLED", "remediation.enabled", True, bool)
    )
    dry_run: bool = field(
        default_factory=lambda: get_env_or_config("REMEDIATION_DRY_RUN", "remediation.dry_run", True, bool)
    )
    allow_rollback: bool = field(
        default_factory=lambda: get_env_or_config("REMEDIATION_ALLOW_ROLLBACK", "remediation.allow_rollback", True, bool)
    )
    verify_wait_seconds: int = field(
        default_factory=lambda: get_env_or_config("REMEDIATION_VERIFY_WAIT", "remediation.verify_wait_seconds", 20, int)
    )
    max_concurrent: int = field(
        default_factory=lambda: get_env_or_config("REMEDIATION_MAX_CONCURRENT", "remediation.max_concurrent", 3, int)
    )
    safe_mode: bool = field(
        default_factory=lambda: get_env_or_config("REMEDIATION_SAFE_MODE", "remediation.safe_mode", False, bool)
    )
    namespace_whitelist: List[str] = field(
        default_factory=lambda: get_env_or_config("REMEDIATION_NS_WHITELIST", "remediation.namespace_whitelist", [], list)
    )
    namespace_blacklist: List[str] = field(
        default_factory=lambda: get_env_or_config("REMEDIATION_NS_BLACKLIST", "remediation.namespace_blacklist", [], list)
    )


@dataclass
class RCAConfig:
    """根因分析配置"""
    default_time_range: int = field(
        default_factory=lambda: get_env_or_config("RCA_DEFAULT_TIME_RANGE", "rca.default_time_range", 30, int)
    )
    max_time_range: int = field(
        default_factory=lambda: get_env_or_config("RCA_MAX_TIME_RANGE", "rca.max_time_range", 1440, int)
    )
    anomaly_threshold: float = field(
        default_factory=lambda: get_env_or_config("RCA_ANOMALY_THRESHOLD", "rca.anomaly_threshold", 0.65, float)
    )
    correlation_threshold: float = field(
        default_factory=lambda: get_env_or_config("RCA_CORRELATION_THRESHOLD", "rca.correlation_threshold", 0.7, float)
    )
    request_override: bool = field(
        default_factory=lambda: get_env_or_config("RCA_REQUEST_OVERRIDE", "rca.request_override", False, bool)
    )
    default_metrics: List[str] = field(
        default_factory=lambda: get_env_or_config("", "rca.default_metrics", [
            "container_cpu_usage_seconds_total",
            "container_memory_working_set_bytes", 
            "kube_pod_container_status_restarts_total",
            "node_cpu_seconds_total",
            "node_memory_MemFree_bytes"
        ])
    )


@dataclass
class LogsConfig:
    """容器/Pod 日志采集配置"""
    enabled: bool = field(
        default_factory=lambda: get_env_or_config("RCA_LOGS_ENABLED", "logs.enabled", False, bool)
    )
    tail_lines: int = field(
        default_factory=lambda: get_env_or_config("RCA_LOGS_TAIL_LINES", "logs.tail_lines", 200, int)
    )
    max_pods: int = field(
        default_factory=lambda: get_env_or_config("RCA_LOGS_MAX_PODS", "logs.max_pods", 5, int)
    )
    include_previous: bool = field(
        default_factory=lambda: get_env_or_config("RCA_LOGS_INCLUDE_PREVIOUS", "logs.include_previous", False, bool)
    )


@dataclass
class TracingConfig:
    """Trace/OTel/Jaeger 采集配置"""
    enabled: bool = field(
        default_factory=lambda: get_env_or_config("RCA_TRACING_ENABLED", "tracing.enabled", False, bool)
    )
    provider: str = field(
        default_factory=lambda: get_env_or_config("RCA_TRACING_PROVIDER", "tracing.provider", "jaeger")
    )
    jaeger_query_url: str = field(
        default_factory=lambda: get_env_or_config("RCA_JAEGER_QUERY_URL", "tracing.jaeger_query_url", "http://127.0.0.1:16686")
    )
    timeout: int = field(
        default_factory=lambda: get_env_or_config("RCA_TRACING_TIMEOUT", "tracing.timeout", 15, int)
    )
    service_name: Optional[str] = field(
        default_factory=lambda: get_env_or_config("RCA_TRACING_SERVICE", "tracing.service_name", None)
    )
    max_traces: int = field(
        default_factory=lambda: get_env_or_config("RCA_TRACING_MAX_TRACES", "tracing.max_traces", 30, int)
    )


@dataclass
class PredictionConfig:
    """预测配置"""
    model_path: str = field(
        default_factory=lambda: get_env_or_config("PREDICTION_MODEL_PATH", "prediction.model_path", "data/models/time_qps_auto_scaling_model.pkl")
    )
    scaler_path: str = field(
        default_factory=lambda: get_env_or_config("PREDICTION_SCALER_PATH", "prediction.scaler_path", "data/models/time_qps_auto_scaling_scaler.pkl")
    )
    max_instances: int = field(
        default_factory=lambda: get_env_or_config("PREDICTION_MAX_INSTANCES", "prediction.max_instances", 20, int)
    )
    min_instances: int = field(
        default_factory=lambda: get_env_or_config("PREDICTION_MIN_INSTANCES", "prediction.min_instances", 1, int)
    )


@dataclass
class NotificationConfig:
    """通知配置"""
    enabled: bool = field(
        default_factory=lambda: get_env_or_config("NOTIFICATION_ENABLED", "notification.enabled", True, bool)
    )
    feishu_webhook: str = field(
        default_factory=lambda: get_env_or_config("FEISHU_WEBHOOK", "notification.feishu_webhook", "")
    )


@dataclass
class RedisConfig:
    """Redis配置"""
    host: str = field(
        default_factory=lambda: get_env_or_config("REDIS_HOST", "redis.host", "127.0.0.1")
    )
    port: int = field(
        default_factory=lambda: get_env_or_config("REDIS_PORT", "redis.port", 6379, int)
    )
    db: int = field(
        default_factory=lambda: get_env_or_config("REDIS_DB", "redis.db", 0, int)
    )
    password: Optional[str] = field(
        default_factory=lambda: get_env_or_config("REDIS_PASSWORD", "redis.password", None)
    )
    decode_responses: bool = field(
        default_factory=lambda: get_env_or_config("REDIS_DECODE_RESPONSES", "redis.decode_responses", False, bool)
    )
    connection_timeout: int = field(
        default_factory=lambda: get_env_or_config("REDIS_CONNECTION_TIMEOUT", "redis.connection_timeout", 5, int)
    )
    max_connections: int = field(
        default_factory=lambda: get_env_or_config("REDIS_MAX_CONNECTIONS", "redis.max_connections", 10, int)
    )
    socket_timeout: int = field(
        default_factory=lambda: get_env_or_config("REDIS_SOCKET_TIMEOUT", "redis.socket_timeout", 5, int)
    )


@dataclass
class DatabaseConfig:
    """MySQL 数据库配置"""
    host: str = field(
        default_factory=lambda: get_env_or_config("DB_HOST", "database.host", "localhost")
    )
    port: int = field(
        default_factory=lambda: get_env_or_config("DB_PORT", "database.port", 3306, int)
    )
    username: str = field(
        default_factory=lambda: get_env_or_config("DB_USERNAME", "database.username", "root")
    )
    password: str = field(
        default_factory=lambda: get_env_or_config("DB_PASSWORD", "database.password", "root")
    )
    name: str = field(
        default_factory=lambda: get_env_or_config("DB_NAME", "database.name", "cloudops")
    )
    echo: bool = field(
        default_factory=lambda: get_env_or_config("DB_ECHO", "database.echo", False, bool)
    )
    pool_size: int = field(
        default_factory=lambda: get_env_or_config("DB_POOL_SIZE", "database.pool_size", 5, int)
    )
    max_overflow: int = field(
        default_factory=lambda: get_env_or_config("DB_MAX_OVERFLOW", "database.max_overflow", 10, int)
    )
    pool_recycle: int = field(
        default_factory=lambda: get_env_or_config("DB_POOL_RECYCLE", "database.pool_recycle", 1800, int)
    )

    @property
    def sqlalchemy_url(self) -> str:
        # 使用 PyMySQL 驱动，注意不影响已有主平台表，仅创建 cl_aiops_ 前缀表
        # 为兼容特殊字符，用户名与密码进行URL编码
        user = quote_plus(str(self.username or ""))
        pwd = quote_plus(str(self.password or ""))
        return (
            f"mysql+pymysql://{user}:{pwd}@{self.host}:{self.port}/{self.name}?charset=utf8mb4"
        )

@dataclass
class RAGConfig:
    """RAG配置"""
    vector_db_path: str = field(
        default_factory=lambda: get_env_or_config("RAG_VECTOR_DB_PATH", "rag.vector_db_path", "data/vector_db")
    )
    collection_name: str = field(
        default_factory=lambda: get_env_or_config("RAG_COLLECTION_NAME", "rag.collection_name", "aiops-assistant")
    )
    knowledge_base_path: str = field(
        default_factory=lambda: get_env_or_config("RAG_KNOWLEDGE_BASE_PATH", "rag.knowledge_base_path", "data/knowledge_base")
    )
    chunk_size: int = field(
        default_factory=lambda: get_env_or_config("RAG_CHUNK_SIZE", "rag.chunk_size", 1000, int)
    )
    chunk_overlap: int = field(
        default_factory=lambda: get_env_or_config("RAG_CHUNK_OVERLAP", "rag.chunk_overlap", 200, int)
    )
    top_k: int = field(
        default_factory=lambda: get_env_or_config("RAG_TOP_K", "rag.top_k", 4, int)
    )
    similarity_threshold: float = field(
        default_factory=lambda: get_env_or_config("RAG_SIMILARITY_THRESHOLD", "rag.similarity_threshold", 0.7, float)
    )
    max_context_length: int = field(
        default_factory=lambda: get_env_or_config("RAG_MAX_CONTEXT_LENGTH", "rag.max_context_length", 4000, int)
    )
    temperature: float = field(
        default_factory=lambda: get_env_or_config("RAG_TEMPERATURE", "rag.temperature", 0.1, float)
    )
    cache_expiry: int = field(
        default_factory=lambda: get_env_or_config("RAG_CACHE_EXPIRY", "rag.cache_expiry", 3600, int)
    )


@dataclass
class TavilyConfig:
    """Tavily搜索配置"""
    api_key: str = field(
        default_factory=lambda: get_env_or_config("TAVILY_API_KEY", "tavily.api_key", "")
    )
    max_results: int = field(
        default_factory=lambda: get_env_or_config("TAVILY_MAX_RESULTS", "tavily.max_results", 5, int)
    )


@dataclass
class MCPConfig:
    """MCP配置"""
    server_url: str = field(
        default_factory=lambda: get_env_or_config("MCP_SERVER_URL", "mcp.server_url", "http://127.0.0.1:9000")
    )
    timeout: int = field(
        default_factory=lambda: get_env_or_config("MCP_TIMEOUT", "mcp.timeout", 120, int)
    )
    max_retries: int = field(
        default_factory=lambda: get_env_or_config("MCP_MAX_RETRIES", "mcp.max_retries", 3, int)
    )


@dataclass
class AppConfig:
    """应用配置"""
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
    
    # 子配置
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    k8s: K8sConfig = field(default_factory=K8sConfig)
    rca: RCAConfig = field(default_factory=RCAConfig)
    prediction: PredictionConfig = field(default_factory=PredictionConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    rag: RAGConfig = field(default_factory=RAGConfig)
    tavily: TavilyConfig = field(default_factory=TavilyConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    logs: LogsConfig = field(default_factory=LogsConfig)
    tracing: TracingConfig = field(default_factory=TracingConfig)
    remediation: RemediationConfig = field(default_factory=RemediationConfig)


# 全局配置实例
config = AppConfig()