#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 应用程序配置管理模块
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

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
            print(f"警告: 未找到配置文件，将使用环境变量默认值")
            return {}
    except Exception as e:
        print(f"加载配置文件出错: {e}")
        return {}


_config_data = load_config()


def get_env_or_config(env_key: str, config_key: str, default: Any = None, cast_type=str):
    """从环境变量或配置文件中获取值"""
    # 优先从环境变量获取
    env_value = os.getenv(env_key)
    if env_value is not None:
        try:
            return cast_type(env_value)
        except (ValueError, TypeError):
            pass
    
    # 从配置文件获取
    keys = config_key.split('.')
    value = _config_data
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return default
    
    try:
        return cast_type(value) if value is not None else default
    except (ValueError, TypeError):
        return default


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
    connection_timeout: int = field(
        default_factory=lambda: get_env_or_config("REDIS_CONNECTION_TIMEOUT", "redis.connection_timeout", 5, int)
    )
    max_connections: int = field(
        default_factory=lambda: get_env_or_config("REDIS_MAX_CONNECTIONS", "redis.max_connections", 10, int)
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
    rag: RAGConfig = field(default_factory=RAGConfig)
    tavily: TavilyConfig = field(default_factory=TavilyConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)


# 全局配置实例
config = AppConfig()