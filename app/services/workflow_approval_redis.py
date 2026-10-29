"""Redis-backed, owner-scoped approvals with durable-before-effect transitions.

A single authoritative Redis with persistent AOF and Redis >= 7.2 is required.
A lost write acknowledgement never grants permission to invoke a side effect.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from typing import Any
import uuid

import redis.asyncio as redis
from redis.exceptions import RedisError

from app.common.exceptions import ValidationError, WorkflowApprovalError
from app.common.identity import principal_context, require_principal
from app.config.settings import config
from app.services.workflow_approvals import _EXPOSED_STATE, WorkflowApproval, WorkflowApprovals

_PREFIX = "aiops:autofix:approval:v1:"
_ACTIVE = _PREFIX + "active"
_ALL = _PREFIX + "all"
_HISTORY = _PREFIX + "history:"
_OWNER_ACTIVE = _PREFIX + "owner-active:"
_MAX_RECORDS = 100000
_MAX_ACTIVE = 1000
_MAX_OWNER_ACTIVE = 50
_TTL_MS = 900000
_HISTORY_SIZE = 200

# Redis scripts validate key types before mutations. A single HSET contains the
# decisive claim or state transition; earlier failures never authorize a write.
_RESERVE = r"""
local function type_ok(key, expected)
 local t=redis.call('TYPE',key).ok; return t=='none' or t==expected
end
if not type_ok(KEYS[1],'hash') or not type_ok(KEYS[2],'zset') or not type_ok(KEYS[3],'set') or not type_ok(KEYS[4],'zset') or not type_ok(KEYS[5],'zset') then return {'corrupt'} end
if redis.call('EXISTS',KEYS[1])==1 then
 if redis.call('HGET',KEYS[1],'owner')~=ARGV[1] or redis.call('HGET',KEYS[1],'request_hash')~=ARGV[3] then return {'conflict'} end
 return {'existing',redis.call('HGET',KEYS[1],'state') or '',redis.call('HGET',KEYS[1],'result') or ''}
end
local now=redis.call('TIME'); local ms=now[1]*1000+math.floor(now[2]/1000)
local stale=redis.call('ZRANGEBYSCORE',KEYS[2],'-inf',ms,'LIMIT',0,1000)
for _,id in ipairs(stale) do
 local key=ARGV[11]..id
 if not type_ok(key,'hash') then return {'corrupt'} end
end
for _,id in ipairs(stale) do
 local key=ARGV[11]..id
 local state=redis.call('HGET',key,'state')
 if state=='pending' then
  redis.call('HSET',key,'state','invalidated','result','','updated_ms',ms,'reason','审批已过期')
 elseif state=='building' or state=='executing' then
  redis.call('HSET',key,'state','uncertain','claim_token','','result','','updated_ms',ms,'reason','执行状态未确认，请人工核对')
 end
 redis.call('ZREM',KEYS[2],id)
