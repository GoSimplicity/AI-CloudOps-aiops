"""单进程有界审批记录，随机ID仅定位，归属与原子状态负责授权"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import time
from typing import Any, Callable
import uuid

from app.common.exceptions import ValidationError, WorkflowApprovalError
from app.common.identity import require_principal

CONFIRM_ENDPOINT = "/api/v1/autofix/workflow/confirm"


@dataclass
class WorkflowApproval:
    plan_id: str
    owner: str
    state: str
    expires: float
    expires_at: str
    result: dict[str, Any] | None = None
    effects_started: bool = False
    reason: str = ""


class WorkflowApprovals:
    """按主体隔离审批，并用终结记录阻止重复执行"""

    def __init__(self, maximum: int = 1000, per_owner: int = 50, ttl: float = 900,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.records: dict[str, WorkflowApproval] = {}
        self.maximum = maximum
        self.per_owner = per_owner
        self.ttl = ttl
        self.clock = clock

    def cleanup_expired(self) -> None:
        now = self.clock()
        for plan_id, record in list(self.records.items()):
            if record.state not in {"building", "executing"} and record.expires <= now:
                del self.records[plan_id]

    def _deadline(self, record: WorkflowApproval) -> None:
        record.expires = self.clock() + self.ttl
        record.expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self.ttl)).isoformat()

    def reserve(self) -> WorkflowApproval:
        owner = require_principal()
        self.cleanup_expired()
        if len(self.records) >= self.maximum or sum(record.owner == owner for record in self.records.values()) >= self.per_owner:
            raise WorkflowApprovalError(429, "审批记录达到容量限制，请等待过期后再生成新计划")
        record = WorkflowApproval(uuid.uuid4().hex, owner, "building", 0, "")
        if record.plan_id in self.records:
            raise WorkflowApprovalError(503, "审批标识生成冲突，请稍后重新生成")
        self._deadline(record)
        self.records[record.plan_id] = record
        return record

    def get(self, plan_id: str) -> WorkflowApproval:
        owner = require_principal()
        self.cleanup_expired()
        record = self.records.get(plan_id)
        if record is None:
            raise WorkflowApprovalError(404, "审批计划不存在、已过期或属于旧数据；请先核对目标实际状态，再重新诊断")
        if record.owner != owner:
            raise WorkflowApprovalError(403, "无权读取或确认此审批计划")
        return record

    @staticmethod
    def _blocked(result: dict[str, Any]) -> list[dict[str, Any]]:
        return (result.get("plan") or {}).get("blocked_actions") or result.get("blocked_actions") or []

    def claim(self, plan_id: str, action_ids: list[str]) -> WorkflowApproval:
        record = self.get(plan_id)
        if record.state != "pending":
            raise WorkflowApprovalError(409, record.reason or "审批正在执行或已经消费，不能重复确认或重试同一计划")
        if not isinstance(action_ids, list) or not action_ids or any(not isinstance(value, str) or not value.strip() for value in action_ids):
            raise ValidationError("approved_action_ids", "必须选择有效的待确认动作")
        normalized = [value.strip() for value in action_ids]
        if len(set(normalized)) != len(normalized):
            raise ValidationError("approved_action_ids", "不能重复选择同一动作")
        blocked = {action.get("action_id"): action for action in self._blocked(record.result or {})}
        if any(action_id not in blocked for action_id in normalized):
            raise ValidationError("approved_action_ids", "动作不属于此计划或已被确认")
        for action_id in normalized:
            risk = blocked[action_id].get("risk_assessment") or {}
            if any(risk.get(key) is not True for key in ("namespace_allowed", "permission_allowed", "resource_exists")):
                raise WorkflowApprovalError(403, "资源、权限或命名空间硬限制未通过，人工审批不能覆盖")
        # 此处与状态检查之间没有await，同一进程内只有一个请求能先领取审批
        record.state = "executing"
        record.effects_started = False
        return record

    def publish(self, record: WorkflowApproval, result: dict[str, Any]) -> None:
        plan = result.setdefault("plan", {})
        plan["plan_id"] = record.plan_id
        if result.get("status") == "needs_human_confirmation":
            executed = {action.get("action_id") for action in result.get("executed_actions", [])}
            pending = []
            for action in plan.get("candidate_actions", []):
                if not action.get("executable") or action.get("action_id") in executed:
                    continue
                action = deepcopy(action)
                risk = action.setdefault("risk_assessment", {})
                if risk.get("allowed"):
                    risk["allowed"] = False
                    risk["requires_human_confirmation"] = True
                    risk["reasons"] = list(risk.get("reasons") or []) + ["workflow paused; explicit approval required"]
                pending.append(action)
            if not pending:
                raise WorkflowApprovalError(409, "原计划没有可保留的待审批动作，请重新诊断")
            pending_by_id = {action["action_id"]: action for action in pending}
            plan["candidate_actions"] = [deepcopy(pending_by_id.get(action.get("action_id"), action)) for action in plan.get("candidate_actions", [])]
            plan["blocked_actions"] = pending
            plan["allowed_actions"] = [action for action in plan.get("allowed_actions", []) if action.get("action_id") in executed]
            result["candidate_actions"] = plan["candidate_actions"]
            result["blocked_actions"] = pending
            review = result.setdefault("review", {})
            review["blocked_actions"] = pending
            review["allowed_actions"] = plan["allowed_actions"]
        self._deadline(record)
        state = "pending" if result.get("status") == "needs_human_confirmation" and self._blocked(result) else "completed"
        result["approval"] = {"expires_at": record.expires_at, "state": state, "retry_safe": False}
        if state == "pending":
            # SEC-26 前端契约：待确认动作与确认入口
            result["approval"]["confirmation_required"] = True
            result["approval"]["confirm_endpoint"] = CONFIRM_ENDPOINT
            result["approval"]["pending_action_ids"] = [action.get("action_id") for action in self._blocked(result) if action.get("action_id")]
        if len(json.dumps(result, ensure_ascii=True, allow_nan=False).encode()) > 1024 * 1024:
            raise WorkflowApprovalError(409, "计划上下文超过1 MiB，请缩小范围重新诊断，禁止重试原计划")
        if state == "pending":
            record.result = deepcopy(result)
        else:
            record.result = None
        record.state = state

    def fail(self, record: WorkflowApproval, reason: str) -> None:
        record.state = "uncertain" if record.effects_started else "invalidated"
        record.result = None
        record.reason = reason
        self._deadline(record)

    def discard(self, record: WorkflowApproval) -> None:
        self.records.pop(record.plan_id, None)

    def clear(self) -> None:
        self.records.clear()
