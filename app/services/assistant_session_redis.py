"""Owner-scoped conversation state; no process-local fallback.

The authority must be Redis >= 7.2 with durable AOF. A running turn is never
automatically reclaimed: a lost worker or lease transitions it to unknown.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any
import uuid

import redis.asyncio as redis
from redis.exceptions import RedisError

from app.common.exceptions import SessionAccessError
from app.common.identity import require_principal
from app.config.settings import config

PREFIX = "aiops:assistant:session:v1:"
GLOBAL = PREFIX + "active"
TTL_SECONDS = 3600
LEASE_MS = 30000
MAX_SESSIONS = 1000
MAX_PER_OWNER = 50
MAX_HISTORY_TURNS = 25
MAX_ITEM_BYTES = 16384
MAX_HISTORY_BYTES = 900000
MAX_TURNS = 100
MAX_RESULT_BYTES = 65536
MAX_RESULT_BUDGET = 900000

_CREATE = r"""
local function ok(k,t) local x=redis.call('TYPE',k).ok;return x=='none' or x==t end
if not ok(KEYS[1],'hash') or not ok(KEYS[2],'zset') or not ok(KEYS[3],'zset') then return {'corrupt'} end
if ARGV[7]=='reuse' then
 for _,sid in ipairs(redis.call('ZREVRANGE',KEYS[3],0,-1)) do
  local item=ARGV[8]..sid
  if redis.call('TYPE',item).ok=='hash' and redis.call('HGET',item,'owner')==ARGV[1] and redis.call('HGET',item,'mode')==ARGV[2] then return {'existing',sid} end
 end
end
if redis.call('EXISTS',KEYS[1])~=0 then return {'conflict'} end
local t=redis.call('TIME');local now=t[1]*1000+math.floor(t[2]/1000)
redis.call('ZREMRANGEBYSCORE',KEYS[2],'-inf',now);redis.call('ZREMRANGEBYSCORE',KEYS[3],'-inf',now)
if redis.call('ZCARD',KEYS[2])>=tonumber(ARGV[4]) or redis.call('ZCARD',KEYS[3])>=tonumber(ARGV[5]) then return {'capacity'} end
local expiry=now+tonumber(ARGV[3])*1000
redis.call('HSET',KEYS[1],'owner',ARGV[1],'mode',ARGV[2],'state','idle','revision','0','history','[]','created_ms',now,'updated_ms',now,'lease_until','0','token','','active_turn','','last_turn','','turn_count','0','result_bytes','0')
redis.call('EXPIRE',KEYS[1],ARGV[3]);redis.call('ZADD',KEYS[2],expiry,ARGV[6]);redis.call('ZADD',KEYS[3],expiry,ARGV[6])
if redis.call('ZCOUNT',KEYS[3],'9000000000000000','9000000000000000')==0 then redis.call('EXPIRE',KEYS[3],ARGV[3]) else redis.call('PERSIST',KEYS[3]) end
return {'ok'}
"""
_READ = r"""
if redis.call('TYPE',KEYS[1]).ok~='hash' then return {'missing'} end
if redis.call('HGET',KEYS[1],'owner')~=ARGV[1] then return {'forbidden'} end
if redis.call('TYPE',KEYS[2]).ok~='zset' or redis.call('TYPE',KEYS[3]).ok~='zset' then return {'corrupt'} end
local t=redis.call('TIME');local now=t[1]*1000+math.floor(t[2]/1000)
local state=redis.call('HGET',KEYS[1],'state')
if state=='running' and tonumber(redis.call('HGET',KEYS[1],'lease_until') or '0')<=now then
 local turn=redis.call('HGET',KEYS[1],'active_turn');local raw=redis.call('HGET',KEYS[1],'turn:'..(turn or ''))
 if not raw then return {'corrupt'} end
 local ok,record=pcall(cjson.decode,raw);if not ok or type(record)~='table' then return {'corrupt'} end
 record.state='unknown';redis.call('HSET',KEYS[1],'state','unknown','token','','updated_ms',now,'turn:'..turn,cjson.encode(record));state='unknown'
