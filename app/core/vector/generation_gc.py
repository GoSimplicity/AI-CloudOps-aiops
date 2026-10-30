"""Redis 服务端协调知识版本租约和回收。

调用方必须在每次物理版本写入的 Redis 事务中 WATCH lease.tombstone_keys，
提交前和任何读取结果返回前调用 lease.check()。发布前先 protect_unknown，
仅在确认 CAS 成功后 confirm_published；结果未知的版本绝不自动删除。
"""
import asyncio
import re
import uuid


class GenerationLeaseLost(RuntimeError):
    """租约失效后禁止继续读取、写入或发布"""


class GenerationConflict(RuntimeError):
    """活动知识版本已发生并发变化"""


class GenerationResolutionOutcomeUnknown(RuntimeError):
    """人工处置可能已提交；必须核对状态，不能盲目重试"""


# 全部租约与回收判定使用 Redis 服务器时间，不依赖客户端系统时钟
_ACQUIRE_ACTIVE = """
local gen = redis.call('HGET', KEYS[1], 'active') or ARGV[1]
if redis.call('EXISTS', ARGV[2] .. gen) ~= 0 then return {err='generation unavailable'} end
local now = redis.call('TIME'); local ms = now[1] * 1000 + math.floor(now[2] / 1000)
local key=ARGV[3] .. gen
redis.call('ZREMRANGEBYSCORE', key, '-inf', ms)
if redis.call('ZCARD', key) >= 4096 then return {err='generation lease limit reached'} end
redis.call('ZADD', key, ms + tonumber(ARGV[4]), ARGV[5])
local last=redis.call('ZREVRANGE', key, 0, 0, 'WITHSCORES')
redis.call('PEXPIREAT', key, math.ceil(tonumber(last[2])) + 1000)
return gen
"""
_ACQUIRE_STAGE = """
local source = redis.call('HGET', KEYS[1], 'active') or ARGV[1]
if redis.call('EXISTS', ARGV[2] .. source) ~= 0 or redis.call('EXISTS', ARGV[2] .. ARGV[3]) ~= 0 then return {err='generation unavailable'} end
if redis.call('SISMEMBER', KEYS[2], ARGV[3]) ~= 0 then return {err='stage already exists'} end
if redis.call('SCARD', KEYS[2]) >= 64 then return {err='generation limit reached'} end
local revision = redis.call('GET', source .. ':revision')
local now = redis.call('TIME'); local ms = now[1] * 1000 + math.floor(now[2] / 1000)
local source_leases=ARGV[5] .. source
redis.call('ZREMRANGEBYSCORE', source_leases, '-inf', ms)
if redis.call('ZCARD', source_leases) >= 4096 then return {err='generation lease limit reached'} end
redis.call('SADD', KEYS[2], ARGV[3]); redis.call('HSET', KEYS[1], 'state:' .. ARGV[3], 'building')
for _,key in ipairs({source_leases,ARGV[5] .. ARGV[3]}) do
  redis.call('ZADD', key, ms + tonumber(ARGV[6]), ARGV[7])
  local last=redis.call('ZREVRANGE', key, 0, 0, 'WITHSCORES')
  redis.call('PEXPIREAT', key, math.ceil(tonumber(last[2])) + 1000)
end
return {source, revision or false}
"""
_RENEW_OR_CHECK = """
local now = redis.call('TIME'); local ms = now[1] * 1000 + math.floor(now[2] / 1000)
for i=1,#KEYS do
  local score = redis.call('ZSCORE', KEYS[i], ARGV[1])
  if not score or tonumber(score) <= ms or redis.call('EXISTS', ARGV[2] .. ARGV[i+3]) ~= 0 then return 0 end
end
if ARGV[3] ~= '0' then
  for i=1,#KEYS do
    redis.call('ZADD', KEYS[i], ms + tonumber(ARGV[3]), ARGV[1])
    local last=redis.call('ZREVRANGE', KEYS[i], 0, 0, 'WITHSCORES')
    redis.call('PEXPIREAT', KEYS[i], math.ceil(tonumber(last[2])) + 1000)
  end
end
return 1
"""
_CAPTURE_REVISION = object()
_MARK_UNKNOWN = """
local now = redis.call('TIME'); local ms = now[1] * 1000 + math.floor(now[2] / 1000)
for i=1,#KEYS-2 do
  local score=redis.call('ZSCORE', KEYS[i], ARGV[1])
  if not score or tonumber(score) <= ms or redis.call('EXISTS', ARGV[2] .. ARGV[i+2]) ~= 0 then return 0 end
end
local gen=ARGV[#KEYS]
local state_key=KEYS[#KEYS-1]
if redis.call('HGET', state_key, 'state:' .. gen) ~= 'building' then return 0 end
local revision=redis.call('GET', KEYS[#KEYS])
local mode=ARGV[#KEYS+1]
if (mode == '0' and revision) or (mode == '1' and revision ~= ARGV[#KEYS+2]) then return -1 end
redis.call('HSET', state_key, 'state:' .. gen, 'unknown',
 'sealed_present:' .. gen, revision and '1' or '0', 'sealed_revision:' .. gen, revision or '')
return 1
"""

