#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 缓存管理（redis_cache_manager）
"""

import gzip
import hashlib
import json
import logging
import pickle
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import redis
from redis.connection import ConnectionPool

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """缓存条目数据结构"""

    timestamp: float
    data: Dict[str, Any]
    access_count: int = 0
    last_access: float = field(default_factory=time.time)
    ttl: int = 3600  # 默认1小时过期

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
            ttl=data.get("ttl", 3600),
        )


class RedisCacheManager:
    """优化的Redis缓存管理器"""

    def __init__(
        self,
        redis_config: Dict[str, Any] = None,
        cache_prefix: str = "aiops_cache:",
        default_ttl: int = 3600,
        max_cache_size: int = 10000,
        enable_compression: bool = True,
    ):
        """
        初始化Redis缓存管理器
        Args:
            redis_config: Redis连接配置（如未提供则自动从 settings.py 读取）
            cache_prefix: 缓存键前缀
            default_ttl: 默认过期时间（秒）
            max_cache_size: 最大缓存条目数
            enable_compression: 是否启用数据压缩
        """
        self.redis_config = redis_config or {}
        self.cache_prefix = cache_prefix
        self.default_ttl = default_ttl
        self.max_cache_size = max_cache_size
        self.enable_compression = enable_compression
        self._lock = threading.Lock()
        self._shutdown = False

        # Redis连接池，所有参数都从settings读取
        self.connection_pool = ConnectionPool(
            host=self.redis_config.get("host", "127.0.0.1"),
            port=self.redis_config.get("port", 6379),
            db=self.redis_config.get("db", 0),
            password=self.redis_config.get("password"),
            decode_responses=False,  # 强制二进制，避免压缩/pickle混淆
            max_connections=self.redis_config.get("max_connections", 10),
            socket_timeout=self.redis_config.get("socket_timeout", 5),
            socket_connect_timeout=self.redis_config.get("connection_timeout", 5),
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
            logger.error(f"Redis缓存管理器初始化失败: {e}")
            raise RuntimeError(f"无法连接到Redis缓存: {e}") from e

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
        """生成缓存键"""
        cache_input = question

        if session_id and history:
            # 只使用最近的历史记录
            recent_history = history[-2:] if len(history) >= 2 else history
            if recent_history:
                history_str = json.dumps(
                    [
                        {
                            "role": h.get("role", ""),
                            "content": h.get("content", "")[:50],
                        }
                        for h in recent_history
                    ],
                    ensure_ascii=False,
                )
                cache_input = f"{question}|{history_str}"

        key_hash = hashlib.sha256(cache_input.encode("utf-8")).hexdigest()
        return f"{self.cache_prefix}{key_hash}"

    def _serialize_data(self, data: Any) -> bytes:
        """序列化数据"""
        try:
            # 使用pickle序列化
            serialized = pickle.dumps(data)

            # 可选压缩
            if self.enable_compression and len(serialized) > 1024:  # 大于1KB才压缩
                serialized = gzip.compress(serialized)
                return b"compressed:" + serialized

            return b"raw:" + serialized

        except Exception as e:
            logger.error(f"数据序列化失败: {e}")
            raise

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
            logger.error(f"数据反序列化失败: {e}")
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
            logger.warning(f"更新统计信息失败: {e}")

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
                logger.warning(f"更新缓存访问信息失败: {e}")

            self._update_stats("hit")
            logger.debug(f"缓存命中: {cache_key[:16]}...")
            return entry.data

        except Exception as e:
            logger.error(f"获取缓存失败: {e}")
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

            logger.debug(f"设置缓存: {cache_key[:16]}...")

        except Exception as e:
            logger.error(f"设置缓存失败: {e}")

    def _cleanup_if_needed(self):
        """根据需要清理缓存"""
        try:
            # 获取当前缓存条目数
            current_size = self.redis_client.scard(self.index_key)

            if current_size > self.max_cache_size:
                # 获取所有缓存键
                cache_keys = self.redis_client.smembers(self.index_key)
                # decode_responses=False 下 smembers/keys 返回 bytes
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
            logger.error(f"缓存清理失败: {e}")

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
            logger.error(f"清空缓存失败: {e}")
            return {
                "success": False,
                "message": f"清空缓存失败: {e}",
                "cleared_count": 0,
            }

    def clear(self) -> Dict[str, Any]:
        """兼容方法：清空缓存（别名）。

        设计意图：
        - 对外暴露更直观的 `clear()` 接口，内部复用 `clear_all()`，避免上层调用方找不到方法。
        """
        return self.clear_all()

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        try:
            stats = self.redis_client.hgetall(self.stats_key)
            # hgetall 在 decode_responses=False 时返回 {bytes: bytes}
            parsed = {}
            for k, v in stats.items():
                key_str = k.decode() if isinstance(k, bytes) else str(k)
                try:
                    parsed[key_str] = int(
                        v.decode() if isinstance(v, bytes) else int(v)
                    )
                except Exception:
                    try:
                        parsed[key_str] = int(v)
                    except Exception:
                        parsed[key_str] = 0

            # 计算命中率
            total_requests = stats.get("total_requests", 0)
            cache_hits = stats.get("cache_hits", 0)
            hit_rate = (cache_hits / total_requests * 100) if total_requests > 0 else 0

            # 当前缓存条目数
            current_entries = self.redis_client.scard(self.index_key)

            return {
                **parsed,
                "hit_rate": round(hit_rate, 2),
                "current_entries": current_entries,
                "max_cache_size": self.max_cache_size,
                "default_ttl": self.default_ttl,
            }

        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
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
            logger.error(f"缓存健康检查失败: {e}")
            return {"status": "unhealthy", "error": str(e), "redis_connected": False}

    def shutdown(self, *, silent: bool = False):
        """关闭缓存管理器

        说明：
        - 测试/进程退出阶段，logging 可能已关闭，避免在析构期间写日志导致 I/O on closed file。
        - 通过 `silent=True` 抑制日志输出，仅执行资源释放。
        """
        self._shutdown = True
        try:
            self.connection_pool.disconnect()
            if not silent:
                try:
                    logger.info("Redis缓存管理器已关闭")
                except Exception:
                    # 进程退出阶段可能触发 logging 关闭，静默处理
                    pass
        except Exception as e:
            if not silent:
                try:
                    logger.warning(f"关闭Redis缓存连接时出错: {e}")
                except Exception:
                    pass

    def __del__(self):
        """清理资源"""
        if not self._shutdown:
            try:
                # 在析构阶段静默释放，避免日志系统已关闭时写日志
                self.shutdown(silent=True)
            except Exception:
                pass
