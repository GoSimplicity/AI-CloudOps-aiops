"""发布前检查真实协议；Redis边界为纯内存替身。"""

from io import BytesIO
import pickle
import struct

import numpy as np
import pytest

from app.core.vector.redis_vector_store import EnhancedRedisVectorStore
from app.core.vector.stage_admission import StageAdmissionError, _metadata, inspect_stage

GEN = "knowledge-refresh-" + "a" * 32
ID = "b" * 32


class Pipeline:
    def __init__(self, client):
        self.client = client
        self.operations = []

    def hgetall(self, key):
        self.operations.append(lambda: self.client.data[key])

    def get(self, key):
        self.operations.append(lambda: self.client.get(key))

    def sscan(self, key, *, cursor, count):
        self.operations.append(lambda: (0, self.client.data[key]))

    def execute(self):
        return [operation() for operation in self.operations]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class MemoryRedis:
    def __init__(self):
        self.data = {
            f"{GEN}:doc:{ID}": {b"content": b"safe", b"metadata": pickle.dumps({"tags": ["safe"], "nested": {"count": 1}})},
            f"{GEN}:vec:{ID}": np.array([1, 0], dtype=np.float32).tobytes(),
            f"{GEN}:term:safe": {ID.encode()},
            f"{GEN}:revision": b"1",
        }
        self.reads = 0
        self.pages = None

    def get(self, key):
        return self.data.get(key)

    def scan(self, cursor, **_kwargs):
        self.reads += 1
        return self.pages[cursor] if self.pages is not None else (0, list(self.data))

    def pipeline(self):
        return Pipeline(self)


def inspect(client, **overrides):
    return inspect_stage(
        client, GEN, 2, max_physical_keys=overrides.get("physical", 100),
        max_snapshot_keys=overrides.get("snapshot", 100), max_vectors=overrides.get("vectors", 100),
        timeout=30, decode_revision=EnhancedRedisVectorStore._decode_revision,
    )


def test_complete_stage_is_checked_without_mutating_its_values():
    client = MemoryRedis()
    before = pickle.dumps(client.data)
    assert inspect(client) == b"1"
    assert pickle.dumps(client.data) == before


def test_empty_stage_without_revision_is_valid_for_empty_publication():
    client = MemoryRedis()
    client.data.clear()
    assert inspect(client) is None


def test_physical_budget_includes_revision_and_duplicate_scan_keys_count_once():
    client = MemoryRedis()
    keys = list(client.data)
    client.pages = {0: (1, keys), 1: (0, keys)}
    assert inspect(client, physical=4, snapshot=3) == b"1"
    with pytest.raises(StageAdmissionError, match="物理键预算"):
        inspect(client, physical=3)


@pytest.mark.parametrize("kind", ["orphan-doc", "orphan-vector", "legacy-idx", "unexpected", "nan", "dimension", "utf8", "metadata", "missing-field", "orphan-term"])
def test_incomplete_or_unreadable_stage_never_passes(kind):
    client = MemoryRedis()
    if kind == "orphan-doc":
        del client.data[f"{GEN}:vec:{ID}"]
    elif kind == "orphan-vector":
        del client.data[f"{GEN}:doc:{ID}"]
    elif kind == "legacy-idx":
        client.data[f"{GEN}:idx:0"] = ID.encode()
    elif kind == "unexpected":
        client.data[f"{GEN}:unknown"] = b"unknown"
    elif kind == "nan":
        client.data[f"{GEN}:vec:{ID}"] = np.array([float("nan"), 0], dtype=np.float32).tobytes()
    elif kind == "dimension":
        client.data[f"{GEN}:vec:{ID}"] = np.array([1], dtype=np.float32).tobytes()
    elif kind == "utf8":
        client.data[f"{GEN}:doc:{ID}"][b"content"] = b"\xffprivate-content-canary"
    elif kind == "metadata":
        client.data[f"{GEN}:doc:{ID}"][b"metadata"] = b"invalid-private-pickle-canary"
    elif kind == "missing-field":
        del client.data[f"{GEN}:doc:{ID}"][b"metadata"]
    else:
        client.data[f"{GEN}:term:safe"] = {b"c" * 32}
    with pytest.raises(StageAdmissionError) as error:
        inspect(client)
    assert "private-" not in str(error.value)


def test_read_and_snapshot_limits_are_independently_enforced():
    with pytest.raises(StageAdmissionError, match="读取预算"):
        inspect(MemoryRedis(), vectors=0)
    with pytest.raises(StageAdmissionError, match="快照预算"):
        inspect(MemoryRedis(), snapshot=2)


def test_revision_change_during_scan_is_not_treated_as_stable():
    client = MemoryRedis()
    scan = client.scan

    def changed(*args, **kwargs):
        result = scan(*args, **kwargs)
        client.data[f"{GEN}:revision"] = b"2"
        return result

    client.scan = changed
    with pytest.raises(StageAdmissionError, match="检查期间发生变化"):
        inspect(client)


_called = False


def _side_effect():
    global _called
    _called = True
    return {}


class ExecutableMetadata:
    def __reduce__(self):
        return _side_effect, ()