_CONFIRM = """
local state=redis.call('HGET', KEYS[1], 'state:' .. ARGV[1])
if state == 'published' then return 1 end
if state ~= 'unknown' or redis.call('HGET', KEYS[1], 'committed:' .. ARGV[1]) ~= '1' then return 0 end
redis.call('HSET', KEYS[1], 'state:' .. ARGV[1], 'published'); return 1
"""
_PUBLISH = """
local now=redis.call('TIME'); local ms=now[1]*1000+math.floor(now[2]/1000)
for i=1,2 do
  local score=redis.call('ZSCORE', KEYS[i], ARGV[1])
  if not score or tonumber(score) <= ms or redis.call('EXISTS', KEYS[i+2]) ~= 0 then return -1 end
end
local active=redis.call('HGET', KEYS[5], 'active') or ARGV[2]
local rev=redis.call('GET', KEYS[6])
if active ~= ARGV[3] or (ARGV[5] == '0' and rev) or (ARGV[5] == '1' and rev ~= ARGV[6]) then return -2 end
if redis.call('HGET', KEYS[5], 'state:' .. ARGV[4]) ~= 'unknown' then return -3 end
local sealed=redis.call('HGET', KEYS[5], 'sealed_present:' .. ARGV[4])
local sealed_revision=redis.call('HGET', KEYS[5], 'sealed_revision:' .. ARGV[4])
local target_revision=redis.call('GET', KEYS[8])
if (sealed ~= '0' and sealed ~= '1') or not sealed_revision or
 (sealed == '0' and (target_revision or sealed_revision ~= '')) or
 (sealed == '1' and (not target_revision or target_revision ~= sealed_revision)) then return -2 end
redis.call('HSET', KEYS[5], 'active', ARGV[4], 'committed:' .. ARGV[4], '1'); return 1
"""
_ABORT = """
if (redis.call('HGET', KEYS[1], 'active') or ARGV[1]) == ARGV[2] then return 0 end
local state=redis.call('HGET', KEYS[1], 'state:' .. ARGV[2])
if state ~= 'building' and not (state == 'unknown' and ARGV[3] == '1') then return 0 end
if redis.call('HGET', KEYS[1], 'committed:' .. ARGV[2]) == '1' then return 0 end
redis.call('HSET', KEYS[1], 'state:' .. ARGV[2], 'aborted'); return 1
"""
_RESOLVE = """
local generation=ARGV[1]; local action=ARGV[2]
local state=redis.call('HGET', KEYS[1], 'state:' .. generation)
if redis.call('SISMEMBER', KEYS[2], generation) ~= 1 or state ~= 'unknown' then return -1 end
local committed=redis.call('HGET', KEYS[1], 'committed:' .. generation) == '1'
local result
if action == 'acknowledge' then
  if not committed then return -2 end
  result='published'
elseif action == 'abandon' then
  if committed or (redis.call('HGET', KEYS[1], 'active') or ARGV[6]) == generation then return -2 end
  local now=redis.call('TIME'); local ms=now[1]*1000+math.floor(now[2]/1000)
  if redis.call('ZCOUNT', KEYS[3], '(' .. ms, '+inf') ~= 0 then return -3 end
  result='aborted'
else return -2 end
local count=0
for _,field in ipairs(redis.call('HKEYS', KEYS[1])) do
  if string.sub(field, 1, 11) == 'resolution:' then count=count+1 end
end
if count >= 10000 then return -4 end
local now=redis.call('TIME')
local audit=cjson.encode({generation=generation, action=action, operator=ARGV[3], reason=ARGV[4], timestamp=now[1]})
-- 一次HSET同时写入人工处置和审计
redis.call('HSET', KEYS[1], 'state:' .. generation, result, 'resolution:' .. ARGV[5], audit)
return 1
"""
_CAPACITY = """
local active=redis.call('HGET', KEYS[1], 'active') or ARGV[1]
local generations=redis.call('SMEMBERS', KEYS[2])
local unknown, committed, building, published, aborted=0,0,0,0,0
for _,gen in ipairs(generations) do
  local state=redis.call('HGET', KEYS[1], 'state:' .. gen)
  if state == 'unknown' then
    if redis.call('HGET', KEYS[1], 'committed:' .. gen) == '1' then committed=committed+1 else unknown=unknown+1 end
  elseif state == 'building' then building=building+1
  elseif state == 'published' then published=published+1
  elseif state == 'aborted' then aborted=aborted+1 end
end
local audits=0
for _,field in ipairs(redis.call('HKEYS', KEYS[1])) do
  if string.sub(field, 1, 11) == 'resolution:' then audits=audits+1 end
end
return {active,#generations,unknown,committed,building,published,aborted,redis.call('SCARD', KEYS[3]),audits}
"""
_INSPECT = """
local active=redis.call('HGET', KEYS[1], 'active') or ARGV[1]
local now=redis.call('TIME'); local ms=now[1]*1000+math.floor(now[2]/1000)
local result={}
for _,gen in ipairs(redis.call('SMEMBERS', KEYS[2])) do
  if redis.call('HGET', KEYS[1], 'state:' .. gen) == 'unknown' then
    table.insert(result, {gen, redis.call('HGET', KEYS[1], 'committed:' .. gen) or '0', gen == active and 1 or 0, redis.call('ZCOUNT', ARGV[2] .. gen, '(' .. ms, '+inf')})
  end
end
return result
"""
_INSPECT_ONE = """
local gen=ARGV[1]
local state=redis.call('HGET', KEYS[1], 'state:' .. gen) or ''
local committed=redis.call('HGET', KEYS[1], 'committed:' .. gen) or '0'
local active=(redis.call('HGET', KEYS[1], 'active') or ARGV[2]) == gen and 1 or 0
local now=redis.call('TIME'); local ms=now[1]*1000+math.floor(now[2]/1000)
local leases=redis.call('ZCOUNT', KEYS[2], '(' .. ms, '+inf')
local entries=redis.call('HGETALL', KEYS[1]); local audit={}
for i=1,#entries,2 do
  if string.sub(entries[i], 1, 11) == 'resolution:' then
    local ok, item=pcall(cjson.decode, entries[i+1])
    if not ok then return {err='audit record invalid'} end
    if item.generation == gen then table.insert(audit, {item.action or '', item.operator or '', item.timestamp or ''}) end
  end
end
return {state,committed,active,leases,audit}
"""

