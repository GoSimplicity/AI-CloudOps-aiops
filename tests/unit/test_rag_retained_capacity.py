"""SCAN重复页不应被当成新增物理键；真实超限与限时保护保持生效。"""

from types import SimpleNamespace

import pytest

from app.core.vector import redis_vector_store as vector


def store_with_pages(pages):
    scans = []

    def scan(cursor, *, match, count):
        scans.append((cursor, match, count))
        return pages[cursor]

    store = object.__new__(vector.EnhancedRedisVectorStore)
    store._closed = True
    store.collection_name = "knowledge"
    store.client = SimpleNamespace(scan=scan)
    return store, scans


def keys(count):
    return [f"knowledge-refresh-{'a' * 32}:doc:{index}".encode() for index in range(count)]


def test_repeated_scan_pages_count_each_real_key_once():
    page = keys(60_000)
    store, scans = store_with_pages({0: (1, page), 1: (0, page)})
    store._check_retained_capacity()
    assert len(scans) == 2
    assert all(match == "knowledge-refresh-*" for _, match, _ in scans)


def test_real_unique_overflow_stops_scanning_at_bound(monkeypatch):
    monkeypatch.setattr(vector, "MAX_RETAINED_KEYS", 3)
    page = keys(4)
    store, scans = store_with_pages({0: (1, page[:3]), 1: (2, page), 2: (0, [])})
    with pytest.raises(RuntimeError, match="超过键数量上限"):
        store._check_retained_capacity()
    assert len(scans) == 2


def test_exact_unique_limit_is_allowed_despite_duplicate_keys(monkeypatch):
    monkeypatch.setattr(vector, "MAX_RETAINED_KEYS", 3)
    page = keys(3)
    store, scans = store_with_pages({0: (1, page), 1: (0, page * 3)})
    store._check_retained_capacity()
    assert len(scans) == 2


def test_infinite_duplicate_pages_remain_bounded_by_existing_deadline(monkeypatch):
    clock = iter([0.0, 0.0, 0.0, 31.0])
    monkeypatch.setattr(vector.time, "monotonic", lambda: next(clock))
    page = keys(1)
    store, scans = store_with_pages({0: (0, [])})
    store.client.scan = lambda cursor, **kwargs: (1, page)
    with pytest.raises(TimeoutError, match="容量检查超时"):
        store._check_retained_capacity()


def test_generation_count_limit_still_rejects_distinct_generations(monkeypatch):
    monkeypatch.setattr(vector, "MAX_RETAINED_GENERATIONS", 2)
    first, second = f"knowledge-refresh-{'a' * 32}:doc:1", f"knowledge-refresh-{'b' * 32}:doc:1"
    store, _ = store_with_pages({0: (0, [first.encode(), second.encode()])})
    with pytest.raises(RuntimeError, match="超过数量上限"):
        store._check_retained_capacity()
