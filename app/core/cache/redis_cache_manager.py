#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Redis缓存管理器
"""

from dataclasses import dataclass, field
import gzip
import hashlib
import logging
import pickle
import threading
import time
from typing import Any, Dict, List, Optional

import redis
from redis.connection import ConnectionPool

logger = logging.getLogger(__name__)

# 缓存配置常量
DEFAULT_TTL = 3600  # 默认1小时过期
COMPRESSION_THRESHOLD = 1024  # 大于1KB才压缩
DEBUG_KEY_LENGTH = 16  # 调试时显示的key长度
SCAN_COUNT = 100  # Redis scan操作每次获取的数量
CONTENT_PREVIEW_LENGTH = 50  # 内容预览长度


@dataclass
class CacheEntry:
    """缓存条目数据结构"""

    timestamp: float
    data: Dict[str, Any]
    access_count: int = 0
    last_access: float = field(default_factory=time.time)
    ttl: int = DEFAULT_TTL

    def is_expired(self, expiry_seconds: int = None) -> bool:
        """检查是否过期"""
        expiry = expiry_seconds or self.ttl
        return time.time() - self.timestamp > expiry

    def update_access(self):
        """更新访问信息"""
        self.access_count += 1
        self.last_access = time.time()

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "timestamp": self.timestamp,
            "data": self.data,
            "access_count": self.access_count,
            "last_access": self.last_access,
            "ttl": self.ttl,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CacheEntry":
        """从字典创建"""
        return cls(
            timestamp=data.get("timestamp", time.time()),
            data=data.get("data", {}),
            access_count=data.get("access_count", 0),
            last_access=data.get("last_access", time.time()),
            ttl=data.get("ttl", DEFAULT_TTL),
        )


class RedisCacheManager:
    """优化的Redis缓存管理器"""

    def __init__(
        self,
        redis_config: Dict[str, Any],
        cache_prefix: str = "aiops_cache:",
        default_ttl: int = DEFAULT_TTL,
        max_cache_size: int = 10000,
        enable_compression: bool = True,
    ):
        """
        初始化Redis缓存管理器

        Args:
            redis_config: Redis连接配置
            cache_prefix: 缓存键前缀
            default_ttl: 默认过期时间（秒）
            max_cache_size: 最大缓存条目数
            enable_compression: 是否启用数据压缩
        """
        self.redis_config = redis_config
        self.cache_prefix = cache_prefix
        self.default_ttl = default_ttl
        self.max_cache_size = max_cache_size
        self.enable_compression = enable_compression
        self._lock = threading.Lock()
        self._shutdown = False

        from app.config.settings import config

        # Redis连接池
        self.connection_pool = ConnectionPool(
            host=redis_config.get("host", config.redis.host),
            port=redis_config.get("port", config.redis.port),
            db=redis_config.get("db", config.redis.db + 1),  # 使用不同的db用于缓存
            password=redis_config.get("password", ""),
            decode_responses=False,  # 处理二进制数据
            max_connections=redis_config.get("max_connections", 20),
            socket_timeout=redis_config.get("socket_timeout", 5),
            socket_connect_timeout=redis_config.get("connection_timeout", 5),
        )

        self.redis_client = redis.Redis(connection_pool=self.connection_pool)

        # 统计信息键
        self.stats_key = f"{self.cache_prefix}stats"
        self.index_key = f"{self.cache_prefix}index"

        # 初始化
        self._initialize()

    def _initialize(self):
        """初始化缓存系统"""
        try:
            # 测试Redis连接
            self.redis_client.ping()
            logger.info("Redis缓存管理器初始化成功")

            # 初始化统计信息
            if not self.redis_client.exists(self.stats_key):
                self._reset_stats()

        except Exception as e:
            logger.error("Redis缓存管理器初始化失败，异常类型: %s", type(e).__name__)
            raise RuntimeError("无法连接到Redis缓存") from e

    def _reset_stats(self):
        """重置统计信息"""
        stats = {
            "total_requests": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "total_entries": 0,
            "expired_entries": 0,
            "evicted_entries": 0,
        }
        self.redis_client.hset(self.stats_key, mapping=stats)

    def _generate_cache_key(
        self, question: str, session_id: str = None, history: List = None
    ) -> str:
        """生成智能缓存键 - 支持相似问题匹配"""
        # 标准化问题文本
        normalized_question = self._normalize_question(question)

        # 提取关键词
        keywords = self._extract_keywords(normalized_question)

        # 生成基础缓存输入
        cache_input = "|".join(sorted(keywords))  # 排序确保一致性

        # 如果有会话上下文，添加简化的上下文信息
        if session_id and history and len(history) > 0:
            # 只使用最后一个问答对的关键信息
            last_interaction = history[-1] if history else ""
            if isinstance(last_interaction, str) and len(last_interaction) > 0:
                last_keywords = self._extract_keywords(last_interaction)[
                    :3
                ]  # 只取前3个关键词
                if last_keywords:
                    context_key = "|".join(sorted(last_keywords))
                    cache_input = f"{cache_input}|ctx:{context_key}"

        # 生成缓存键
        key_hash = hashlib.sha256(cache_input.encode("utf-8")).hexdigest()
        return f"{self.cache_prefix}smart:{key_hash}"

    def _normalize_question(self, question: str) -> str:
        """标准化问题文本"""
        import re

        # 转换为小写
        normalized = question.lower().strip()

        # 移除多余的标点符号和空格
        normalized = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", normalized)  # 保留中文字符

        # 合并多个空格为单个空格
        normalized = re.sub(r"\s+", " ", normalized)

        return normalized.strip()

    def _extract_keywords(self, text: str, max_keywords: int = 8) -> List[str]:
        """提取关键词"""
        if not text:
            return []

        # 简单的关键词提取：移除停用词并按长度过滤
        stop_words = {
            "的",
            "了",
            "是",
            "在",
            "有",
            "和",
            "与",
            "或",
            "但",
            "不",
            "没",
            "也",
            "都",
            "要",
            "会",
            "能",
            "可以",
            "什么",
            "怎么",
            "为什么",
            "如何",
            "the",
            "is",
            "in",
            "and",
            "or",
            "but",
            "not",
            "to",
            "a",
            "an",
            "what",
            "how",
            "why",
            "can",
            "could",
            "should",
            "would",
        }

        words = text.split()
        keywords = []

        for word in words:
            # 过滤条件：长度>1，不在停用词中
            if len(word) > 1 and word not in stop_words:
                keywords.append(word)

        # 按词频排序（简单实现）或取最长的词
        keywords = list(set(keywords))  # 去重
        keywords.sort(key=len, reverse=True)  # 按长度排序，长词通常更有意义

        return keywords[:max_keywords]

    def _serialize_data(self, data: Any) -> bytes:
        """序列化数据"""
        try:
            # 使用pickle序列化
            serialized = pickle.dumps(data)

            # 可选压缩
            if self.enable_compression and len(serialized) > COMPRESSION_THRESHOLD:
                serialized = gzip.compress(serialized)
                return b"compressed:" + serialized

            return b"raw:" + serialized

        except Exception as e:
            logger.error("数据序列化失败，异常类型: %s", type(e).__name__)
            raise RuntimeError("数据序列化失败") from e

    def _deserialize_data(self, data: bytes) -> Any:
        """反序列化数据"""
        try:
            if data.startswith(b"compressed:"):
                # 解压缩
                compressed_data = data[11:]  # 去掉前缀
                decompressed = gzip.decompress(compressed_data)
                return pickle.loads(decompressed)
            elif data.startswith(b"raw:"):
                # 直接反序列化
                raw_data = data[4:]  # 去掉前缀
                return pickle.loads(raw_data)
            else:
                # 兼容旧格式
                return pickle.loads(data)

        except Exception as e:
            logger.error("数据反序列化失败，异常类型: %s", type(e).__name__)
            return None

    def _update_stats(self, operation: str):
        """更新统计信息"""
        try:
            with self.redis_client.pipeline() as pipe:
                pipe.hincrby(self.stats_key, "total_requests", 1)
                if operation in ["hit", "miss", "expired", "evicted"]:
                    pipe.hincrby(self.stats_key, f"cache_{operation}s", 1)
                pipe.execute()
        except Exception as e:
            logger.warning("更新统计信息失败，异常类型: %s", type(e).__name__)

    def get(
        self, question: str, session_id: str = None, history: List = None
    ) -> Optional[Dict[str, Any]]:
        """获取缓存"""
        if self._shutdown:
            return None

        try:
            self._update_stats("request")
            cache_key = self._generate_cache_key(question, session_id, history)

            # 从Redis获取数据
            cached_data = self.redis_client.get(cache_key)

            if cached_data is None:
                self._update_stats("miss")
                return None

            # 反序列化数据
            entry_dict = self._deserialize_data(cached_data)
            if entry_dict is None:
                self._update_stats("miss")
                return None

            entry = CacheEntry.from_dict(entry_dict)

            # 检查是否过期
            if entry.is_expired(self.default_ttl):
                self.redis_client.delete(cache_key)
                self.redis_client.srem(self.index_key, cache_key)
                self._update_stats("expired")
                return None

            # 更新访问信息
            entry.update_access()

            # 异步更新到Redis（不阻塞返回）
            try:
                serialized = self._serialize_data(entry.to_dict())
                self.redis_client.setex(cache_key, self.default_ttl, serialized)
            except Exception as e:
                logger.warning("更新缓存访问信息失败，异常类型: %s", type(e).__name__)

            self._update_stats("hit")
            logger.debug(f"缓存命中: {cache_key[:DEBUG_KEY_LENGTH]}...")
            return entry.data

        except Exception as e:
            logger.error("获取缓存失败，异常类型: %s", type(e).__name__)
            return None

    def set(
        self,
        question: str,
        response_data: Dict[str, Any],
        session_id: str = None,
        history: List = None,
        ttl: int = None,
    ):
        """设置缓存"""
        if self._shutdown:
            return

        try:
            cache_key = self._generate_cache_key(question, session_id, history)
            cache_ttl = ttl or self.default_ttl

            # 创建缓存条目
            entry = CacheEntry(timestamp=time.time(), data=response_data, ttl=cache_ttl)

            # 序列化数据
            serialized = self._serialize_data(entry.to_dict())

            # 存储到Redis
            self.redis_client.setex(cache_key, cache_ttl, serialized)

            # 添加到索引
            self.redis_client.sadd(self.index_key, cache_key)

            # 检查缓存大小并清理
            self._cleanup_if_needed()

            logger.debug(f"设置缓存: {cache_key[:DEBUG_KEY_LENGTH]}...")

        except Exception as e:
            logger.error("设置缓存失败，异常类型: %s", type(e).__name__)

    def _cleanup_if_needed(self):
        """根据需要清理缓存"""
        try:
            # 获取当前缓存条目数
            current_size = self.redis_client.scard(self.index_key)

            if current_size > self.max_cache_size:
                # 获取所有缓存键
                cache_keys = self.redis_client.smembers(self.index_key)
                cache_keys = [
                    key.decode() if isinstance(key, bytes) else key
                    for key in cache_keys
                ]

                # 获取每个缓存的访问统计
                entries_with_stats = []

                for cache_key in cache_keys:
                    try:
                        cached_data = self.redis_client.get(cache_key)
                        if cached_data:
                            entry_dict = self._deserialize_data(cached_data)
                            if entry_dict:
                                entry = CacheEntry.from_dict(entry_dict)
                                entries_with_stats.append(
                                    (cache_key, entry.access_count, entry.last_access)
                                )
                    except Exception:
                        # 无效缓存，标记为删除
                        entries_with_stats.append((cache_key, 0, 0))

                # 按LRU算法排序（访问次数 + 最后访问时间）
                entries_with_stats.sort(key=lambda x: (x[1], x[2]))

                # 删除最少使用的缓存
                to_remove = current_size - self.max_cache_size + 100  # 额外删除一些
                for i in range(min(to_remove, len(entries_with_stats))):
                    cache_key = entries_with_stats[i][0]
                    self.redis_client.delete(cache_key)
                    self.redis_client.srem(self.index_key, cache_key)
                    self._update_stats("evicted")

                logger.info(f"清理了 {to_remove} 个缓存条目")

        except Exception as e:
            logger.error("缓存清理失败，异常类型: %s", type(e).__name__)

    def clear_pattern(self, pattern: str) -> Dict[str, Any]:
        """根据模式清除缓存"""
        try:
            # 获取匹配模式的所有键
            cursor = 0
            deleted_count = 0

            while True:
                cursor, keys = self.redis_client.scan(cursor, pattern, count=SCAN_COUNT)
                if keys:
                    # 批量删除
                    self.redis_client.delete(*keys)
                    deleted_count += len(keys)

                    # 从索引中移除
                    for key in keys:
                        self.redis_client.srem(self.index_key, key)

                if cursor == 0:
                    break

            logger.info(f"根据模式 '{pattern}' 清除了 {deleted_count} 个缓存项")

            return {
                "success": True,
                "message": f"根据模式清除了 {deleted_count} 个缓存项",
                "cleared_count": deleted_count,
                "pattern": pattern,
            }

        except Exception as e:
            logger.error("根据模式清除缓存失败，异常类型: %s，模式: %s", type(e).__name__, pattern)
            return {
                "success": False,
                "message": "根据模式清除缓存失败",
                "cleared_count": 0,
                "pattern": pattern,
            }

    def clear_all(self) -> Dict[str, Any]:
        """清空所有缓存"""
        try:
            # 获取所有缓存键
            cache_keys = self.redis_client.smembers(self.index_key)
            cache_count = len(cache_keys)

            # 删除所有缓存
            if cache_keys:
                # 转换为字符串
                cache_keys = [
                    key.decode() if isinstance(key, bytes) else key
                    for key in cache_keys
                ]
                self.redis_client.delete(*cache_keys)

            # 清空索引
            self.redis_client.delete(self.index_key)

            # 重置统计
            self._reset_stats()

            logger.info(f"已清空所有缓存，共 {cache_count} 条")

            return {
                "success": True,
                "message": f"已清空 {cache_count} 条缓存",
                "cleared_count": cache_count,
            }

        except Exception as e:
            logger.error("清空缓存失败，异常类型: %s", type(e).__name__)
            return {
                "success": False,
                "message": "清空缓存失败",
                "cleared_count": 0,
            }

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        try:
            stats = self.redis_client.hgetall(self.stats_key)
            stats = {k.decode(): int(v.decode()) for k, v in stats.items()}

            # 计算命中率
            total_requests = stats.get("total_requests", 0)
            cache_hits = stats.get("cache_hits", 0)
            hit_rate = (cache_hits / total_requests * 100) if total_requests > 0 else 0

            # 当前缓存条目数
            current_entries = self.redis_client.scard(self.index_key)

            return {
                **stats,
                "hit_rate": round(hit_rate, 2),
                "current_entries": current_entries,
                "max_cache_size": self.max_cache_size,
                "default_ttl": self.default_ttl,
            }

        except Exception as e:
            logger.error("获取统计信息失败，异常类型: %s", type(e).__name__)
            return {}

    def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        try:
            # 测试Redis连接
            ping_result = self.redis_client.ping()

            # 获取统计信息
            stats = self.get_stats()

            return {
                "status": "healthy" if ping_result else "unhealthy",
                "redis_connected": ping_result,
                "stats": stats,
            }

        except Exception as e:
            logger.error("缓存健康检查失败，异常类型: %s", type(e).__name__)
            return {"status": "unhealthy", "error": "缓存健康检查失败", "redis_connected": False}

    def get_embedding_cache(self, text: str) -> Optional[List[float]]:
        """获取嵌入向量缓存"""
        if self._shutdown:
            return None

        try:
            # 生成嵌入向量缓存键
            embedding_key = self._generate_embedding_key(text)

            # 从Redis获取缓存的嵌入向量
            cached_embedding = self.redis_client.get(embedding_key)

            if cached_embedding is None:
                return None

            # 反序列化嵌入向量
            embedding_data = self._deserialize_data(cached_embedding)
            if embedding_data is None:
                return None

            # 检查是否过期
            if embedding_data.get("timestamp"):
                if time.time() - embedding_data["timestamp"] > 86400:  # 24小时过期
                    self.redis_client.delete(embedding_key)
                    return None

            logger.debug(f"嵌入向量缓存命中: {embedding_key[:16]}...")
            return embedding_data.get("embedding")

        except Exception as e:
            logger.error("获取嵌入向量缓存失败，异常类型: %s", type(e).__name__)
            return None

    def set_embedding_cache(self, text: str, embedding: List[float], ttl: int = 86400):
        """设置嵌入向量缓存"""
        if self._shutdown:
            return

        try:
            # 生成嵌入向量缓存键
            embedding_key = self._generate_embedding_key(text)

            # 创建缓存数据
            cache_data = {
                "embedding": embedding,
                "timestamp": time.time(),
                "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            }

            # 序列化数据
            serialized = self._serialize_data(cache_data)

            # 存储到Redis
            self.redis_client.setex(embedding_key, ttl, serialized)

            logger.debug(f"嵌入向量缓存设置: {embedding_key[:16]}...")

        except Exception as e:
            logger.error("设置嵌入向量缓存失败，异常类型: %s", type(e).__name__)

    def _generate_embedding_key(self, text: str) -> str:
        """生成嵌入向量缓存键"""
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"{self.cache_prefix}embedding:{text_hash}"

    def clear_embedding_cache(self) -> Dict[str, Any]:
        """清除所有嵌入向量缓存"""
        try:
            pattern = f"{self.cache_prefix}embedding:*"
            return self.clear_pattern(pattern)
        except Exception as e:
            logger.error("清除嵌入向量缓存失败，异常类型: %s", type(e).__name__)
            return {"success": False, "message": "清除失败", "cleared_count": 0}

    def shutdown(self):
        """关闭缓存管理器"""
        self._shutdown = True
        try:
            self.connection_pool.disconnect()
            logger.info("Redis缓存管理器已关闭")
        except Exception as e:
            logger.warning("关闭Redis缓存连接时出错，异常类型: %s", type(e).__name__)

    def __del__(self):
        """清理资源"""
        if not self._shutdown:
            try:
                self.shutdown()
            except Exception:
                pass