_ARCHIVE = """
local fields={}
for i=1,#ARGV,2 do
  local current=redis.call('HGET', KEYS[1], ARGV[i])
  if current and current ~= ARGV[i+1] then return -1 end
  if current then table.insert(fields, ARGV[i]) end
end
if #fields == 0 then return 0 end
return redis.call('HDEL', KEYS[1], unpack(fields))
"""
_CLAIM = """
if (redis.call('HGET', KEYS[1], 'active') or ARGV[1]) == ARGV[2] then return 0 end
if redis.call('SISMEMBER', KEYS[2], ARGV[2]) == 0 then return 0 end
local state=redis.call('HGET', KEYS[1], 'state:' .. ARGV[2]); if state == 'unknown' or not state then return 0 end
local now=redis.call('TIME'); local ms=now[1]*1000+math.floor(now[2]/1000)
redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', ms)
if redis.call('ZCARD', KEYS[4]) ~= 0 then return 0 end
local expired=redis.call('ZRANGEBYSCORE', KEYS[7], '-inf', ms, 'LIMIT', 0, 1000)
for _, gen in ipairs(expired) do
  if redis.call('EXISTS', ARGV[3] .. gen) == 0 then
    redis.call('SREM', KEYS[6], gen); redis.call('ZREM', KEYS[7], gen)
  end
end
if redis.call('EXISTS', KEYS[5]) == 0 and redis.call('SCARD', KEYS[6]) >= 10000 then return -1 end
redis.call('SADD', KEYS[6], ARGV[2])
redis.call('SET', KEYS[5], '1'); return 1
"""
_FINISH = """
if (redis.call('HGET', KEYS[1], 'active') or ARGV[1]) == ARGV[2] or redis.call('HGET', KEYS[1], 'state:' .. ARGV[2]) == 'unknown' then return 0 end
local now=redis.call('TIME'); local ms=now[1]*1000+math.floor(now[2]/1000)
redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', ms)
if redis.call('ZCARD', KEYS[4]) ~= 0 or redis.call('EXISTS', KEYS[5]) == 0 then return 0 end
redis.call('SREM', KEYS[2], ARGV[2]); redis.call('HDEL', KEYS[1], 'state:' .. ARGV[2], 'committed:' .. ARGV[2], 'sealed_present:' .. ARGV[2], 'sealed_revision:' .. ARGV[2]); redis.call('DEL', KEYS[4])
redis.call('PEXPIRE', KEYS[5], ARGV[3]); redis.call('ZADD', KEYS[6], ms + tonumber(ARGV[3]), ARGV[2])
return 1
"""


