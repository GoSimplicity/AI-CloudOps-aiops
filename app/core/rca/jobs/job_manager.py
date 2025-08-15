#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: RCA 任务管理器（Huey）
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.db.base import session_scope
from app.db.models import (
    RCAJobRecord,
    RCAAnalysisRecord,
    RCACorrelationRecord,
    RCASimpleCorrelationRecord,
    RCARecord,
)
from .tasks import rca_execute_job

logger = logging.getLogger("aiops.rca.jobs")


class RCAJobManager:
    """RCA 异步任务管理器（使用 Huey 进行任务调度）。"""

    def __init__(self, ttl_seconds: int = 24 * 3600):
        self.ttl_seconds = ttl_seconds

    # ----------------------------- 公共接口 ----------------------------- #

    def submit_job(self, params: Dict[str, Any]) -> str:
        """提交 RCA 任务并返回 job_id。

        参数说明：
        - params: 包含 start_time/end_time/metrics/namespace 等参数，需可 JSON 序列化。
        """
        job_id = uuid.uuid4().hex

        # 入库：创建任务记录（waiting）。若服务重启后重投递，应只允许 waiting -> running 的单次执行。
        try:
            with session_scope() as session:
                rec = RCAJobRecord(
                    job_id=job_id,
                    status="waiting",
                    progress=0.0,
                    namespace=(params or {}).get("namespace"),
                    params_json=json.dumps(self._jsonify(params), ensure_ascii=False),
                    result_json=None,
                    error=None,
                )
                session.add(rec)
        except Exception as e:
            logger.warning(f"写入RCA任务记录失败（忽略）：{e}")

        # 提交到 Huey 任务队列
        try:
            rca_execute_job(job_id, self._jsonify(params))
        except Exception as e:
            logger.error(f"提交 Huey 任务失败: {e}")
            # 标记错误
            try:
                with session_scope() as session:
                    rec = (
                        session.query(RCAJobRecord)
                        .filter_by(job_id=job_id)
                        .one_or_none()
                    )
                    if rec:
                        rec.status = "error"
                        rec.progress = 1.0
                        rec.error = str(e)
                        session.add(rec)
            except Exception:
                pass

        logger.info(f"RCA 任务已提交: {job_id}")
        return job_id

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """查询任务状态/结果（基于数据库）。"""
        try:
            from app.db.base import session_scope as _scope
            from app.db.models import RCAJobRecord as _Rec

            with _scope() as session:
                rec = session.query(_Rec).filter_by(job_id=job_id).one_or_none()
                if not rec:
                    return None
                return {
                    "id": rec.job_id,
                    "status": rec.status,
                    "progress": rec.progress,
                    "params": json.loads(rec.params_json) if rec.params_json else None,
                    "result": json.loads(rec.result_json) if rec.result_json else None,
                    "error": rec.error,
                    "created_at": rec.created_at.timestamp()
                    if rec.created_at
                    else None,
                    "updated_at": rec.updated_at.timestamp()
                    if rec.updated_at
                    else None,
                }
        except Exception:
            return None

    # 备注：Huey 已负责队列持久化与恢复，此处不再维护 Redis 缓存文档

    def _jsonify(self, obj: Any) -> Any:
        """将对象转换为 JSON 友好结构。

        说明：
        - datetime 使用 ISO 格式字符串
        - 其他不可序列化对象统一转字符串，避免任务失败
        """
        try:
            import datetime as _dt

            if obj is None:
                return None
            if isinstance(obj, (str, int, float, bool)):
                return obj
            if isinstance(obj, _dt.datetime):
                return obj.isoformat()
            if isinstance(obj, (list, tuple)):
                return [self._jsonify(x) for x in obj]
            if isinstance(obj, dict):
                return {k: self._jsonify(v) for k, v in obj.items()}
            return str(obj)
        except Exception:
            return str(obj)


__all__ = ["RCAJobManager"]


# ----------------------------- 恢复逻辑（应用启动时调用） ----------------------------- #


def _parse_params(params_json: Optional[str]) -> Dict[str, Any]:
    try:
        return json.loads(params_json) if params_json else {}
    except Exception:
        return {}


