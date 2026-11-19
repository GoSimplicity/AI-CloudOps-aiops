"""旧 provenance-v2 预测缓存不能再被当成新的模式和量化结果。

命中会经 RedisCacheManager 用 setex 续期。测试用内存客户端，不连接 Redis。
"""

import hashlib
import threading

import pytest

from app.common.identity import INTERNAL_PRINCIPAL
from app.core.cache.redis_cache_manager import RedisCacheManager
from app.services.prediction_service import PredictionService

OLD_CONTRACT = "provenance-v2"


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.sets = {}
        self.setex_calls = []

    def setex(self, key, ttl, value):
        self.values[key] = value
        self.setex_calls.append((key, ttl))

    def get(self, key):
        return self.values.get(key)

    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    def scard(self, key):
        return len(self.sets.get(key, ()))

    def smembers(self, key):
        return set(self.sets.get(key, ()))

    def srem(self, key, member):
        self.sets.get(key, set()).discard(member)

    def delete(self, key):
        self.values.pop(key, None)

    def pipeline(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def hincrby(self, *args):
        return self

    def hset(self, *args, **kwargs):
        return 1

    def execute(self):
        return []


def legacy_prediction_key(prediction_type="qps", current_value=12.5, hours=24):
    """按升级前的 provenance-v2 参数拼出当时会写入的预测缓存键。"""
    params = {
        "type": prediction_type,
        "data_contract": "observed-hourly-v1",
        "time_contract": "utc-v1",
        "prediction_contract": OLD_CONTRACT,
        "principal": INTERNAL_PRINCIPAL,
        "current": round(current_value, 2),
        "query": "default",
        "hours": hours,
        "granularity": "hour",
        "ai": False,
    }
    payload = "|".join(f"{key}:{value}" for key, value in sorted(params.items()))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"prediction:{prediction_type}:{digest}"


def cache_manager():
    client = MemoryRedis()
    manager = RedisCacheManager.__new__(RedisCacheManager)
    manager.redis_client = client
    manager._shutdown = False
    manager.cache_prefix = "predict_cache:"
    manager.default_ttl = 3600
    manager.max_cache_size = 5000
    manager.enable_compression = False
    manager.index_key = "predict_cache:index"
    manager.stats_key = "predict_cache:stats"
    manager._lock = threading.Lock()
    return manager, client


@pytest.mark.asyncio
async def test_old_provenance_v2_cache_is_not_hit_and_ttl_is_not_refreshed():
    legacy_key = legacy_prediction_key()
    stale = {
        "pattern_analysis": {
            "has_periodicity": True,
            "seasonality": {"daily_pattern": True, "weekly_pattern": False, "monthly_pattern": False},
        },
        "quantitative_metrics": {"trend_analysis": {"direction": "increasing", "change_percentage": 0}},
        "stale_measured_pattern": True,
    }
    manager, client = cache_manager()
    manager.set(question=legacy_key, response_data=stale, ttl=3600)
    writes_before = len(client.setex_calls)

    service = PredictionService()
    service._initialized = True
    service._cache_manager = manager
    current_key = service._generate_prediction_cache_key(
        prediction_type="qps",
        current_value=12.5,
        metric_query=None,
        prediction_hours=24,
        granularity="hour",
        ai_enhanced=False,
    )
    found = await service._get_from_cache(current_key)

    assert found is None
    assert current_key != legacy_key
    assert len(client.setex_calls) == writes_before
    assert client.get(client.setex_calls[0][0]) is not None