class GenerationLease:
    def __init__(self, owner, generation, token, protected, source_generation=None, source_revision=None):
        self.owner = owner
        self.generation = generation
        self.token = token
        self.protected = tuple(dict.fromkeys(protected))
        self.source_generation = source_generation
        self.source_revision = source_revision
        self._lost = False
        self._closed = False
        self._close_task = None
        self._heartbeat = asyncio.create_task(self._keep_alive())

    @property
    def tombstone_keys(self):
        return tuple(self.owner.tombstone_key(generation) for generation in self.protected)

    @property
    def watch_keys(self):
        return self.tombstone_keys + tuple(self.owner.lease_key(generation) for generation in self.protected)

    async def _keep_alive(self):
        try:
            while True:
                await asyncio.sleep(self.owner.lease_ms / 3000)
                if not await self.owner._lease_valid(self, renew=True):
                    self._lost = True
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lost = True

    async def check(self):
        if self._closed or self._lost:
            raise GenerationLeaseLost("知识库版本租约已失效")
        try:
            valid = await self.owner._lease_valid(self, renew=False)
        except Exception as error:
            self._lost = True
            raise GenerationLeaseLost("无法确认知识库版本租约") from error
        if not valid:
            self._lost = True
            raise GenerationLeaseLost("知识库版本租约已失效")

    async def close(self):
        if self._close_task is None:
            self._closed = True

            async def release():
                self._heartbeat.cancel()
                await asyncio.gather(self._heartbeat, return_exceptions=True)
                results = await asyncio.gather(*(
                    asyncio.to_thread(self.owner.client.zrem, self.owner.lease_key(generation), self.token)
                    for generation in self.protected
                ), return_exceptions=True)
                if any(isinstance(result, BaseException) for result in results):
                    raise GenerationLeaseLost("无法释放部分知识库租约，将等待过期")

            self._close_task = asyncio.create_task(release())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            # 等全部后台释放操作结束；不能因重复取消留下尚在运行的连接操作
            while not self._close_task.done():
                try:
                    await asyncio.shield(self._close_task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not self._close_task.cancelled():
                self._close_task.exception()
            raise

    async def __aenter__(self):
        await self.check()
        return self

    async def __aexit__(self, *_args):
        await self.close()


class GenerationGC:
    def __init__(self, client, collection: str, *, lease_ms: int = 30000):
        if not collection or not re.fullmatch(r"[A-Za-z0-9_.:-]+", collection):
            raise ValueError("非法知识库名称")
        if lease_ms < 100 or lease_ms > 3600000:
            raise ValueError("租约时长无效")
        self.client = client
        self.collection = collection
        self.lease_ms = lease_ms
        self.active_key = f"{collection}:active_generation"
        self.registry_key = f"{collection}:gc:generations"
        self._prefix = f"{collection}:gc:"
        self._tombstones_key = self._prefix + "tombstones"
        self._tombstone_expiry_key = self._prefix + "tombstone_expiry"

    def _validate(self, generation: str):
        if generation != self.collection and not re.fullmatch(re.escape(self.collection) + r"-refresh-[0-9a-f]{32}", generation):
            raise ValueError("非法知识库版本")

    def lease_key(self, generation):
        return self._prefix + "leases:" + generation

    def tombstone_key(self, generation):
        return self._prefix + "deleting:" + generation

    def _state_key(self, generation):
        return self.active_key

    @staticmethod
    def _text(value):
        return value.decode("ascii") if isinstance(value, bytes) else str(value)

    async def acquire_active(self):
        token = uuid.uuid4().hex
        generation = self._text(await asyncio.to_thread(self.client.eval, _ACQUIRE_ACTIVE, 1, self.active_key,
            self.collection, self._prefix + "deleting:", self._prefix + "leases:", self.lease_ms, token))
        try:
            self._validate(generation)
        except Exception:
            await asyncio.to_thread(self.client.zrem, self.lease_key(generation), token)
            raise
        return GenerationLease(self, generation, token, [generation])

    async def acquire_stage(self, generation):
        self._validate(generation)
        if generation == self.collection:
            raise ValueError("暂存版本不能是旧版固定前缀")
        token = uuid.uuid4().hex
        source, revision = await asyncio.to_thread(self.client.eval, _ACQUIRE_STAGE, 2, self.active_key, self.registry_key,
            self.collection, self._prefix + "deleting:", generation, self._prefix + "state:", self._prefix + "leases:", self.lease_ms, token)
        source = self._text(source)
        try:
            self._validate(source)
        except Exception:
            await asyncio.to_thread(self.client.zrem, self.lease_key(source), token)
            await asyncio.to_thread(self.client.zrem, self.lease_key(generation), token)
            await asyncio.to_thread(self.client.srem, self.registry_key, generation)
            await asyncio.to_thread(self.client.hdel, self.active_key, "state:" + generation)
            raise
        return GenerationLease(self, generation, token, [source, generation], source, revision)

    async def _lease_valid(self, lease, *, renew):
        keys = [self.lease_key(gen) for gen in lease.protected]
        return bool(await asyncio.to_thread(self.client.eval, _RENEW_OR_CHECK, len(keys), *keys,
            lease.token, self._prefix + "deleting:", self.lease_ms if renew else 0, *lease.protected))

    async def protect_unknown(self, lease, *, target_revision=_CAPTURE_REVISION):
        if lease.owner is not self or lease.source_generation is None or lease.protected != (lease.source_generation, lease.generation):
            raise ValueError("需要当前知识库的双版本暂存租约")
        keys = [self.lease_key(gen) for gen in lease.protected] + [self._state_key(lease.generation), f"{lease.generation}:revision"]
        mode = "capture" if target_revision is _CAPTURE_REVISION else "0" if target_revision is None else "1"
        expected = "" if target_revision is _CAPTURE_REVISION or target_revision is None else target_revision
        ok = await asyncio.to_thread(self.client.eval, _MARK_UNKNOWN, len(keys), *keys,
            lease.token, self._prefix + "deleting:", *lease.protected, mode, expected)
        if ok == -1:
            raise GenerationConflict("知识暂存版本在检查后发生变化")
        if ok != 1:
            raise GenerationLeaseLost("无法保护待发布版本")

    async def confirm_published(self, lease):
        ok = await asyncio.to_thread(self.client.eval, _CONFIRM, 2, self.active_key, self._state_key(lease.generation), lease.generation)
        if not ok:
            raise RuntimeError("无法确认发布版本")

    async def publish_cas(self, lease):
        if lease.owner is not self or lease.source_generation is None or lease.protected != (lease.source_generation, lease.generation):
            raise ValueError("需要当前知识库的双版本暂存租约")
        source, target = lease.source_generation, lease.generation
        revision = lease.source_revision
        result = await asyncio.to_thread(self.client.eval, _PUBLISH, 8,
            self.lease_key(source), self.lease_key(target), self.tombstone_key(source),
            self.tombstone_key(target), self.active_key, f"{source}:revision", self._state_key(target), f"{target}:revision",
            lease.token, self.collection, source, target, "0" if revision is None else "1", revision or "")
        if result == -1:
            raise GenerationLeaseLost("知识库版本租约已失效")
        if result == -2:
            raise GenerationConflict("知识库版本已发生并发变化")
        if result != 1:
            raise RuntimeError("待发布知识库状态无效")

    async def capacity_metrics(self):
        values = await asyncio.to_thread(self.client.eval, _CAPACITY, 3, self.active_key, self.registry_key,
            self._tombstones_key, self.collection)
        active = self._text(values[0])
        self._validate(active)
        names = ("registered", "unknown_uncommitted", "unknown_committed", "building", "published", "aborted", "tombstone_registry", "resolution_audits")
        return {"active": active, **{name: int(value) for name, value in zip(names, values[1:])}}

    async def inspect_uncertain(self):
        rows = await asyncio.to_thread(self.client.eval, _INSPECT, 2, self.active_key, self.registry_key,
            self.collection, self._prefix + "leases:")
        result = []
        for generation, committed, active, live in rows:
            name = self._text(generation)
            self._validate(name)
            result.append({"generation": name, "committed": self._text(committed) == "1", "active": bool(active), "live_leases": int(live)})
        return sorted(result, key=lambda row: row["generation"])

    async def inspect_generation(self, generation):
        self._validate(generation)
        state, committed, active, leases, audits = await asyncio.to_thread(
            self.client.eval, _INSPECT_ONE, 2, self.active_key, self.lease_key(generation), generation, self.collection
        )
        return {"generation": generation, "state": self._text(state), "committed": self._text(committed) == "1",
                "active": bool(active), "live_leases": int(leases),
                "resolutions": [{"action": self._text(a), "operator": self._text(o), "time": self._text(t)} for a, o, t in audits]}

    async def resolve_uncertain(self, generation, action, operator, reason):
        import redis

        self._validate(generation)
        if generation == self.collection or action not in {"acknowledge", "abandon"}:
            raise ValueError("非法人工处置")
        if not isinstance(operator, str) or not 1 <= len(operator.strip()) <= 128 or not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise ValueError("需要有效操作者和核对原因")
        operation = asyncio.create_task(asyncio.to_thread(self.client.eval, _RESOLVE, 3, self.active_key, self.registry_key, self.lease_key(generation),
            generation, action, operator.strip(), reason.strip(), uuid.uuid4().hex, self.collection))
        try:
            result = await asyncio.shield(operation)
        except asyncio.CancelledError as error:
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not operation.cancelled():
                operation.exception()
            raise GenerationResolutionOutcomeUnknown("人工处置结果未知，请核对状态，勿自动重试") from error
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError, redis.exceptions.InvalidResponse,
                ConnectionError, TimeoutError) as error:
            raise GenerationResolutionOutcomeUnknown("人工处置结果未知，请核对状态，勿自动重试") from error
        if result == -3:
            raise GenerationLeaseLost("仍有活动租约，不能放弃版本")
        if result == -4:
            raise RuntimeError("人工处置审计容量已满")
        if result != 1:
            raise GenerationConflict("版本状态不允许此处置")

    @staticmethod
    def _audit_digest(entries):
        import hashlib
        import json

        payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _archive_receipt(directory, digest, phase, count):
        """记录本机归档操作；不包含原因、凭据或消息正文"""
        from datetime import datetime, timezone
        import fcntl
        import json
        import os
        import stat

        path = directory / "receipts.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            # 归档互斥冲突时显式失败，避免阻塞事件循环
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1:
                raise ValueError("归档审计文件权限或所有者无效")
            record = {"operator": _local_operator(), "time": datetime.now(timezone.utc).isoformat(),
                      "sha256": digest, "archive": f"{digest}.json", "phase": phase, "count": count}
            remaining = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("写入归档审计失败")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    async def export_audit(self, destination):
        """原子导出完整审计快照；目标必须不存在，文件权限为0600"""
        import json
        import os
        from pathlib import Path
        import tempfile

        target = Path(destination)
        if not target.is_absolute():
            raise ValueError("审计文件必须使用绝对路径")
        values = await asyncio.to_thread(self.client.hgetall, self.active_key)
        entries = []
        for key, value in values.items():
            key = self._text(key)
            if key.startswith("resolution:"):
                if not re.fullmatch(r"resolution:[0-9a-f]{32}", key):
                    raise RuntimeError("审计字段无效")
                text_value = value.decode("utf-8") if isinstance(value, bytes) else str(value)
                json.loads(text_value)
                entries.append({"field": key, "value": text_value})
        entries.sort(key=lambda item: item["field"])
        if len(entries) > 10000:
            raise RuntimeError("审计记录超过安全上限")
        digest = self._audit_digest(entries)
        document = {"version": 1, "collection": self.collection, "count": len(entries), "sha256": digest, "entries": entries}
        data = (json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=".cloudops-audit-", dir=target.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # hard link原子创建且不覆盖已有文件
            os.link(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.unlink(temporary)
        return {"count": len(entries), "sha256": digest}

    async def archive_audit(self, source, confirmed_sha256):
        """在工具生成的持久私有副本就绪后，按确认摘要安全移除审计"""
        import hashlib
        import hmac
        import json
        import os
        from pathlib import Path
        import stat
        import tempfile

        path = Path(source)
        if not path.is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", confirmed_sha256):
            raise ValueError("审计路径或确认摘要无效")

        def read_private(fd, target):
            info = os.fstat(fd)
            named = os.stat(target, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
                raise ValueError("审计文件权限、所有者或身份无效")
            if info.st_size > 32 * 1024 * 1024:
                raise ValueError("审计文件超过大小限制")
            os.fsync(fd)
            data = os.pread(fd, info.st_size + 1, 0)
            after = os.fstat(fd)
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(data) != info.st_size:
                raise ValueError("审计文件读取时发生变化")
            return data, after

        def check_same(fd, target, baseline, digest):
            data, current = read_private(fd, target)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
                baseline.st_dev, baseline.st_ino, baseline.st_size, baseline.st_mtime_ns, baseline.st_ctime_ns
            ) or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
                raise GenerationConflict("审计文件发生变化，停止归档")

        source_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        archive_fd = None
        try:
            data, source_stat = read_private(source_fd, path)
            document = json.loads(data)
            entries = document.get("entries")
            if document.get("version") != 1 or document.get("collection") != self.collection or not isinstance(entries, list) or len(entries) > 10000 or document.get("count") != len(entries):
                raise ValueError("审计文件合同无效")
            fields = set()
            for entry in entries:
                if (not isinstance(entry, dict) or set(entry) != {"field", "value"}
                        or not isinstance(entry["field"], str) or not re.fullmatch(r"resolution:[0-9a-f]{32}", entry["field"])
                        or not isinstance(entry["value"], str) or entry["field"] in fields):
                    raise ValueError("审计记录无效")
                fields.add(entry["field"])
                json.loads(entry["value"])
            digest = self._audit_digest(entries)
            if not hmac.compare_digest(digest, confirmed_sha256) or not hmac.compare_digest(digest, str(document.get("sha256", ""))):
                raise ValueError("审计摘要不匹配")
            data_digest = hashlib.sha256(data).hexdigest()
            archive_dir = path.parent / ".cloudops-audit-archive"
            archive_dir.mkdir(mode=0o700, exist_ok=True)
            directory_info = archive_dir.lstat()
            if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) & 0o077:
                raise ValueError("审计归档目录权限或所有者无效")
            parent_fd = os.open(path.parent, os.O_RDONLY)
            try:
                # 同时持久化源文件目录项和新建的归档目录项
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            archive_path = archive_dir / f"{digest}.json"
            temporary_fd, temporary = tempfile.mkstemp(prefix=".archive-", dir=archive_dir)
            try:
                os.fchmod(temporary_fd, 0o600)
                with os.fdopen(temporary_fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, archive_path)
                except FileExistsError:
                    pass  # 中断后重试仅能复用内容一致的已有归档
            finally:
                os.unlink(temporary)
            directory_fd = os.open(archive_dir, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            archive_fd = os.open(archive_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            archived_data, archive_stat = read_private(archive_fd, archive_path)
            if not hmac.compare_digest(hashlib.sha256(archived_data).hexdigest(), data_digest):
                raise GenerationConflict("已有归档内容不一致")
            check_same(source_fd, path, source_stat, data_digest)
            check_same(archive_fd, archive_path, archive_stat, data_digest)

            # 在任何Redis删除前持久化归档操作者与摘要；中断时保留begin以便核对续传
            self._archive_receipt(archive_dir, digest, "begin", len(entries))
            removed = 0
            for offset in range(0, len(entries), 500):
                check_same(source_fd, path, source_stat, data_digest)
                check_same(archive_fd, archive_path, archive_stat, data_digest)
                args = []
                for entry in entries[offset:offset + 500]:
                    args.extend((entry["field"], entry["value"]))
                operation = asyncio.create_task(asyncio.to_thread(self.client.eval, _ARCHIVE, 1, self.active_key, *args))
                try:
                    result = await asyncio.shield(operation)
                except asyncio.CancelledError:
                    while not operation.done():
                        try:
                            await asyncio.shield(operation)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if not operation.cancelled():
                        operation.exception()
                    raise
                if result < 0:
                    raise GenerationConflict("审计记录已变化，停止归档")
                removed += result
                check_same(source_fd, path, source_stat, data_digest)
                check_same(archive_fd, archive_path, archive_stat, data_digest)
            self._archive_receipt(archive_dir, digest, "completed", removed)
            return {"removed": removed, "sha256": digest, "archive_path": str(archive_path)}
        finally:
            if archive_fd is not None:
                os.close(archive_fd)
            os.close(source_fd)

    async def list_candidates(self, limit=100):
        if limit < 1 or limit > 1000:
            raise ValueError("候选数量无效")
        def read():
            result = []
            for value in self.client.sscan_iter(self.registry_key, count=min(limit, 100)):
                generation = self._text(value)
                self._validate(generation)
                result.append(generation)
                if len(result) >= limit:
                    break
            return result
        return await asyncio.to_thread(read)

    async def abort_stage(self, lease, *, known_not_committed=False):
        ok = await asyncio.to_thread(self.client.eval, _ABORT, 2, self.active_key, self._state_key(lease.generation),
            self.collection, lease.generation, "1" if known_not_committed else "0")
        if not ok:
            raise RuntimeError("版本可能已发布，拒绝清理")

    async def collect(self, generation):
        self._validate(generation)
        if generation == self.collection:
            return False  # 固定前缀保留供历史版本恢复
        claimed = await asyncio.to_thread(self.client.eval, _CLAIM, 7, self.active_key, self.registry_key,
            self._state_key(generation), self.lease_key(generation), self.tombstone_key(generation),
            self._tombstones_key, self._tombstone_expiry_key,
            self.collection, generation, self._prefix + "deleting:")
        if claimed == -1:
            raise RuntimeError("知识库回收标记已达安全上限")
        if claimed != 1:
            return False
        # 清理完成前标记不设TTL；写者必须先WATCH标记再检查租约，迟到EXEC会因WATCH失效
        def delete_keys():
            batch = []
            for key in self.client.scan_iter(match=f"{generation}:*", count=200):
                batch.append(key)
                if len(batch) == 200:
                    self.client.delete(*batch)
                    batch.clear()
            if batch:
                self.client.delete(*batch)
        await asyncio.to_thread(delete_keys)
        # 清理完成后保留24小时标记；迟到请求仍因租约过期或已WATCH标记而失败
        finished = await asyncio.to_thread(self.client.eval, _FINISH, 6, self.active_key, self.registry_key,
            self._state_key(generation), self.lease_key(generation), self.tombstone_key(generation),
            self._tombstone_expiry_key, self.collection, generation, 86400000)
        if finished != 1:
            raise RuntimeError("知识库版本清理状态已变化")
        return True


def _local_operator():
    """以进程真实UID标识操作者；容器无passwd条目时保留UID"""
    import os
    import pwd

    uid = os.getuid()
    try:
        return f"uid:{uid}:{pwd.getpwuid(uid).pw_name}"
    except KeyError:
        return f"uid:{uid}"


def main():
    """管理员以系统账号核对未知提交；Redis 凭据只从进程环境读取。"""
    import argparse
    import json
    import os
    import sys

    import redis

    parser = argparse.ArgumentParser(description="检查或人工处置知识库未知提交")
    parser.add_argument("--collection", required=True)
    parser.add_argument("--generation", help="仅inspect时查看指定版本及不含原因的处置记录")
    parser.add_argument("action", choices=("inspect", "resolve", "export", "archive"), nargs="?", default="inspect")
    args = parser.parse_args()
    url = os.environ.get("CLOUDOPS_RAG_ADMIN_REDIS_URL")
    if not url:
        parser.error("必须通过进程环境配置 CLOUDOPS_RAG_ADMIN_REDIS_URL")
    client = redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
    gc = GenerationGC(client, args.collection)

    async def run():
        try:
            if args.action == "inspect":
                result = {"capacity": await gc.capacity_metrics(), "uncertain": await gc.inspect_uncertain()}
                if args.generation:
                    result["generation"] = await gc.inspect_generation(args.generation)
                return result
            if args.generation:
                raise ValueError("--generation 仅供只读查询")
            request = json.load(sys.stdin)
            if args.action == "export":
                return await gc.export_audit(request["path"])
            if args.action == "archive":
                return await gc.archive_audit(request["path"], request["sha256"])
            # 操作者取运行进程的真实系统账户，不能从输入中伪造
            operator = _local_operator()
            await gc.resolve_uncertain(request["generation"], request["action"], operator, request["reason"])
            return {"resolved": True, "capacity": await gc.capacity_metrics()}
        finally:
            await asyncio.to_thread(client.close)

    try:
        print(json.dumps(asyncio.run(run()), ensure_ascii=False))
    except GenerationResolutionOutcomeUnknown:
        print("人工处置结果未知，请先inspect指定版本及审计记录，勿盲目重试", file=sys.stderr)
        return 1
    except GenerationLeaseLost:
        print("仍有活动租约或租约未确认，暂不能处置", file=sys.stderr)
        return 1
    except GenerationConflict:
        print("版本状态不允许此处置，请重新核对", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"知识库管理操作失败（{type(error).__name__}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
