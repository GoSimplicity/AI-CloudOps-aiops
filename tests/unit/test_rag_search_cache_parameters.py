"""真实搜索入口必须按完整配置快照、实例及预嵌入向量隔离缓存。"""

import asyncio
from dataclasses import fields, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from langchain_core.documents import Document
import numpy as np
import pytest

from app.core.vector import redis_vector_store as vector


class MemoryCache:
    def __init__(self, values=None):
        self.values = {} if values is None else values
        self.reads = 0
        self.writes = 0

    def get(self, key):
        self.reads += 1
        return self.values.get(key)

    def set(self, key, value, _ttl):
        self.writes += 1
        self.values[key] = value


class VectorPipeline:
    def __init__(self, vectors):
        self.vectors = vectors
        self.keys = []

    def get(self, key):
        self.keys.append(key)
        return self

    def execute(self):
        return [self.vectors[key] for key in self.keys]


def make_store(*, cache=None, query_vector=None, semantic=None):
    store = object.__new__(vector.EnhancedRedisVectorStore)
    store._closed = False
    store._search_cache_scope = uuid.uuid4().hex
    store.stats = {"search_count": 0, "cache_hits": 0, "error_count": 0}
    store.get_revision = AsyncMock(return_value="fixture-generation:1")
    store.query_cache = cache or MemoryCache()
    store.vector_dim = 2
    store.embedding_model = SimpleNamespace(embed_query=Mock(return_value=query_vector if query_vector is not None else [1.0, 0.0]))
    store._semantic_search = AsyncMock(return_value=semantic or [(Document(page_content="candidate"), 0.4)])
    store._lexical_search = AsyncMock(return_value=[])
    store._physical_collection = lambda: "fixture-generation"
    return store


async def test_threshold_change_does_not_reuse_less_restrictive_results():
    store = make_store()
    low = vector.SearchConfig(semantic_weight=1.0, lexical_weight=0.0, similarity_threshold=0.1, use_mmr=False)
    high = replace(low, similarity_threshold=0.9)
    assert len(await store.similarity_search("same", 1, low)) == 1
    # The real fusion threshold is max(threshold * .5, .1): .4 < .45.
    assert await store.similarity_search("same", 1, high) == []
    assert store.embedding_model.embed_query.call_count == 2
    assert store._semantic_search.await_count == 2


async def test_mmr_switch_and_lambda_use_real_reranker_and_distinct_cache_keys():
    docs = [Document(page_content=name) for name in ("first", "similar", "diverse")]
    store = make_store(semantic=list(zip(docs, [0.9, 0.8, 0.7])))
    vectors = {}
    for doc, value in zip(docs, ([1, 0], [0.99, 0.1], [0, 1])):
        vectors[f"fixture-generation:vec:{store._generate_doc_id(doc.page_content)}"] = np.array(value, dtype=np.float32).tobytes()
    store.client = SimpleNamespace(pipeline=lambda: VectorPipeline(vectors))
    ordinary = vector.SearchConfig(semantic_weight=1.0, lexical_weight=0.0, use_mmr=False)
    diversity = replace(ordinary, use_mmr=True, mmr_lambda=0.0)
    relevance = replace(diversity, mmr_lambda=1.0)
    def names(results):
        return [doc.page_content for doc, _ in results]
    assert names(await store.similarity_search("same", 2, ordinary)) == ["first", "similar"]
    assert names(await store.similarity_search("same", 2, diversity)) == ["first", "diverse"]
    assert names(await store.similarity_search("same", 2, relevance)) == ["first", "similar"]
    assert names(await store.similarity_search("same", 2, diversity)) == ["first", "diverse"]
    assert store.embedding_model.embed_query.call_count == 3
    assert store.query_cache.writes == 3


async def test_omitted_and_explicit_default_configuration_share_same_instance_cache():
    store = make_store()
    first = await store.similarity_search("same", 1)
    second = await store.similarity_search("same", 1, vector.SearchConfig())
    assert first == second
    assert store.embedding_model.embed_query.call_count == 1
    assert store.stats["cache_hits"] == 1


async def test_two_stores_with_shared_redis_cache_do_not_share_model_dependent_results():
    shared = {}
    first = make_store(cache=MemoryCache(shared), query_vector=[1, 0])
    second = make_store(cache=MemoryCache(shared), query_vector=[0, 1])
    first._semantic_search = AsyncMock(return_value=[(Document(page_content="first model result"), 0.9)])
    second._semantic_search = AsyncMock(return_value=[(Document(page_content="second model result"), 0.9)])
    assert (await first.similarity_search("same", 1))[0][0].page_content == "first model result"
    assert (await second.similarity_search("same", 1))[0][0].page_content == "second model result"
    await second.similarity_search("same", 1)
    assert first.embedding_model.embed_query.call_count == second.embedding_model.embed_query.call_count == 1
    assert len(shared) == 2


