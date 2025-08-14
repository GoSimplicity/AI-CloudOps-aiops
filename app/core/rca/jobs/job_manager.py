#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

import redis
from redis.connection import ConnectionPool

from app.config.settings import config
from app.core.rca.analyzer import RCAAnalyzer
from app.core.rca.correlator import CorrelationAnalyzer
from app.db.base import session_scope
from app.db.models import (
    RCAJobRecord,
    RCAAnalysisRecord,
    RCARecord,
    RCACorrelationRecord,
    RCASimpleCorrelationRecord,
)

logger = logging.getLogger("aiops.rca.jobs")


class RCAJobManager:
    """RCA 异步任务管理器。

    说明：
    - 采用 Redis 保存任务信息，键名格式为 `aiops:rca:job:{job_id}`。
    - 任务状态：queued -> running -> succeeded/failed。
    - 结果采用 JSON 存储，尽量保持与 API 响应兼容的结构。
    """

    _executor: ThreadPoolExecutor = ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="rca-job"
    )

    def __init__(self, ttl_seconds: int = 24 * 3600):
        self.ttl_seconds = ttl_seconds
        pool = ConnectionPool(
            host=config.redis.host,
            port=config.redis.port,
            db=config.redis.db,
            password=config.redis.password or None,
            decode_responses=True,
            max_connections=config.redis.max_connections,
            socket_timeout=config.redis.socket_timeout,
            socket_connect_timeout=config.redis.connection_timeout,
        )
        self.redis_client = redis.Redis(connection_pool=pool)

        # 简单连通性检查，便于早期发现配置问题
        try:
            self.redis_client.ping()
            logger.info("RCAJobManager 已连接 Redis")
        except Exception as e:
            logger.error(f"连接 Redis 失败: {e}")
            raise

    # ----------------------------- 公共接口 ----------------------------- #

    def submit_job(self, params: Dict[str, Any]) -> str:
        """提交 RCA 任务并返回 job_id。

        参数说明：
        - params: 包含 start_time/end_time/metrics/namespace 等参数，需可 JSON 序列化。
        """
        job_id = uuid.uuid4().hex

        job_doc = {
            "id": job_id,
            "status": "queued",
            "progress": 0.0,
            "params": self._jsonify(params),
            "result": None,
            "error": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

        self._save(job_id, job_doc)

        # 入库：创建任务记录
        try:
            with session_scope() as session:
                rec = RCAJobRecord(
                    job_id=job_id,
                    status="queued",
                    progress=0.0,
                    namespace=(params or {}).get("namespace"),
                    params_json=json.dumps(self._jsonify(params), ensure_ascii=False),
                    result_json=None,
                    error=None,
                )
                session.add(rec)
        except Exception as e:
            logger.warning(f"写入RCA任务记录失败（忽略）：{e}")

        # 在线程池中执行任务，避免阻塞请求线程
        self._executor.submit(self._run_job_safely, job_id, params)

        logger.info(f"RCA 任务已提交: {job_id}")
        return job_id

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """查询任务状态/结果。"""
        return self._load(job_id)

    def list_jobs(
        self,
        page: int = 1,
        size: int = 20,
        status: Optional[str] = None,
        namespace: Optional[str] = None,
    ) -> Dict[str, Any]:
        """列出任务（支持分页与简单过滤）。

        返回结构：{"items": [...], "total": int}
        """
        try:
            pattern = "aiops:rca:job:*"
            jobs: list[Dict[str, Any]] = []
            # 扫描所有任务键
            for key in self.redis_client.scan_iter(match=pattern):
                try:
                    raw = self.redis_client.get(key)
                    if not raw:
                        continue
                    doc = json.loads(raw)
                    # 过滤状态
                    if status and doc.get("status") != status:
                        continue
                    # 过滤命名空间（写在 params.namespace 中）
                    if namespace:
                        params = doc.get("params") or {}
                        if params.get("namespace") != namespace:
                            continue
                    jobs.append(doc)
                except Exception:
                    # 单条失败不影响整体
                    continue

            # 按创建时间倒序
            jobs.sort(key=lambda d: d.get("created_at") or 0, reverse=True)

            total = len(jobs)
            page = max(1, int(page or 1))
            size = max(1, min(100, int(size or 20)))
            start = (page - 1) * size
            end = start + size
            page_items = jobs[start:end]

            # 精简字段，避免返回过大 payload
            def _to_summary(doc: Dict[str, Any]) -> Dict[str, Any]:
                params = doc.get("params") or {}
                return {
                    "id": doc.get("id"),
                    "status": doc.get("status"),
                    "progress": doc.get("progress"),
                    "namespace": params.get("namespace"),
                    "time_range": {
                        "start": params.get("start_time"),
                        "end": params.get("end_time"),
                    },
                    "created_at": doc.get("created_at"),
                    "updated_at": doc.get("updated_at"),
                    "has_result": bool(doc.get("result")),
                    "has_error": bool(doc.get("error")),
                }

            return {"items": [_to_summary(d) for d in page_items], "total": total}
        except Exception as e:
            logger.error(f"列出RCA任务失败: {e}")
            return {"items": [], "total": 0}

    def _run_job_safely(self, job_id: str, params: Dict[str, Any]):
        """执行任务并确保状态持久化。"""
        try:
            self._update(job_id, {"status": "running", "progress": 0.05})
            try:
                with session_scope() as session:
                    rec = session.query(RCAJobRecord).filter_by(job_id=job_id).one_or_none()
                    if rec:
                        rec.status = "running"
                        rec.progress = 0.05
                        session.add(rec)
            except Exception:
                pass

            analyzer = RCAAnalyzer()
            corr_analyzer = CorrelationAnalyzer()

            # 进度提示：数据采集阶段
            self._update(job_id, {"progress": 0.2})

            job_type = (params or {}).get("job_type") or "analysis"
            if job_type == "cross_correlation":
                # 收集数据并执行跨时滞相关
                metrics = params.get("metrics")
                if not metrics:
                    try:
                        from app.config.settings import config as _config
                        metrics = _config.rca.default_metrics
                    except Exception:
                        metrics = []
                metrics_data = asyncio.run(
                    analyzer._collect_metrics_data(
                        params["start_time"], params["end_time"], metrics, namespace=params.get("namespace")
                    )
                )
                if not metrics_data:
                    result = {"correlations": {}, "cross_correlations": {}}
                else:
                    max_lags = int((params.get("max_lags") or 10))
                    result = asyncio.run(
                        corr_analyzer.analyze_correlations_with_cross_lag(metrics_data, max_lags=max(1, min(20, max_lags)))
                    )
            elif job_type == "correlation":
                # 普通相关性分析（目标指标可选）
                target_metric = params.get("target_metric")
                metrics = params.get("metrics")
                if not metrics:
                    try:
                        from app.config.settings import config as _config
                        metrics = _config.rca.default_metrics
                    except Exception:
                        metrics = []
                result = asyncio.run(
                    analyzer.analyze_correlations(
                        params["start_time"],
                        params["end_time"],
                        target_metric,
                        metrics,
                        namespace=params.get("namespace"),
                    )
                )
            else:
                # 执行常规分析（在工作线程中创建事件循环执行异步分析）
                result = asyncio.run(
                    analyzer.analyze(
                        params["start_time"],
                        params["end_time"],
                        params.get("metrics"),
                        include_logs=params.get("include_logs"),
                        include_traces=None,
                        namespace=params.get("namespace"),
                    )
                )

            # 分阶段推进进度
            self._update(job_id, {"progress": 0.9})

            # 成功完成
            self._update(
                job_id,
                {
                    "status": "succeeded",
                    "progress": 1.0,
                    "result": self._jsonify(result),
                },
            )
            try:
                with session_scope() as session:
                    rec = session.query(RCAJobRecord).filter_by(job_id=job_id).one_or_none()
                    if rec:
                        rec.status = "succeeded"
                        rec.progress = 1.0
                        rec.result_json = json.dumps(self._jsonify(result), ensure_ascii=False)
                        session.add(rec)
                    if job_type == "cross_correlation":
                        # 入库跨时滞相关结果
                        summary_text = None
                        try:
                            cc = (result or {}).get("cross_correlations") or {}
                            summary_text = f"cc_pairs={sum(len(v) for v in cc.values())}"
                        except Exception:
                            summary_text = None
                        cc_rec = RCACorrelationRecord(
                            job_id=job_id,
                            record_type="cross_correlation",
                            namespace=params.get("namespace"),
                            start_time=str(params.get("start_time")),
                            end_time=str(params.get("end_time")),
                            metrics=json.dumps(params.get("metrics") or [], ensure_ascii=False),
                            params_json=json.dumps(self._jsonify(params), ensure_ascii=False),
                            status="success",
                            summary=summary_text,
                            result_json=json.dumps(self._jsonify(result), ensure_ascii=False),
                            error=None,
                        )
                        session.add(cc_rec)
                        # 同步写入统一记录表
                        try:
                            rc = RCARecord(
                                record_type="cross_correlation",
                                namespace=params.get("namespace"),
                                start_time=str(params.get("start_time")),
                                end_time=str(params.get("end_time")),
                                metrics=json.dumps(params.get("metrics") or [], ensure_ascii=False),
                                params_json=json.dumps(self._jsonify(params), ensure_ascii=False),
                                job_id=job_id,
                                status="success",
                                summary=summary_text,
                                result_json=json.dumps(self._jsonify(result), ensure_ascii=False),
                                error=None,
                            )
                            session.add(rc)
                        except Exception:
                            pass
                    elif job_type == "correlation":
                        # 入库普通相关性（目标指标与相关性列表）
                        try:
                            summary_text = None
                            try:
                                if isinstance(result, dict) and len(result) == 1:
                                    # 取唯一目标的top个数
                                    only_key = next(iter(result.keys()))
                                    summary_text = f"pairs={len(result.get(only_key) or [])}"
                            except Exception:
                                summary_text = None
                            corr_rec = RCASimpleCorrelationRecord(
                                job_id=job_id,
                                record_type="correlation",
                                namespace=params.get("namespace"),
                                start_time=str(params.get("start_time")),
                                end_time=str(params.get("end_time")),
                                metrics=json.dumps(params.get("metrics") or [], ensure_ascii=False),
                                params_json=json.dumps(self._jsonify(params), ensure_ascii=False),
                                status="success",
                                summary=summary_text,
                                result_json=json.dumps(self._jsonify(result), ensure_ascii=False),
                                error=None,
                            )
                            session.add(corr_rec)
                        except Exception:
                            pass
                        # 同步写入统一记录表
                        try:
                            rc = RCARecord(
                                record_type="correlation",
                                namespace=params.get("namespace"),
                                start_time=str(params.get("start_time")),
                                end_time=str(params.get("end_time")),
                                metrics=json.dumps(params.get("metrics") or [], ensure_ascii=False),
                                params_json=json.dumps(self._jsonify(params), ensure_ascii=False),
                                job_id=job_id,
                                status="success",
                                summary=summary_text,
                                result_json=json.dumps(self._jsonify(result), ensure_ascii=False),
                                error=None,
                            )
                            session.add(rc)
                        except Exception:
                            pass
                    else:
                        # 保存一份完整的分析记录到 RCAAnalysisRecord（record接口查看）
                        summary_text = None
                        try:
                            summary_text = (result or {}).get("summary")
                        except Exception:
                            summary_text = None
                        analysis = RCAAnalysisRecord(
                            start_time=str(params.get("start_time")),
                            end_time=str(params.get("end_time")),
                            metrics=json.dumps(params.get("metrics") or [] , ensure_ascii=False),
                            namespace=params.get("namespace"),
                            service_name=None,
                            status="success",
                            summary=summary_text,
                            result_json=json.dumps(self._jsonify(result), ensure_ascii=False),
                            error=None,
                        )
                        session.add(analysis)
                        # 同步写入统一记录表，便于统一查询
                        try:
                            rc = RCARecord(
                                record_type="analysis",
                                namespace=params.get("namespace"),
                                start_time=str(params.get("start_time")),
                                end_time=str(params.get("end_time")),
                                metrics=json.dumps(params.get("metrics") or [], ensure_ascii=False),
                                params_json=json.dumps(self._jsonify(params), ensure_ascii=False),
                                job_id=job_id,
                                status="success",
                                summary=summary_text,
                                result_json=json.dumps(self._jsonify(result), ensure_ascii=False),
                                error=None,
                            )
                            session.add(rc)
                        except Exception:
                            pass
            except Exception:
                pass
            logger.info(f"RCA 任务成功完成: {job_id}")

        except Exception as e:
            logger.exception(f"RCA 任务执行失败: {job_id}")
            self._update(job_id, {"status": "failed", "error": str(e), "progress": 1.0})
            try:
                with session_scope() as session:
                    rec = session.query(RCAJobRecord).filter_by(job_id=job_id).one_or_none()
                    if rec:
                        rec.status = "failed"
                        rec.progress = 1.0
                        rec.error = str(e)
                        session.add(rec)
                        job_type = (params or {}).get("job_type") or "analysis"
                        if job_type == "cross_correlation":
                            cc_rec = RCACorrelationRecord(
                                job_id=job_id,
                                record_type="cross_correlation",
                                namespace=params.get("namespace"),
                                start_time=str(params.get("start_time")),
                                end_time=str(params.get("end_time")),
                                metrics=json.dumps(params.get("metrics") or [], ensure_ascii=False),
                                params_json=json.dumps(self._jsonify(params), ensure_ascii=False),
                                status="failed",
                                summary=None,
                                result_json=None,
                                error=str(e),
                            )
                            session.add(cc_rec)
                            # 同步写入统一记录表
                            try:
                                rc = RCARecord(
                                    record_type="cross_correlation",
                                    namespace=params.get("namespace"),
                                    start_time=str(params.get("start_time")),
                                    end_time=str(params.get("end_time")),
                                    metrics=json.dumps(params.get("metrics") or [], ensure_ascii=False),
                                    params_json=json.dumps(self._jsonify(params), ensure_ascii=False),
                                    job_id=job_id,
                                    status="failed",
                                    summary=None,
                                    result_json=None,
                                    error=str(e),
                                )
                                session.add(rc)
                            except Exception:
                                pass
                        elif job_type == "correlation":
                            try:
                                corr_rec = RCASimpleCorrelationRecord(
                                    job_id=job_id,
                                    record_type="correlation",
                                    namespace=params.get("namespace"),
                                    start_time=str(params.get("start_time")),
                                    end_time=str(params.get("end_time")),
                                    metrics=json.dumps(params.get("metrics") or [], ensure_ascii=False),
                                    params_json=json.dumps(self._jsonify(params), ensure_ascii=False),
                                    status="failed",
                                    summary=None,
                                    result_json=None,
                                    error=str(e),
                                )
                                session.add(corr_rec)
                            except Exception:
                                pass
                            # 同步写入统一记录表
                            try:
                                rc = RCARecord(
                                    record_type="correlation",
                                    namespace=params.get("namespace"),
                                    start_time=str(params.get("start_time")),
                                    end_time=str(params.get("end_time")),
                                    metrics=json.dumps(params.get("metrics") or [], ensure_ascii=False),
                                    params_json=json.dumps(self._jsonify(params), ensure_ascii=False),
                                    job_id=job_id,
                                    status="failed",
                                    summary=None,
                                    result_json=None,
                                    error=str(e),
                                )
                                session.add(rc)
                            except Exception:
                                pass
                        else:
                            # 同时写入 RCAAnalysisRecord 失败记录
                            analysis = RCAAnalysisRecord(
                                start_time=str(params.get("start_time")),
                                end_time=str(params.get("end_time")),
                                metrics=json.dumps(params.get("metrics") or [] , ensure_ascii=False),
                                namespace=params.get("namespace"),
                                service_name=None,
                                status="failed",
                                summary=None,
                                result_json=None,
                                error=str(e),
                            )
                            session.add(analysis)
                            # 同步写入统一记录表
                            try:
                                rc = RCARecord(
                                    record_type="analysis",
                                    namespace=params.get("namespace"),
                                    start_time=str(params.get("start_time")),
                                    end_time=str(params.get("end_time")),
                                    metrics=json.dumps(params.get("metrics") or [], ensure_ascii=False),
                                    params_json=json.dumps(self._jsonify(params), ensure_ascii=False),
                                    job_id=job_id,
                                    status="failed",
                                    summary=None,
                                    result_json=None,
                                    error=str(e),
                                )
                                session.add(rc)
                            except Exception:
                                pass
            except Exception:
                pass

    # ----------------------------- Redis 序列化 ----------------------------- #

    def _key(self, job_id: str) -> str:
        return f"aiops:rca:job:{job_id}"

    def _save(self, job_id: str, doc: Dict[str, Any]):
        value = json.dumps(self._jsonify(doc), ensure_ascii=False)
        self.redis_client.setex(self._key(job_id), self.ttl_seconds, value)

    def _load(self, job_id: str) -> Optional[Dict[str, Any]]:
        raw = self.redis_client.get(self._key(job_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data
        except Exception:
            return None

    def _update(self, job_id: str, fields: Dict[str, Any]):
        doc = self._load(job_id) or {"id": job_id}
        doc.update(self._jsonify(fields))
        doc["updated_at"] = time.time()
        self._save(job_id, doc)

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