end
redis.call('ZREMRANGEBYSCORE',KEYS[5],'-inf',ms)
if redis.call('SCARD',KEYS[3])>=tonumber(ARGV[7]) or redis.call('ZCARD',KEYS[2])>=tonumber(ARGV[8]) then return {'capacity'} end
if redis.call('ZCARD',KEYS[5])>=tonumber(ARGV[9]) then return {'capacity'} end
local expires=ms+tonumber(ARGV[6])
redis.call('HSET',KEYS[1],'owner',ARGV[1],'operation_id',ARGV[2],'request_hash',ARGV[3],'plan_id',ARGV[4],'state','building','created_ms',ms,'updated_ms',ms,'expires_ms',expires,'effects','0','claim_token','','result','','reason','','claimed_actions','[]')
redis.call('SADD',KEYS[3],ARGV[4]);redis.call('ZADD',KEYS[2],expires,ARGV[4]);redis.call('ZADD',KEYS[5],expires,ARGV[4]);redis.call('ZADD',KEYS[4],ms,ARGV[4])
redis.call('ZREMRANGEBYRANK',KEYS[4],0,-tonumber(ARGV[10])-1)
return {'created',tostring(expires)}
"""
_CLAIM = r"""
if redis.call('TYPE',KEYS[1]).ok~='hash' then return {'missing'} end
if redis.call('HGET',KEYS[1],'owner')~=ARGV[1] then return {'forbidden'} end
local state=redis.call('HGET',KEYS[1],'state'); local now=redis.call('TIME');local ms=now[1]*1000+math.floor(now[2]/1000)
if state~='pending' then return {'consumed'} end
if tonumber(redis.call('HGET',KEYS[1],'expires_ms') or '0')<=ms then return {'expired'} end
local payload=redis.call('HGET',KEYS[1],'result');if not payload or payload=='' then return {'corrupt'} end
if payload~=ARGV[3] then return {'stale'} end
redis.call('HSET',KEYS[1],'state','executing','claim_token',ARGV[2],'updated_ms',ms,'effects','0','claimed_actions',ARGV[4])
return {'claimed',payload}
"""
_TRANSITION = r"""
if redis.call('TYPE',KEYS[1]).ok~='hash' then return {'missing'} end
local global_type=redis.call('TYPE',KEYS[2]).ok; local owner_type=redis.call('TYPE',KEYS[3]).ok
if (global_type~='none' and global_type~='zset') or (owner_type~='none' and owner_type~='zset') then return {'corrupt'} end
if redis.call('HGET',KEYS[1],'owner')~=ARGV[1] then return {'forbidden'} end
if redis.call('HGET',KEYS[1],'state')~=ARGV[2] or (ARGV[3]~='' and redis.call('HGET',KEYS[1],'claim_token')~=ARGV[3]) then return {'conflict'} end
local now=redis.call('TIME');local ms=now[1]*1000+math.floor(now[2]/1000)
local deadline=tonumber(redis.call('HGET',KEYS[1],'expires_ms') or '0')
if deadline<=ms then
 redis.call('HSET',KEYS[1],'state','uncertain','reason','执行状态未确认，请人工核对','updated_ms',ms,'claim_token','','result','')
 return {'expired'}
end
if ARGV[4]=='mark' then redis.call('HSET',KEYS[1],'effects','1','updated_ms',ms);return {'ok'} end
if ARGV[4]=='release' then redis.call('HSET',KEYS[1],'state','pending','effects','0','claim_token','','updated_ms',ms);return {'ok'} end
local expiry=deadline;redis.call('HSET',KEYS[1],'state',ARGV[4],'result',ARGV[5],'reason',ARGV[6],'effects',ARGV[7],'claim_token','','updated_ms',ms,'expires_ms',expiry)
if ARGV[4]~='pending' then redis.call('ZREM',KEYS[2],ARGV[9]);redis.call('ZREM',KEYS[3],ARGV[9]) end
return {'ok',tostring(expiry)}
"""
_READ = r"""
if redis.call('TYPE',KEYS[1]).ok~='hash' then return {'missing'} end
if redis.call('HGET',KEYS[1],'owner')~=ARGV[1] then return {'forbidden'} end
local state=redis.call('HGET',KEYS[1],'state');local expiry=tonumber(redis.call('HGET',KEYS[1],'expires_ms') or '')
if not state or not expiry then return {'corrupt'} end
local now=redis.call('TIME');local ms=now[1]*1000+math.floor(now[2]/1000)
if expiry<=ms then
 if state=='building' or state=='executing' then
  redis.call('HSET',KEYS[1],'state','uncertain','claim_token','','result','','updated_ms',ms,'reason','执行状态未确认，请人工核对');state='uncertain'
 elseif state=='pending' then
  redis.call('HSET',KEYS[1],'state','invalidated','claim_token','','result','','updated_ms',ms,'reason','审批已过期');state='invalidated'
 end
