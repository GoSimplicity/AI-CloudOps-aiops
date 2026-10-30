"""发布前只读核验暂存代；检查失败不改变活动版本或未知状态。"""

import io
import math
import pickle
import pickletools
import re
import time

import numpy as np


class StageAdmissionError(RuntimeError):
    """暂存代不满足当前读取和复制合同。"""


_MAX_METADATA_NODES = 100_000

_PRIMITIVE_OPCODES = frozenset({
    "PROTO", "FRAME", "STOP", "MARK", "POP", "POP_MARK", "DUP", "NONE", "NEWTRUE", "NEWFALSE",
    "INT", "BININT", "BININT1", "BININT2", "LONG", "LONG1", "LONG4", "FLOAT", "BINFLOAT",
    "STRING", "BINSTRING", "SHORT_BINSTRING", "UNICODE", "BINUNICODE", "SHORT_BINUNICODE", "BINUNICODE8",
    "BINBYTES", "SHORT_BINBYTES", "BINBYTES8", "EMPTY_LIST", "APPEND", "APPENDS", "LIST",
    "EMPTY_TUPLE", "TUPLE", "TUPLE1", "TUPLE2", "TUPLE3", "EMPTY_DICT", "DICT", "SETITEM", "SETITEMS",
    "EMPTY_SET", "ADDITEMS", "FROZENSET", "BINPUT", "LONG_BINPUT", "PUT", "BINGET", "LONG_BINGET", "GET", "MEMOIZE",
})


class _PrimitiveUnpickler(pickle.Unpickler):
    def find_class(self, _module, _name):
        raise StageAdmissionError("知识元数据包含不支持的类")

    def persistent_load(self, _pid):
        raise StageAdmissionError("知识元数据包含外部引用")


