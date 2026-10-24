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
import sys
from typing import IO, Any, Dict, Optional, Protocol, runtime_checkable
import uuid

from app.common.identity import principal_context

AUDIT_EVENT = "autofix.mutation"
AUDIT_SCHEMA_VERSION = 1
PHASE_INTENT = "intent"
PHASE_RESULT = "result"
PHASE_REFUSED = "refused"

audit_logger = logging.getLogger("aiops.audit.autofix")
# SEC-44 修正 P2-3：审计 logger 固定 INFO、不向上传播（不受 LOG_LEVEL / 控制台前缀格式影响）
audit_logger.setLevel(logging.INFO)
audit_logger.propagate = False


@runtime_checkable
class AuditSink(Protocol):
    """审计落点：``write`` 抛出任何异常都视为写入失败（意图审计失败时调用方不得执行写操作）"""

    def write(self, record: Dict[str, Any]) -> None: ...


class StrictJsonLineHandler(logging.StreamHandler):
    """只输出 ``%(message)s`` 一行；写入或 flush 失败原样上抛（不走 logging 的 handleError 吞异常）。

    stream 为 None 时每次写入都解析当前的 ``sys.stdout``（容器日志采集），避免持有启动时的旧流。
    """

    def __init__(self, stream: Optional[IO[str]] = None) -> None:
        super().__init__(stream if stream is not None else sys.stdout)
        self._fixed_stream = stream
        self.setLevel(logging.INFO)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        stream = self._fixed_stream if self._fixed_stream is not None else sys.stdout
        stream.write(self.format(record) + self.terminator)
        stream.flush()

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API
        raise


class LoggerAuditSink:
    """默认 sink：一行一条纯 JSON 写入 stdout（或注入的流），由容器日志采集转发。

    SEC-44 修正 P2-3：不经过 logger 的级别 / disable / propagate / 根 handler，直接交给专用
    StrictJsonLineHandler；因此 LOG_LEVEL=WARNING 也会写出，且写失败会抛给调用方（意图审计失败 → 不执行）。
    """

    def __init__(self, stream: Optional[IO[str]] = None) -> None:
        self._handler = StrictJsonLineHandler(stream)

    def write(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
        log_record = audit_logger.makeRecord(audit_logger.name, logging.INFO, __file__, 0, line, None, None)
        self._handler.handle(log_record)


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