def test_metadata_validation_never_loads_or_executes_classes():
    global _called
    _called = False
    with pytest.raises(StageAdmissionError, match="不支持的类"):
        _metadata(pickle.dumps(ExecutableMetadata()))
    assert _called is False


@pytest.mark.parametrize("value", [[], {"bad": float("nan")}, {"bad": BytesIO()}, {"big": "x" * (1024 * 1024)}])
def test_metadata_requires_bounded_primitive_dictionary(value):
    with pytest.raises(StageAdmissionError):
        _metadata(pickle.dumps(value))


def test_cyclic_or_trailing_metadata_is_rejected():
    cyclic = {}
    cyclic["cycle"] = cyclic
    with pytest.raises(StageAdmissionError, match="结构"):
        _metadata(pickle.dumps(cyclic))
    with pytest.raises(StageAdmissionError, match="合同"):
        _metadata(pickle.dumps({}) + b"trailing")


def test_duplicate_pages_cannot_bypass_the_deadline(monkeypatch):
    import app.core.vector.stage_admission as admission

    tick = 0

    def clock():
        nonlocal tick
        tick += 1
        return float(tick)

    monkeypatch.setattr(admission.time, "monotonic", clock)
    client = MemoryRedis()
    client.pages = {0: (1, [f"{GEN}:revision"]), 1: (1, [f"{GEN}:revision"])}
    with pytest.raises(StageAdmissionError, match="超时"):
        inspect(client)
    assert client.reads < 30


@pytest.mark.parametrize("payload", [b"\x80\x02\x82\x01.", b"\x80\x02Pexternal\n.", b"\x80\x05\x97."])
def test_extension_persistent_and_out_of_band_opcodes_rejected_before_unpickling(payload):
    with pytest.raises(StageAdmissionError):
        _metadata(payload)


def test_metadata_surrogate_string_is_not_treated_as_valid_utf8():
    with pytest.raises(StageAdmissionError, match="UTF8"):
        _metadata(pickle.dumps({"bad": "\udcff"}))


@pytest.mark.parametrize("payload", [
    b"\x80\x04}r" + struct.pack("<I", 100_000) + b".",
    b"\x80\x04}r" + struct.pack("<I", 2**31 - 1) + b".",
    b"}p-1\n.",
    b"}p100000\n.",
    b"\x80\x04h\x00.",
    b"g0\n.",
    b"\x80\x04}q\x01h\x00.",
    b"\x80\x04}r" + struct.pack("<I", 99) + b"j" + struct.pack("<I", 100) + b".",
    b"\x80\x04}" + b"\x94" * 100_001 + b".",
    b"\x80\x04}" + b"q\x00" * 100_001 + b".",
])
def test_memo_limits_reject_before_constructing_any_unpickler(monkeypatch, payload):
    import app.core.vector.stage_admission as admission

    constructed = False

    def forbidden(*_args, **_kwargs):
        nonlocal constructed
        constructed = True
        raise AssertionError("unsafe payload reached the C unpickler")

    monkeypatch.setattr(admission, "_PrimitiveUnpickler", forbidden)
    with pytest.raises(StageAdmissionError, match="memo"):
        _metadata(payload)
    assert constructed is False


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_normal_metadata_aliases_remain_decodable_across_supported_protocols(protocol):
    shared = [{"tag": "safe"}]
    _metadata(pickle.dumps({"first": shared, "second": shared}, protocol=protocol))


def test_small_sparse_memo_index_is_bounded_and_still_decodable():
    _metadata(b"\x80\x04}r" + struct.pack("<I", 99) + b".")
    _metadata(b"\x80\x04}r" + struct.pack("<I", 99) + b"\x94h\x01.")


async def test_actual_markdown_write_metadata_passes_memo_limits():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.core.vector import redis_vector_store as vector

    if not vector.MD_PROCESSOR_AVAILABLE:
        pytest.skip("installed Markdown processor required")
    stage = object.__new__(vector.EnhancedRedisVectorStore)
    stage._closed = False
    stage._is_refresh_stage = True
    stage._md_processor_initialized = False
    stage.md_processor = None
    stage._initialize_md_processors()
    checked = []

    async def embed(documents):
        return [np.array([1, 0], dtype=np.float32) for _ in documents]

    async def persist(documents, _embeddings):
        for document in documents:
            _metadata(pickle.dumps(document.metadata))
            checked.append(document.metadata)
        return [stage._generate_doc_id(document.page_content) for document in documents]

    stage._batch_embed = embed
    stage._parallel_store = persist
    stage.index_manager = SimpleNamespace(update_index=AsyncMock())
    stage.query_cache = SimpleNamespace(invalidate_pattern=AsyncMock())
    stage.hierarchical_retriever = None
    try:
        result = await stage.add_md_documents(["# Owned fixture\n\nA valid document.\n\n- item"], [{"source": "owned.md", "tags": ["safe"]}])
        assert result
        assert checked
    finally:
        if stage.md_processor._executor:
            stage.md_processor._executor.shutdown(wait=True)
        stage._closed = True