end
if state=='idle' then
 local expiry=now+tonumber(ARGV[2])*1000
 redis.call('HSET',KEYS[1],'updated_ms',now);redis.call('EXPIRE',KEYS[1],ARGV[2]);redis.call('ZADD',KEYS[2],expiry,ARGV[3]);redis.call('ZADD',KEYS[3],expiry,ARGV[3])
 if redis.call('ZCOUNT',KEYS[3],'9000000000000000','9000000000000000')==0 then redis.call('EXPIRE',KEYS[3],ARGV[2]) else redis.call('PERSIST',KEYS[3]) end
end
local active=redis.call('HGET',KEYS[1],'active_turn') or ''
local active_record='';if active~='' then active_record=redis.call('HGET',KEYS[1],'turn:'..active) or '' end
return {'ok',state,redis.call('HGET',KEYS[1],'mode') or '',redis.call('HGET',KEYS[1],'revision') or '',
 redis.call('HGET',KEYS[1],'history') or '',redis.call('HGET',KEYS[1],'created_ms') or '',
 redis.call('HGET',KEYS[1],'updated_ms') or '',active,
 redis.call('HGET',KEYS[1],'last_turn') or '',active_record}
"""
_BEGIN = r"""
if redis.call('TYPE',KEYS[1]).ok~='hash' then return {'missing'} end
if redis.call('TYPE',KEYS[2]).ok~='zset' or redis.call('TYPE',KEYS[3]).ok~='zset' then return {'corrupt'} end
if redis.call('HGET',KEYS[1],'owner')~=ARGV[1] then return {'forbidden'} end
local t=redis.call('TIME');local now=t[1]*1000+math.floor(t[2]/1000)
local state=redis.call('HGET',KEYS[1],'state')
local prior=redis.call('HGET',KEYS[1],'turn:'..ARGV[5])
if prior then
 local ok,record=pcall(cjson.decode,prior);if not ok or type(record)~='table' then return {'corrupt'} end
 if record.hash~=ARGV[6] then return {'conflict'} end
 if record.state=='completed' then return {'replay',record.result} end
end
if state=='running' then
 if tonumber(redis.call('HGET',KEYS[1],'lease_until') or '0')<=now then
  local turn=redis.call('HGET',KEYS[1],'active_turn');local raw=redis.call('HGET',KEYS[1],'turn:'..(turn or ''))
  if not raw then return {'corrupt'} end
  local ok,record=pcall(cjson.decode,raw);if not ok or type(record)~='table' then return {'corrupt'} end
  record.state='unknown';redis.call('HSET',KEYS[1],'state','unknown','token','','updated_ms',now,'turn:'..turn,cjson.encode(record)); return {'unknown'}
 end
 return {'busy'}
end
if state=='unknown' then return {'unknown'} end
if state~='idle' then return {'corrupt'} end
if prior then return {'unknown'} end
if tonumber(redis.call('HGET',KEYS[1],'turn_count') or '0')>=tonumber(ARGV[7]) or tonumber(redis.call('HGET',KEYS[1],'result_bytes') or '0')>=tonumber(ARGV[8]) then return {'capacity'} end
local history=redis.call('HGET',KEYS[1],'history');local revision=redis.call('HGET',KEYS[1],'revision')
if not history or not revision then return {'corrupt'} end
redis.call('HSET',KEYS[1],'state','running','token',ARGV[2],'active_turn',ARGV[5],'lease_until',now+tonumber(ARGV[3]),'updated_ms',now,
 'turn:'..ARGV[5],cjson.encode({hash=ARGV[6],state='running',question=ARGV[9],mode=tonumber(ARGV[10])}))
redis.call('HINCRBY',KEYS[1],'turn_count',1)
redis.call('PERSIST',KEYS[1]);redis.call('ZADD',KEYS[2],'9000000000000000',ARGV[4]);redis.call('ZADD',KEYS[3],'9000000000000000',ARGV[4])
redis.call('PERSIST',KEYS[3])
return {'ok',revision,history}
"""
_RENEW = r"""
if redis.call('TYPE',KEYS[1]).ok~='hash' then return {'missing'} end
local t=redis.call('TIME');local now=t[1]*1000+math.floor(t[2]/1000)
if redis.call('HGET',KEYS[1],'owner')~=ARGV[1] or redis.call('HGET',KEYS[1],'state')~='running'
 or redis.call('HGET',KEYS[1],'token')~=ARGV[2] or tonumber(redis.call('HGET',KEYS[1],'lease_until') or '0')<=now then return {'lost'} end
