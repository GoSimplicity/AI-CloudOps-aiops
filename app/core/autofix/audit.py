#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: SEC-44 G4 自动修复写操作的结构化审计（意图 + 结果两条记录，可注入 sink）
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, Optional, Protocol, runtime_checkable
import uuid

from app.common.identity import principal_context

AUDIT_EVENT = "autofix.mutation"
AUDIT_SCHEMA_VERSION = 1
PHASE_INTENT = "intent"
PHASE_RESULT = "result"
PHASE_REFUSED = "refused"

audit_logger = logging.getLogger("aiops.audit.autofix")


@runtime_checkable
class AuditSink(Protocol):
    """审计落点：``write`` 抛出任何异常都视为写入失败（意图审计失败时调用方不得执行写操作）"""

    def write(self, record: Dict[str, Any]) -> None: ...


class LoggerAuditSink:
    """默认 sink：一行一条 JSON 写入专用 logger ``aiops.audit.autofix``（由日志采集转发）"""

    def __init__(self, target: Optional[logging.Logger] = None) -> None:
        self._logger = target or audit_logger

    def write(self, record: Dict[str, Any]) -> None:
        self._logger.info(json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False))


_sink: AuditSink = LoggerAuditSink()


def get_audit_sink() -> AuditSink:
    return _sink


def set_audit_sink(sink: AuditSink) -> AuditSink:
    """替换全局审计 sink，返回旧 sink（测试或接入 Go 侧审计时使用）"""
    global _sink
    if not callable(getattr(sink, "write", None)):
        raise TypeError("audit sink 必须实现 write(record)")
    previous = _sink
    _sink = sink
    return previous


def new_audit_id() -> str:
    return uuid.uuid4().hex


def build_audit_record(
    phase: str,
    *,
    audit_id: str,
    namespace: str,
    name: str,
    action: Dict[str, Any],
    auto_executed: bool,
    result: Optional[str] = None,
    error_type: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """构造审计记录：只含白名单字段。

    不记录 action.parameters（patch 内容、镜像、资源值等）、异常消息 str(e)、ApiException body。
    ``error_type`` 只允许异常类名；``reason`` 只允许调用方给出的固定短语。
    """
    principal = principal_context.get()
    return {
        "event": AUDIT_EVENT,
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit_id": audit_id,
        "phase": phase,
        "time": datetime.now(timezone.utc).isoformat(),
        "principal": principal if isinstance(principal, str) and principal else "unknown",
        "target": {"namespace": str(namespace or ""), "kind": "Deployment", "name": str(name or "")},
        "action": {"action_id": str(action.get("action_id") or ""), "action_type": str(action.get("action_type") or "")},
        "auto_executed": bool(auto_executed),
        "result": result,
        "error_type": error_type,
        "reason": reason,
    }


def emit_audit(record: Dict[str, Any]) -> None:
    """写入审计；失败时向上抛出，由调用方决定（意图审计失败 → 不执行）"""
    get_audit_sink().write(record)