def _detect_job_has_persisted_result(job: RCAJobRecord) -> bool:
    """检测该 job 是否已在结果表中有结果（避免重复执行）。

    优先检查统一表 `RCARecord`（包含 job_id），其次按类型分表回退检查。
    """
    params = _parse_params(job.params_json)
    job_type = (params or {}).get("job_type") or "analysis"
    try:
        with session_scope() as session:
            if job.result_json:
                return True
            # 统一结果表（最可靠）
            rc = (
                session.query(RCARecord)
                .filter(
                    RCARecord.job_id == job.job_id,
                    RCARecord.deleted_at.is_(None),
                    RCARecord.result_json.isnot(None),
                )
                .one_or_none()
            )
            if rc and rc.result_json:
                return True
            # 分表回退检查
            if job_type == "cross_correlation":
                row = (
                    session.query(RCACorrelationRecord)
                    .filter(
                        RCACorrelationRecord.job_id == job.job_id,
                        RCACorrelationRecord.record_type == "cross_correlation",
                        RCACorrelationRecord.deleted_at.is_(None),
                    )
                    .one_or_none()
                )
                return bool(row and row.result_json)
            if job_type == "correlation":
                row = (
                    session.query(RCASimpleCorrelationRecord)
                    .filter(
                        RCASimpleCorrelationRecord.job_id == job.job_id,
                        RCASimpleCorrelationRecord.record_type == "correlation",
                        RCASimpleCorrelationRecord.deleted_at.is_(None),
                    )
                    .one_or_none()
                )
                return bool(row and row.result_json)
            # analysis 任务回退检查
            row = (
                session.query(RCAAnalysisRecord)
                .filter(RCAAnalysisRecord.created_at >= job.created_at)
                .order_by(RCAAnalysisRecord.id.desc())
                .first()
            )
            return bool(row and row.result_json)
    except Exception:
        return False


def recover_rca_jobs_on_startup(
    *,
    max_age_seconds: int = 300,
    max_jobs: int = 100,
) -> Tuple[int, int]:
    """应用启动时的自愈：

    - 将“长时间未更新”的 running 任务视为悬挂任务：
      - 若已有结果，标记为 success（progress=1.0）
      - 否则重置为 waiting 并重新入队执行

    返回值：(processed_jobs, requeued_jobs)
    """
    logger.info(
        "RCA 启动恢复：扫描悬挂的 running 任务（阈值=%ss，最多处理=%s）",
        max_age_seconds,
        max_jobs,
    )
    now = datetime.now(timezone.utc)
    threshold_time = now - timedelta(seconds=max_age_seconds)

    jobs_to_requeue: List[Tuple[str, Dict[str, Any]]] = []
    processed = 0
    requeued = 0

    try:
        with session_scope() as session:
            # 仅挑选长时间未更新的 running 任务
            candidates: List[RCAJobRecord] = (
                session.query(RCAJobRecord)
                .filter(
                    RCAJobRecord.status == "running",
                    RCAJobRecord.updated_at < threshold_time,
                    RCAJobRecord.deleted_at.is_(None),
                )
                .order_by(RCAJobRecord.id.asc())
                .limit(max(1, int(max_jobs)))
                .all()
            )

            for job in candidates:
                processed += 1
                try:
                    if _detect_job_has_persisted_result(job):
                        job.status = "success"
                        job.progress = 1.0
                        session.add(job)
                        continue

                    # 重置为 waiting，准备重新入队
                    params = _parse_params(job.params_json)
                    job.status = "waiting"
                    job.progress = 0.0
                    session.add(job)
                    jobs_to_requeue.append((job.job_id, params))
                except Exception:
                    # 单条失败不影响其他
                    continue
    except Exception as e:
        logger.warning(f"RCA 启动恢复扫描失败：{e}")
        return 0, 0

    # 统一在事务提交后重新入队
    for job_id, params in jobs_to_requeue:
        try:
            rca_execute_job(job_id, params)
            requeued += 1
        except Exception as e:
            logger.warning(f"RCA 恢复重投任务失败：{job_id}, err={e}")

    if requeued or processed:
        logger.info("RCA 启动恢复完成：处理=%s，重投=%s", processed, requeued)
    return processed, requeued


__all__.append("recover_rca_jobs_on_startup")