def _metadata(raw):
    if not isinstance(raw, bytes) or len(raw) > 1024 * 1024:
        raise StageAdmissionError("知识元数据大小或格式无效")
    # 先拒绝一切类/调用/外部buffer操作码。EXT缓存可绕过find_class，
    # 因此不能只依赖Unpickler的类查找钩子。
    stream = io.BytesIO(raw)
    memo = set()
    memo_writes = 0
    try:
        for opcode, argument, _position in pickletools.genops(raw):
            name = opcode.name
            if name not in _PRIMITIVE_OPCODES:
                raise StageAdmissionError("知识元数据包含不支持的类或操作")
            if name in {"BINPUT", "LONG_BINPUT", "PUT", "MEMOIZE"}:
                index = len(memo) if name == "MEMOIZE" else argument
                memo_writes += 1
                if type(index) is not int or not 0 <= index < _MAX_METADATA_NODES or memo_writes > _MAX_METADATA_NODES:
                    raise StageAdmissionError("知识元数据memo索引或条目数超过上限")
                memo.add(index)
            elif name in {"BINGET", "LONG_BINGET", "GET"}:
                if type(argument) is not int or not 0 <= argument < _MAX_METADATA_NODES or argument not in memo:
                    raise StageAdmissionError("知识元数据memo引用无效")
        value = _PrimitiveUnpickler(stream).load()
    except StageAdmissionError:
        raise
    except Exception as error:
        raise StageAdmissionError("知识元数据无法安全解码") from error
    if stream.read(1) or type(value) is not dict or any(type(key) is not str for key in value):
        raise StageAdmissionError("知识元数据合同无效")
    remaining = _MAX_METADATA_NODES
    active = set()

    def check(item, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 64:
            raise StageAdmissionError("知识元数据结构超过上限")
        if type(item) is str:
            try:
                item.encode("utf-8")
            except UnicodeError as error:
                raise StageAdmissionError("知识元数据字符串UTF8无效") from error
            return
        if type(item) in (type(None), bytes, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) not in (dict, list, tuple, set, frozenset) or id(item) in active:
            raise StageAdmissionError("知识元数据包含不支持的结构")
        active.add(id(item))
        try:
            values = (child for pair in item.items() for child in pair) if type(item) is dict else item
            for child in values:
                check(child, depth + 1)
        finally:
            active.remove(id(item))

    check(value, 0)


def inspect_stage(client, generation, vector_dim, *, max_physical_keys, max_snapshot_keys, max_vectors, timeout, decode_revision):
    """同一revision下完整核验。调用方须持lease，随后原子freeze此revision。"""
    deadline = time.monotonic() + timeout

    def within_deadline():
        if time.monotonic() >= deadline:
            raise StageAdmissionError("知识版本发布前检查超时")

    def text(value):
        try:
            return value.decode("utf-8") if isinstance(value, bytes) else str(value)
        except UnicodeError as error:
            raise StageAdmissionError("知识版本键编码无效") from error

    revision_key = f"{generation}:revision"
    revision = client.get(revision_key)
    decode_revision(revision)
    seen, docs, vectors, terms = set(), {}, {}, []
    snapshot_keys = 0
    cursor = 0
    prefix = generation + ":"
    while True:
        within_deadline()
        cursor, keys = client.scan(cursor, match=prefix + "*", count=500)
        for key in keys:
            within_deadline()
            name = text(key)
            if name in seen:
                continue
            seen.add(name)
            if len(seen) > max_physical_keys:
                raise StageAdmissionError("暂存知识版本超过物理键预算")
            if not name.startswith(prefix):
                raise StageAdmissionError("暂存知识版本键身份无效")
            suffix = name[len(prefix):]
            if suffix == "revision":
                continue
            snapshot_keys += 1
            if snapshot_keys > max_snapshot_keys:
                raise StageAdmissionError("暂存知识版本超过快照预算")
            if suffix.startswith("doc:") or suffix.startswith("vec:"):
                kind, identifier = suffix.split(":", 1)
                if not re.fullmatch(r"[0-9a-f]{32}", identifier):
                    raise StageAdmissionError("知识文档标识无效")
                target = docs if kind == "doc" else vectors
                target[identifier] = key
                if len(target) > max_vectors:
                    raise StageAdmissionError("暂存知识版本超过读取预算")
            elif suffix.startswith("term:") and suffix[5:]:
                terms.append(key)
            else:
                # 当前writer无持久idx格式；clone也跳过旧idx，不猜旧位置映射。
                raise StageAdmissionError("暂存知识版本包含不支持的索引或键")
        if cursor == 0:
            break
    if docs.keys() != vectors.keys():
        raise StageAdmissionError("知识文档与向量不完整配对")
    if type(vector_dim) is not int or vector_dim <= 0:
        raise StageAdmissionError("知识向量维度无效")
    identifiers = list(docs)
    for offset in range(0, len(identifiers), 16):
        within_deadline()
        batch = identifiers[offset:offset + 16]
        with client.pipeline() as pipe:
            for identifier in batch:
                pipe.hgetall(docs[identifier])
                pipe.get(vectors[identifier])
            values = pipe.execute()
        if len(values) != len(batch) * 2:
            raise StageAdmissionError("知识版本读取不完整")
        for index, identifier in enumerate(batch):
            within_deadline()
            document, raw_vector = values[2 * index:2 * index + 2]
            if type(document) is not dict or b"content" not in document or b"metadata" not in document:
                raise StageAdmissionError("知识文档字段不完整")
            content = document[b"content"]
            if not isinstance(content, bytes) or len(content) > 10 * 1024 * 1024:
                raise StageAdmissionError("知识文档大小或格式无效")
            try:
                content.decode("utf-8")
            except UnicodeError as error:
                raise StageAdmissionError("知识文档UTF8无效") from error
            _metadata(document[b"metadata"])
            if not isinstance(raw_vector, bytes) or len(raw_vector) != vector_dim * 4:
                raise StageAdmissionError("知识向量维度不匹配")
            if not np.all(np.isfinite(np.frombuffer(raw_vector, dtype=np.float32))):
                raise StageAdmissionError("知识向量包含非有限值")
    for offset in range(0, len(terms), 100):
        pending = [(key, 0) for key in terms[offset:offset + 100]]
        while pending:
            within_deadline()
            with client.pipeline() as pipe:
                for key, term_cursor in pending:
                    pipe.sscan(key, cursor=term_cursor, count=200)
                pages = pipe.execute()
            if len(pages) != len(pending):
                raise StageAdmissionError("知识词条读取不完整")
            following = []
            for (key, _), (term_cursor, members) in zip(pending, pages):
                if any(text(member) not in docs for member in members):
                    raise StageAdmissionError("知识词条引用不存在的文档")
                if term_cursor != 0:
                    following.append((key, term_cursor))
            pending = following
    within_deadline()
    if client.get(revision_key) != revision:
        raise StageAdmissionError("知识暂存版本在检查期间发生变化")
    return revision