end
return {'ok',state,redis.call('HGET',KEYS[1],'operation_id') or '',redis.call('HGET',KEYS[1],'plan_id') or '',
 redis.call('HGET',KEYS[1],'created_ms') or '0',redis.call('HGET',KEYS[1],'updated_ms') or '0',
 redis.call('HGET',KEYS[1],'expires_ms') or '0',redis.call('HGET',KEYS[1],'result') or '',redis.call('HGET',KEYS[1],'reason') or '',
 redis.call('HGET',KEYS[1],'claimed_actions') or '[]'}
"""


def _owner_id(owner: str) -> str:
    return hashlib.sha256(owner.encode()).hexdigest()


def _key(plan_id: str) -> str:
    return _PREFIX + "plan:" + plan_id


def _timestamp(ms: str | int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()


class RedisWorkflowApprovals:
    """No memory fallback. All operations use authenticated Redis owner checks."""

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

    async def _durable(self, script: str, keys: list[str], args: list[Any]) -> list[Any]:
        try:
            async with self.client.pipeline(transaction=False) as pipe:
                pipe.config_get("maxmemory-policy")
                pipe.eval(script, len(keys), *keys, *args)
                pipe.execute_command("WAITAOF", 1, 0, 5000)
                policy, result, acknowledgement = await pipe.execute()
            if policy.get("maxmemory-policy") != "noeviction":
                raise WorkflowApprovalError(503, "审批存储淘汰策略不安全，禁止执行")
            if not acknowledgement or int(acknowledgement[0]) < 1:
                raise WorkflowApprovalError(503, "审批存储未确认持久化，请核对状态后重试")
            return result
        except WorkflowApprovalError:
            raise
        except (RedisError, OSError, TimeoutError) as error:
            raise WorkflowApprovalError(503, "审批存储不可用或结果未确认，请核对状态后重试") from error

    async def reserve(self, operation_id: str | None = None, request_hash: str = "") -> WorkflowApproval:
        owner = require_principal()
        if operation_id:
            plan_id = uuid.uuid5(uuid.NAMESPACE_URL, f"aiops-autofix:{owner}:{operation_id}").hex
        else:
            plan_id = uuid.uuid4().hex
        owner_id = _owner_id(owner)
        result = await self._durable(
            _RESERVE,
            [_key(plan_id), _ACTIVE, _ALL, _HISTORY + owner_id, _OWNER_ACTIVE + owner_id],
            [owner_id, operation_id or "", request_hash, plan_id, "", _TTL_MS, _MAX_RECORDS, _MAX_ACTIVE, _MAX_OWNER_ACTIVE, _HISTORY_SIZE, _PREFIX + "plan:"],
        )
        code = result[0]
        if code == "existing":
            raise WorkflowApprovalError(409, "此操作已存在或正在执行，请查询原操作状态，禁止重复执行")
        if code == "conflict":
            raise WorkflowApprovalError(409, "操作标识已用于其他请求，禁止重复执行")
        if code == "capacity":
            raise WorkflowApprovalError(429, "审批记录达到容量限制，请联系管理员处理")
        if code != "created":
            raise WorkflowApprovalError(503, "审批存储状态异常")
        record = WorkflowApproval(plan_id, owner, "building", int(result[1]) / 1000, _timestamp(result[1]))
        record.operation_id = operation_id or ""
        return record

    async def claim(self, plan_id: str, action_ids: list[str]) -> WorkflowApproval:
        # Validate against the persisted result before the CAS; the CAS protects
        # against another replica winning while validation is underway.
        if not isinstance(action_ids, list) or not action_ids or len(action_ids) > 100 or any(not isinstance(value, str) or not value.strip() or len(value.encode("utf-8", "surrogatepass")) > 256 for value in action_ids):
            raise ValidationError("approved_action_ids", "请选择不超过100个有效动作")
        record = await self.get(plan_id)
        if record.state != "pending":
            raise WorkflowApprovalError(409, "审批正在执行或已经消费，禁止重复确认")
        validator = WorkflowApprovals()
        validator.records[record.plan_id] = record
        validator.clock = lambda: 0
        validator.claim(plan_id, action_ids)
        claim_token = uuid.uuid4().hex
        result = await self._durable(_CLAIM, [_key(plan_id)], [_owner_id(record.owner), claim_token, record.payload_snapshot, json.dumps([value.strip() for value in action_ids], ensure_ascii=True)])
        if result[0] != "claimed":
            raise WorkflowApprovalError(409, "审批已过期或被领取，禁止重复确认")
        record.result = json.loads(result[1])
        record.state = "executing"
        record.claim_token = claim_token
        record.effects_started = False
        record.claimed_action_ids = [value.strip() for value in action_ids]
        return record

    async def _transition(self, record: WorkflowApproval, from_state: str, state: str, payload: str = "", reason: str = "", effects: bool = False) -> None:
        result = await self._durable(
            _TRANSITION,
            [_key(record.plan_id), _ACTIVE, _OWNER_ACTIVE + _owner_id(record.owner)],
            [_owner_id(record.owner), from_state, record.claim_token, state, payload, reason, "1" if effects else "0", _TTL_MS, record.plan_id],
        )
        if result[0] != "ok":
            raise WorkflowApprovalError(409, "审批状态已变化或结果不确定，请人工核对")
        if len(result) > 1:
            record.expires = int(result[1]) / 1000
            record.expires_at = _timestamp(result[1])

    async def mark_effects_started(self, record: WorkflowApproval) -> None:
        await self._transition(record, record.state, "mark")
        record.effects_started = True

    async def publish(self, record: WorkflowApproval, result: dict[str, Any]) -> None:
        previous_state, token = record.state, record.claim_token
        # Reuse the established plan normalization and size bound on a private copy.
        normalized = deepcopy(record)
        WorkflowApprovals().publish(normalized, result)
        result["approval"]["expires_at"] = record.expires_at
        execution = result.get("execution") or {}
        summary = {
            "status": execution.get("status") or result.get("status"),
            "audit_result_recorded": execution.get("audit_result_recorded", result.get("audit_result_recorded")),
            "executed_actions": [{"action_id": action.get("action_id"), "status": action.get("status")} for action in (result.get("executed_actions") or []) if isinstance(action, dict)],
        }
        payload = json.dumps(result if normalized.state == "pending" else {"execution_summary": summary}, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        record.claim_token = token
        await self._transition(record, previous_state, normalized.state, payload, effects=record.effects_started)
        record.state = normalized.state
        record.result = deepcopy(result) if normalized.state == "pending" else None
        record.claim_token = ""

    async def release(self, record: WorkflowApproval, claim_token: str) -> bool:
        if record.state != "executing" or record.effects_started or not claim_token or claim_token != record.claim_token:
            return False
        await self._transition(record, "executing", "release")
        record.state = "pending"
        record.claim_token = ""
        return True

    async def fail(self, record: WorkflowApproval, reason: str) -> None:
        state = "uncertain" if record.effects_started else "invalidated"
        await self._transition(record, record.state, state, reason=reason, effects=record.effects_started)
        record.state = state
        record.result = None
        record.claim_token = ""
        record.reason = reason

    async def discard(self, record: WorkflowApproval) -> None:
        # Never delete an operation tombstone; its key prevents replay.
        await self.fail(record, "计划初始化失败，未执行操作")

    async def get(self, plan_id: str) -> WorkflowApproval:
        owner = require_principal()
        if not isinstance(plan_id, str) or not plan_id or len(plan_id) != 32 or any(ch not in "0123456789abcdef" for ch in plan_id):
            raise WorkflowApprovalError(404, "审批计划不存在或已过期")
        values = await self._durable(_READ, [_key(plan_id)], [_owner_id(owner)])
        if values[0] == "forbidden":
            raise WorkflowApprovalError(403, "无权读取或确认此审批计划")
        if values[0] == "corrupt":
            raise WorkflowApprovalError(503, "审批存储数据无效")
        if values[0] != "ok":
            raise WorkflowApprovalError(404, "审批计划不存在或已过期")
        try:
            if len(values[7].encode()) > 1024 * 1024:
                raise ValueError("oversized payload")
            payload = json.loads(values[7]) if values[7] else None
            claimed = json.loads(values[9])
            if not isinstance(claimed, list) or len(claimed) > 100 or any(not isinstance(item, str) or len(item.encode("utf-8", "surrogatepass")) > 256 for item in claimed):
                raise ValueError("invalid action metadata")
            if values[1] not in _EXPOSED_STATE:
                raise ValueError("invalid approval state")
            if payload is not None and not isinstance(payload, dict):
                raise ValueError("invalid payload")
            if values[1] == "pending" and not payload:
                raise ValueError("missing pending plan")
            record = WorkflowApproval(plan_id, owner, values[1], int(values[6]) / 1000, _timestamp(values[6]), payload, reason=values[8])
        except (ValueError, TypeError, OverflowError, IndexError) as error:
            raise WorkflowApprovalError(503, "审批存储数据无效") from error
        record.operation_id = values[2]
        record.claimed_action_ids = claimed
        record.payload_snapshot = values[7]
        record.created_at = _timestamp(values[4])
        record.updated_at = _timestamp(values[5])
        return record

    async def approval_state(self, plan_id: Any) -> str:
        if not principal_context.get():
            return "pending"
        try:
            record = await self.get(plan_id)
        except WorkflowApprovalError as error:
            if error.status_code == 503:
                raise
            return "voided"
        return _EXPOSED_STATE.get(record.state, "voided")

    @staticmethod
    def _record_view(record: WorkflowApproval) -> dict[str, Any]:
        return {
            "plan_id": record.plan_id,
            "operation_id": record.operation_id or None,
            "state": record.state,
            "approval_state": _EXPOSED_STATE.get(record.state, "voided"),
            "expires_at": record.expires_at,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "reason": record.reason or None,
            "retry_safe": False,
            "claimed_action_ids": record.claimed_action_ids,
            "result": record.result if record.state == "pending" else None,
            "execution_summary": (record.result or {}).get("execution_summary") if record.state == "completed" else None,
        }

    async def get_operation(self, operation_id: str) -> dict[str, Any]:
        owner = require_principal()
        plan_id = uuid.uuid5(uuid.NAMESPACE_URL, f"aiops-autofix:{owner}:{operation_id}").hex
        return self._record_view(await self.get(plan_id))

    async def get_plan(self, plan_id: str) -> dict[str, Any]:
        return self._record_view(await self.get(plan_id))

    async def history(self, limit: int = 50) -> list[dict[str, Any]]:
        owner = require_principal()
        owner_id = _owner_id(owner)
        try:
            async with asyncio.timeout(10):
                # Empty indexes must not hide a missing or unsafe durable store.
                await self._durable("return {'ok'}", [], [])
                ids = await self.client.zrevrange(_HISTORY + owner_id, 0, limit - 1)
                output = []
                for plan_id in ids:
                    try:
                        record = await self.get(plan_id)
                    except WorkflowApprovalError as error:
                        if error.status_code == 503:
                            raise
                        continue
                    output.append(
                        {
                            "plan_id": record.plan_id,
                            "operation_id": record.operation_id or None,
                            "state": record.state,
                            "approval_state": _EXPOSED_STATE.get(record.state, "voided"),
                            "expires_at": record.expires_at,
                            "created_at": record.created_at,
                            "updated_at": record.updated_at,
                            "reason": record.reason or None,
                            "claimed_action_ids": record.claimed_action_ids,
                            "execution_summary": (record.result or {}).get("execution_summary") if record.state == "completed" else None,
                        }
                    )
                return output
        except (RedisError, OSError, TimeoutError) as error:
            raise WorkflowApprovalError(503, "审批历史不可用") from error

    async def close(self) -> None:
        await self.client.aclose()