async def test_configuration_snapshot_is_fixed_before_first_await():
    store = make_store()
    entered, release = asyncio.Event(), asyncio.Event()

    async def revision():
        entered.set()
        await release.wait()
        return "fixture-generation:1"

    store.get_revision = revision
    original = vector.SearchConfig(semantic_weight=1.0, lexical_weight=0.0, similarity_threshold=0.1, use_mmr=False)
    task = asyncio.create_task(store.similarity_search("same", 1, original))
    await entered.wait()
    original.similarity_threshold = 0.9
    release.set()
    assert len(await task) == 1
    assert await store.similarity_search("same", 1, original) == []


async def test_preembedded_search_binds_actual_vector_and_keeps_same_vector_cache_hit():
    store = make_store()
    async def semantic(embedding, _k):
        return [(Document(page_content="horizontal" if embedding[0] == 1 else "vertical"), 0.9)]

    store._semantic_search_with_embedding = AsyncMock(side_effect=semantic)
    config = vector.SearchConfig(use_mmr=False)
    first = await store._single_search_with_embedding("same", np.array([1, 0], dtype=np.float32), 1, config)
    second = await store._single_search_with_embedding("same", np.array([0, 1], dtype=np.float32), 1, config)
    third = await store._single_search_with_embedding("same", np.array([1, 0], dtype=np.float32), 1, config)
    assert first[0][0].page_content == third[0][0].page_content == "horizontal"
    assert second[0][0].page_content == "vertical"
    assert store._semantic_search_with_embedding.await_count == 2
    assert store.query_cache.writes == 2


@pytest.mark.parametrize("change", [
    lambda config: setattr(config, "similarity_threshold", float("nan")),
    lambda config: setattr(config, "mmr_lambda", float("inf")),
    lambda config: setattr(config, "extra_candidate_rule", "unknown"),
])
def test_unprovable_configuration_contract_disables_cache(change):
    store = make_store()
    config = vector.SearchConfig()
    change(config)
    assert store._get_search_cache_key("same", 1, config, "fixture-generation:1") is None


async def test_unknown_configuration_subclass_preserves_search_but_never_reads_or_writes_cache():
    class UnknownConfiguration(vector.SearchConfig):
        pass

    store = make_store()
    config = UnknownConfiguration(use_mmr=False)
    await store.similarity_search("same", 1, config)
    await store.similarity_search("same", 1, config)
    assert store.embedding_model.embed_query.call_count == 2
    assert store.query_cache.reads == store.query_cache.writes == 0


def test_new_key_contract_cannot_hit_legacy_md5_and_does_not_include_raw_query():
    store = make_store()
    key = store._get_search_cache_key("private query canary", 1, vector.SearchConfig(), "fixture-generation:1")
    assert key and key.startswith("search-config-v2:")
    assert "private query canary" not in key


@pytest.mark.parametrize("field", fields(vector.SearchConfig), ids=lambda field: field.name)
def test_every_declared_configuration_parameter_is_bound(field):
    store = make_store()
    original = vector.SearchConfig()
    changed = replace(original)
    value = getattr(changed, field.name)
    replacement = not value if type(value) is bool else value + 1
    setattr(changed, field.name, replacement)
    assert store._get_search_cache_key("same", 1, original) != store._get_search_cache_key("same", 1, changed)


def test_integer_and_float_weights_share_normalized_contract():
    store = make_store()
    integer = vector.SearchConfig(semantic_weight=1, lexical_weight=0)
    floating = vector.SearchConfig(semantic_weight=1.0, lexical_weight=0.0)
    assert store._get_search_cache_key("same", 1, integer) == store._get_search_cache_key("same", 1, floating)


async def test_preembedded_query_is_snapshotted_before_first_await():
    store = make_store()
    entered, release = asyncio.Event(), asyncio.Event()

    async def revision():
        entered.set()
        await release.wait()
        return "fixture-generation:1"

    async def semantic(embedding, _k):
        return [(Document(page_content="horizontal" if embedding[0] == 1 else "vertical"), 0.9)]

    store.get_revision = revision
    store._semantic_search_with_embedding = AsyncMock(side_effect=semantic)
    original = np.array([1, 0], dtype=np.float32)
    config = vector.SearchConfig(use_mmr=False)
    task = asyncio.create_task(store._single_search_with_embedding("same", original, 1, config))
    await entered.wait()
    original[:] = [0, 1]
    release.set()
    assert (await task)[0][0].page_content == "horizontal"
    assert (await store._single_search_with_embedding("same", original, 1, config))[0][0].page_content == "vertical"


async def test_extra_configuration_fields_are_not_discarded_to_enable_cache():
    store = make_store()
    config = vector.SearchConfig(use_mmr=False)
    config.unknown_candidate_rule = "unsupported"
    await store.similarity_search("same", 1, config)
    await store.similarity_search("same", 1, config)
    assert store.embedding_model.embed_query.call_count == 2
    assert store.query_cache.reads == store.query_cache.writes == 0


async def test_missing_preembedding_cannot_hit_ordinary_query_cache():
    store = make_store()
    config = vector.SearchConfig(use_mmr=False)
    assert await store.similarity_search("same", 1, config)
    before_reads = store.query_cache.reads
    store._standard_search_with_embedding = AsyncMock(return_value=[])
    assert await store._single_search_with_embedding("same", None, 1, config) == []
    store._standard_search_with_embedding.assert_awaited_once()
    assert store.query_cache.reads == before_reads