redis.call('HSET',KEYS[1],'lease_until',now+tonumber(ARGV[3]));return {'ok'}
"""
_COMPLETE = r"""
if redis.call('TYPE',KEYS[1]).ok~='hash' or redis.call('TYPE',KEYS[2]).ok~='zset' or redis.call('TYPE',KEYS[3]).ok~='zset' then return {'corrupt'} end
local t=redis.call('TIME');local now=t[1]*1000+math.floor(t[2]/1000)
if redis.call('HGET',KEYS[1],'owner')~=ARGV[1] or redis.call('HGET',KEYS[1],'state')~='running'
 or redis.call('HGET',KEYS[1],'token')~=ARGV[2] or tonumber(redis.call('HGET',KEYS[1],'lease_until') or '0')<=now then return {'lost'} end
local revision=tonumber(redis.call('HGET',KEYS[1],'revision') or '-1')
if revision~=tonumber(ARGV[3]) then return {'lost'} end
if redis.call('HGET',KEYS[1],'active_turn')~=ARGV[8] then return {'lost'} end
local raw=redis.call('HGET',KEYS[1],'turn:'..ARGV[8]);local ok,record=pcall(cjson.decode,raw or '')
if not ok or type(record)~='table' or record.state~='running' then return {'corrupt'} end
local bytes=tonumber(redis.call('HGET',KEYS[1],'result_bytes') or '0')+string.len(ARGV[9]);if bytes>tonumber(ARGV[10]) then return {'capacity'} end
record.state='completed';record.result=ARGV[9]
redis.call('HSET',KEYS[1],'state','idle','token','','active_turn','','last_turn',ARGV[8],'lease_until','0','revision',revision+1,'history',ARGV[4],'mode',ARGV[5],'updated_ms',now,'result_bytes',bytes,'turn:'..ARGV[8],cjson.encode(record))
local expiry=now+tonumber(ARGV[6])*1000
redis.call('EXPIRE',KEYS[1],ARGV[6]);redis.call('ZADD',KEYS[2],expiry,ARGV[7]);redis.call('ZADD',KEYS[3],expiry,ARGV[7])
if redis.call('ZCOUNT',KEYS[3],'9000000000000000','9000000000000000')==0 then redis.call('EXPIRE',KEYS[3],ARGV[6]) else redis.call('PERSIST',KEYS[3]) end
return {'ok'}
"""
_UNKNOWN = r"""
if redis.call('TYPE',KEYS[1]).ok~='hash' then return {'missing'} end
if redis.call('HGET',KEYS[1],'owner')~=ARGV[1] then return {'forbidden'} end
if redis.call('HGET',KEYS[1],'state')~='running' or redis.call('HGET',KEYS[1],'token')~=ARGV[2] then return {'lost'} end
local t=redis.call('TIME');local now=t[1]*1000+math.floor(t[2]/1000)
local turn=redis.call('HGET',KEYS[1],'active_turn');local raw=redis.call('HGET',KEYS[1],'turn:'..(turn or ''))
if not raw then return {'corrupt'} end
local ok,record=pcall(cjson.decode,raw);if not ok or type(record)~='table' then return {'corrupt'} end
record.state='unknown';redis.call('HSET',KEYS[1],'state','unknown','token','','updated_ms',now,'turn:'..turn,cjson.encode(record));return {'ok'}
"""


def _owner_key(owner: str) -> str:
    return PREFIX + "owner:" + hashlib.sha256(owner.encode()).hexdigest()


def _session_key(session_id: str) -> str:
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SessionAccessError(404, "会话不存在或已失效") from None
    return PREFIX + "item:" + session_id


def _timestamp(raw: str) -> str:
    return datetime.fromtimestamp(int(raw) / 1000, timezone.utc).isoformat()


def _history(raw: str) -> list[dict[str, str]]:
    try:
        if len(raw.encode()) > MAX_HISTORY_BYTES:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) > MAX_HISTORY_TURNS:
            raise ValueError
        for item in value:
            if not isinstance(item, dict) or set(item) != {"q", "a", "mode"} or not isinstance(item["q"], str) or not isinstance(item["a"], str) or type(item["mode"]) is not int or item["mode"] not in (1, 2):
                raise ValueError
            if len(item["q"].encode()) > MAX_ITEM_BYTES or len(item["a"].encode()) > MAX_ITEM_BYTES:
                raise ValueError
        return value
    except (ValueError, TypeError, json.JSONDecodeError):
        raise SessionAccessError(503, "会话历史损坏，已停止处理") from None


class RedisAssistantSessions:
    def __init__(self, client: redis.Redis | None = None) -> None:
        self.client = client or redis.Redis(
            host=config.redis.host,
            port=config.redis.port,
            db=config.redis.db,
            password=config.redis.password or None,
            decode_responses=True,
            socket_connect_timeout=config.redis.connection_timeout,
            socket_timeout=max(config.redis.socket_timeout, 7),
            max_connections=config.redis.max_connections,
        )

    async def _eval(self, script: str, keys: list[str], args: list[Any]) -> list[str]:
        try:
            async with self.client.client() as pinned:
                connection = pinned.connection

                async def exchange(*command):
                    await connection.send_command(*command)
                    return await connection.read_response()

                def safe_policy(value):
                    return isinstance(value, (list, tuple)) and len(value) == 2 and value[0] == "maxmemory-policy" and value[1] == "noeviction"

                try:
                    if not safe_policy(await exchange("CONFIG", "GET", "maxmemory-policy")):
                        raise SessionAccessError(503, "会话存储必须使用 noeviction 策略")
                    if await exchange("MULTI") != "OK":
                        raise SessionAccessError(503, "会话存储事务不可用")
                    if await exchange("CONFIG", "GET", "maxmemory-policy") != "QUEUED":
                        raise SessionAccessError(503, "会话存储事务不可用")
                    if await exchange("EVAL", script, len(keys), *keys, *args) != "QUEUED":
                        raise SessionAccessError(503, "会话存储事务不可用")
                    transaction = await exchange("EXEC")
                    if not isinstance(transaction, list) or len(transaction) != 2 or not safe_policy(transaction[0]):
                        raise SessionAccessError(503, "会话存储必须使用 noeviction 策略")
                    ack = await exchange("WAITAOF", 1, 0, 5000)
                    if not ack or int(ack[0]) < 1:
                        raise SessionAccessError(503, "会话存储未确认持久化，请查询会话状态")
                    if not safe_policy(await exchange("CONFIG", "GET", "maxmemory-policy")):
                        raise SessionAccessError(503, "会话存储必须使用 noeviction 策略")
                    if not isinstance(transaction[1], list):
                        raise SessionAccessError(503, "会话存储执行失败")
                    return transaction[1]
                finally:
                    # Never return an interrupted MULTI or unread reply to the pool.
                    try:
                        await connection.disconnect(nowait=True)
                    except BaseException:
                        connection._close()
                        raise
        except SessionAccessError:
            raise
        except (RedisError, OSError, TimeoutError, ValueError, TypeError, AttributeError):
            raise SessionAccessError(503, "会话存储不可用或持久化条件不足") from None

    @staticmethod
    def _check(result: list[str]) -> None:
        code = result[0] if result else "corrupt"
        if code == "ok":
            return
        status = {"missing": 404, "forbidden": 403, "busy": 409, "unknown": 409, "capacity": 429, "conflict": 409, "lost": 409}.get(code, 503)
        message = {
            "missing": "会话不存在或已失效",
            "forbidden": "无权访问此会话",
            "busy": "会话正在处理其他请求",
            "unknown": "上次请求结果未知，请核对历史后处理",
            "capacity": "会话数量达到限制",
            "lost": "会话租约失效，请核对历史",
        }.get(code, "会话状态不可用")
        raise SessionAccessError(status, message)

    async def _create(self, mode: int, reuse: bool) -> tuple[str, bool]:
        owner = require_principal()
        sid = str(uuid.uuid4())
        result = await self._eval(_CREATE, [_session_key(sid), GLOBAL, _owner_key(owner)], [owner, mode, TTL_SECONDS, MAX_SESSIONS, MAX_PER_OWNER, sid, "reuse" if reuse else "new", PREFIX + "item:"])
        if result and result[0] == "existing":
            return result[1], False
        self._check(result)
        return sid, True

    async def create(self, mode: int) -> str:
        sid, _ = await self._create(mode, False)
        return sid

    async def create_or_get(self, mode: int) -> tuple[str, bool]:
        return await self._create(mode, True)

    async def info(self, sid: str) -> dict[str, Any]:
        owner = require_principal()
        result = await self._eval(_READ, [_session_key(sid), GLOBAL, _owner_key(owner)], [owner, TTL_SECONDS, sid])
        self._check(result)
        try:
            if result[1] not in ("idle", "running", "unknown") or int(result[2]) not in (1, 2) or not 0 <= int(result[3]) <= MAX_TURNS:
                raise ValueError
            history = _history(result[4])
            active = None
            if result[7]:
                if len(result[9].encode()) > MAX_ITEM_BYTES * 6 + 1024:
                    raise ValueError
                active = json.loads(result[9])
                if (
                    not isinstance(active, dict)
                    or not isinstance(active.get("question"), str)
                    or len(active["question"].encode()) > MAX_ITEM_BYTES
                    or type(active.get("mode")) is not int
                    or active.get("mode") not in (1, 2)
                ):
                    raise ValueError
            return {
                "success": True,
                "session_id": sid,
                "created_time": _timestamp(result[5]),
                "last_activity": _timestamp(result[6]),
                "message_count": int(result[3]) * 2,
                "mode": int(result[2]),
                "status": result[1],
                "revision": int(result[3]),
                "conversation_history": history,
                "active_turn_id": result[7] or None,
                "last_turn_id": result[8] or None,
                "active_question": active.get("question") if active else None,
                "active_mode": active.get("mode") if active else None,
            }
        except (ValueError, TypeError, IndexError, OverflowError, OSError):
            raise SessionAccessError(503, "会话状态损坏，已停止处理") from None

    async def begin(self, sid: str, turn_id: str, request_hash: str, question: str, mode: int) -> tuple[str | None, int, list[dict[str, str]], dict[str, Any] | None]:
        owner = require_principal()
        token = uuid.uuid4().hex
        if len(question.encode()) > MAX_ITEM_BYTES:
            raise SessionAccessError(400, "问题内容超过安全上限")
        result = await self._eval(_BEGIN, [_session_key(sid), GLOBAL, _owner_key(owner)], [owner, token, LEASE_MS, sid, turn_id, request_hash, MAX_TURNS, MAX_RESULT_BUDGET, question, mode])
        if result and result[0] == "replay":
            try:
                raw = result[1]
                if len(raw.encode()) > MAX_RESULT_BYTES:
                    raise ValueError
                response = json.loads(raw)
                if not isinstance(response, dict) or not isinstance(response.get("answer"), str):
                    raise ValueError
                return None, 0, [], response
            except (ValueError, TypeError, IndexError):
                raise SessionAccessError(503, "会话结果损坏，已停止处理") from None
        self._check(result)
        return token, int(result[1]), _history(result[2]), None

    async def renew(self, sid: str, token: str) -> None:
        result = await self._eval(_RENEW, [_session_key(sid)], [require_principal(), token, LEASE_MS])
        self._check(result)

    async def complete(self, sid: str, token: str, revision: int, history: list[dict[str, str]], question: str, answer: str, mode: int, turn_id: str, response: dict[str, Any]) -> None:
        if len(question.encode()) > MAX_ITEM_BYTES or len(answer.encode()) > MAX_ITEM_BYTES:
            raise SessionAccessError(503, "会话内容超过安全上限")
        updated = (history + [{"q": question, "a": answer, "mode": mode}])[-MAX_HISTORY_TURNS:]
        raw = json.dumps(updated, ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode()) > MAX_HISTORY_BYTES:
            raise SessionAccessError(503, "会话历史超过安全上限")
        try:
            result_data = json.dumps(response, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)
        except (TypeError, ValueError):
            raise SessionAccessError(503, "会话结果无法安全保存") from None
        if len(result_data.encode()) > MAX_RESULT_BYTES:
            raise SessionAccessError(503, "会话结果超过安全上限")
        owner = require_principal()
        result = await self._eval(_COMPLETE, [_session_key(sid), GLOBAL, _owner_key(owner)], [owner, token, revision, raw, mode, TTL_SECONDS, sid, turn_id, result_data, MAX_RESULT_BUDGET])
        self._check(result)

    async def unknown(self, sid: str, token: str) -> None:
        result = await self._eval(_UNKNOWN, [_session_key(sid)], [require_principal(), token])
        if result and result[0] not in ("ok", "lost", "missing"):
            self._check(result)

    async def close(self) -> None:
        await self.client.aclose()

    async def health_check(self) -> bool:
        try:
            result = await self._eval("return {'ok'}", [], [])
            return bool(result and result[0] == "ok")
        except SessionAccessError:
            return False
