#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: RCA 异步任务定义（Huey）
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
from typing import Any, Dict, Optional

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
from app.core.rca.collectors.k8s_events_collector import K8sEventsCollector

from .huey_app import rca_huey

logger = logging.getLogger("aiops.rca.tasks")


def _jsonify(obj: Any) -> Any:
    """将对象转换为 JSON 友好结构（datetime->iso，其余转字符串）。"""
    try:
        import datetime as _dt

        if obj is None:
            return None
        if isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, _dt.datetime):
            return obj.isoformat()
        if isinstance(obj, (list, tuple)):
            return [_jsonify(x) for x in obj]
        if isinstance(obj, dict):
            return {k: _jsonify(v) for k, v in obj.items()}
        return str(obj)
    except Exception:
        return str(obj)


def _update_job_status(
    job_id: str,
    *,
    status: Optional[str] = None,
    progress: Optional[float] = None,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
):
    """数据库内更新任务状态与结果。

    注意：result_json 可能很大，若写入 cl_aiops_rca_jobs 失败，不要中断主流程，允许结果进入分表。
    """
    try:
        with session_scope() as session:
            rec = session.query(RCAJobRecord).filter_by(job_id=job_id).one_or_none()
            if not rec:
                return
            if status is not None:
                rec.status = status
            if progress is not None:
                rec.progress = progress
            if result is not None:
                try:
                    result_str = json.dumps(_jsonify(result), ensure_ascii=False)
                    # 保护：过大结果不写入 jobs 表，避免 DataError
                    if len(result_str) > 60000:
                        rec.result_json = None
                    else:
                        rec.result_json = result_str
                except Exception:
                    rec.result_json = None
            if error is not None:
                rec.error = error
            session.add(rec)
    except Exception as e:
        logger.warning(f"更新任务状态失败（尝试降级重试）：{e}")
        # 降级重试：不写 result_json，再次仅更新状态/进度/错误
        try:
            with session_scope() as session:
                rec = session.query(RCAJobRecord).filter_by(job_id=job_id).one_or_none()
                if not rec:
                    return
                if status is not None:
                    rec.status = status
                if progress is not None:
                    rec.progress = progress
                if error is not None:
                    rec.error = error
                # 显式清空 result_json，防止再次因过大失败
                rec.result_json = None
                session.add(rec)
        except Exception as e2:
            logger.warning(f"更新任务状态降级重试仍失败（忽略）：{e2}")


async def _rca_execute_job_async(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """RCA 任务异步执行核心。

    在有事件循环的环境中使用（FastAPI/测试时的 immediate 模式）。
    """
    # 幂等保护：仅允许 waiting 状态进入执行。其余状态直接跳过，避免重启/重复调度二次执行
    try:
        with session_scope() as session:
            rec = session.query(RCAJobRecord).filter_by(job_id=job_id).one_or_none()
            if rec is None:
                # 若找不到记录，创建最小记录并允许执行
                rec = RCAJobRecord(
                    job_id=job_id,
                    status="waiting",
                    progress=0.0,
                    namespace=(params or {}).get("namespace"),
                    params_json=json.dumps(_jsonify(params), ensure_ascii=False),
                )
                session.add(rec)
            if rec.status != "waiting":
                # 如果是 dangling 的 running 状态，直接标记为 skipped，避免重启后重复执行
                if rec.status == "running":
                    rec.status = "skipped"
                    rec.progress = 1.0
                    session.add(rec)
                logger.info(
                    f"RCA 任务跳过执行（非waiting状态）：{job_id}, status={rec.status}"
                )
                return {
                    "status": "skipped",
                    "job_id": job_id,
                    "reason": f"status={rec.status}",
                }
            # 将 waiting -> running
            rec.status = "running"
            rec.progress = 0.05
            session.add(rec)
    except Exception as _e:
        logger.warning(f"RCA 任务状态预检查失败（继续尝试执行）：{job_id}, err={_e}")
        _update_job_status(job_id, status="running", progress=0.05)

    try:
        analyzer = RCAAnalyzer()
        corr_analyzer = CorrelationAnalyzer()

        # 任务分派
        job_type = (params or {}).get("job_type") or "analysis"

        # 进度：数据准备
        _update_job_status(job_id, progress=0.2)

        # 统一解析时间参数为 datetime，兼容字符串/时间戳
        def _to_dt(val: Any) -> _dt.datetime:
            if isinstance(val, _dt.datetime):
                return val if val.tzinfo else val.replace(tzinfo=_dt.timezone.utc)
            if isinstance(val, (int, float)):
                return _dt.datetime.fromtimestamp(float(val), tz=_dt.timezone.utc)
            if isinstance(val, str):
                txt = val.strip()
                if txt.endswith("Z"):
                    txt = txt[:-1] + "+00:00"
                try:
                    dt = _dt.datetime.fromisoformat(txt)
                except Exception:
                    try:
                        from dateutil import parser as _parser  # type: ignore

                        dt = _parser.parse(val)
                    except Exception as e:
                        raise ValueError(f"invalid datetime: {val}") from e
                return dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)
            raise ValueError(f"invalid datetime type: {type(val)}")

        try:
            params["start_time"] = _to_dt(params.get("start_time"))
            params["end_time"] = _to_dt(params.get("end_time"))
        except Exception:
            # 失败时保持原值，后续流程可能自行处理
            pass

        if job_type == "cross_correlation":
            metrics = params.get("metrics")
            if not metrics:
                try:
                    from app.config.settings import config as _config

                    metrics = _config.rca.default_metrics
                except Exception:
                    metrics = []
            # 输入指标数量保护，避免过多基础指标导致Prometheus超时
            try:
                from app.config.settings import config as _config

                _max_input = int(getattr(_config.rca, "input_metrics_max", 80) or 80)
            except Exception:
                _max_input = 80
            _metrics_eff = list(metrics)[: max(1, _max_input)]
            metrics_data = await analyzer._collect_metrics_data(
                params["start_time"],
                params["end_time"],
                _metrics_eff,
                namespace=params.get("namespace"),
            )
            if not metrics_data:
                result: Dict[str, Any] = {"correlations": {}, "cross_correlations": {}}
            else:
                max_lags = int((params.get("max_lags") or 10))
                result = await corr_analyzer.analyze_correlations_with_cross_lag(
                    metrics_data, max_lags=max(1, min(20, max_lags))
                )
                # 兜底：若跨时滞结果完全为空，尝试叠加默认动态指标再次计算
                try:
                    cc_pairs = sum(len(v) for v in (result or {}).get("cross_correlations", {}).values())
                except Exception:
                    cc_pairs = 0
                if cc_pairs == 0:
                    try:
                        from app.config.settings import config as _config
                        dynamic_defaults = [
                            "container_cpu_usage_seconds_total",
                            "container_memory_working_set_bytes",
                            "kube_pod_container_status_restarts_total",
                        ]
                        merged = list(dict.fromkeys(list(_metrics_eff) + dynamic_defaults))
                        _metrics_eff2 = merged[: max(1, int(getattr(_config.rca, "input_metrics_max", 80) or 80))]
                        metrics_data2 = await analyzer._collect_metrics_data(
                            params["start_time"], params["end_time"], _metrics_eff2, namespace=params.get("namespace")
                        )
                        if metrics_data2:
                            result2 = await corr_analyzer.analyze_correlations_with_cross_lag(
                                metrics_data2, max_lags=max(1, min(20, max_lags))
                            )
                            try:
                                cc_pairs2 = sum(len(v) for v in (result2 or {}).get("cross_correlations", {}).values())
                            except Exception:
                                cc_pairs2 = 0
                            if cc_pairs2 > 0:
                                result = result2
                    except Exception:
                        pass
        elif job_type == "correlation":
            target_metric = params.get("target_metric")
            metrics = params.get("metrics")
            if not metrics:
                try:
                    from app.config.settings import config as _config

                    metrics = _config.rca.default_metrics
                except Exception:
                    metrics = []
            # 输入指标数量保护
            try:
                from app.config.settings import config as _config

                _max_input = int(getattr(_config.rca, "input_metrics_max", 80) or 80)
            except Exception:
                _max_input = 80
            _metrics_eff = list(metrics)[: max(1, _max_input)]
            result = await analyzer.analyze_correlations(
                params["start_time"],
                params["end_time"],
                target_metric,
                _metrics_eff,
                namespace=params.get("namespace"),
            )
        elif job_type == "timeline":
            # 生成时间线：可选传入 events；若未提供则自动拉取 K8s 事件
            try:
                events = params.get("events") or []
                if not events:
                    try:
                        ns_eff = params.get("namespace")
                        events = await K8sEventsCollector(namespace=ns_eff).pull(limit=200)
                    except Exception:
                        events = []
                # 尝试轻量异常与相关性以富集时间线（使用默认指标，受 input_metrics_max 限制）
                metrics = params.get("metrics")
                if not metrics:
                    try:
                        from app.config.settings import config as _config
                        metrics = _config.rca.default_metrics
                    except Exception:
                        metrics = []
                # 输入指标限制
                try:
                    from app.config.settings import config as _config
                    _max_input = int(getattr(_config.rca, "input_metrics_max", 80) or 80)
                except Exception:
                    _max_input = 80
                _metrics_eff = list(metrics)[: max(1, _max_input)]
                anomalies = await analyzer.detect_anomalies(
                    params["start_time"], params["end_time"], _metrics_eff
                )
                correlations = await analyzer.analyze_correlations(
                    params["start_time"], params["end_time"], None, _metrics_eff, namespace=params.get("namespace")
                )
            except Exception:
                events, anomalies, correlations = [], {}, {}

            # 构造时间线
            try:
                timeline = await analyzer.generate_timeline(
                    params["start_time"],
                    params["end_time"],
                    events,
                    anomalies=anomalies,
                    correlations=correlations,
                    cross_correlations={},
                    logs=None,
                )
            except Exception:
                timeline = []

            result = {
                "timeline": timeline,
                "period": {
                    "start": analyzer._jsonify(params.get("start_time")) if hasattr(analyzer, "_jsonify") else str(params.get("start_time")),
                    "end": analyzer._jsonify(params.get("end_time")) if hasattr(analyzer, "_jsonify") else str(params.get("end_time")),
                },
            }

        else:
            result = await analyzer.analyze(
                params["start_time"],
                params["end_time"],
                params.get("metrics"),
                include_logs=params.get("include_logs"),
                include_traces=None,
                namespace=params.get("namespace"),
            )

        # 即将完成
        _update_job_status(job_id, progress=0.9)

        # 按类型写入结果记录
        try:
            with session_scope() as session:
                if job_type == "cross_correlation":
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
                        metrics=json.dumps(
                            params.get("metrics") or [], ensure_ascii=False
                        ),
                        params_json=json.dumps(_jsonify(params), ensure_ascii=False),
                        status="success",
                        summary=summary_text,
                        result_json=json.dumps(_jsonify(result), ensure_ascii=False),
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
                            metrics=json.dumps(
                                params.get("metrics") or [], ensure_ascii=False
                            ),
                            params_json=json.dumps(
                                _jsonify(params), ensure_ascii=False
                            ),
                            job_id=job_id,
                            status="success",
                            summary=summary_text,
                            result_json=json.dumps(
                                _jsonify(result), ensure_ascii=False
                            ),
                            error=None,
                        )
                        session.add(rc)
                    except Exception:
                        pass
                elif job_type == "correlation":
                    summary_text = None
                    try:
                        if isinstance(result, dict) and len(result) == 1:
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
                        metrics=json.dumps(
                            params.get("metrics") or [], ensure_ascii=False
                        ),
                        params_json=json.dumps(_jsonify(params), ensure_ascii=False),
                        status="success",
                        summary=summary_text,
                        result_json=json.dumps(_jsonify(result), ensure_ascii=False),
                        error=None,
                    )
                    session.add(corr_rec)
                    try:
                        rc = RCARecord(
                            record_type="correlation",
                            namespace=params.get("namespace"),
                            start_time=str(params.get("start_time")),
                            end_time=str(params.get("end_time")),
                            metrics=json.dumps(
                                params.get("metrics") or [], ensure_ascii=False
                            ),
                            params_json=json.dumps(
                                _jsonify(params), ensure_ascii=False
                            ),
                            job_id=job_id,
                            status="success",
                            summary=summary_text,
                            result_json=json.dumps(
                                _jsonify(result), ensure_ascii=False
                            ),
                            error=None,
                        )
                        session.add(rc)
                    except Exception:
                        pass
                elif job_type == "timeline":
                    # 写入统一记录表
                    try:
                        rc = RCARecord(
                            record_type="timeline",
                            namespace=params.get("namespace"),
                            start_time=str(params.get("start_time")),
                            end_time=str(params.get("end_time")),
                            metrics=None,
                            params_json=json.dumps(
                                {"events_count": len(events or [])}, ensure_ascii=False
                            ),
                            job_id=job_id,
                            status="success",
                            summary=None,
                            result_json=json.dumps(_jsonify(result), ensure_ascii=False),
                            error=None,
                        )
                        session.add(rc)
                    except Exception:
                        pass
                else:
                    summary_text = None
                    try:
                        summary_text = (result or {}).get("summary")
                    except Exception:
                        summary_text = None
                    analysis = RCAAnalysisRecord(
                        start_time=str(params.get("start_time")),
                        end_time=str(params.get("end_time")),
                        metrics=json.dumps(
                            params.get("metrics") or [], ensure_ascii=False
                        ),
                        namespace=params.get("namespace"),
                        service_name=None,
                        status="success",
                        summary=summary_text,
                        result_json=json.dumps(_jsonify(result), ensure_ascii=False),
                        error=None,
                    )
                    session.add(analysis)
                    try:
                        rc = RCARecord(
                            record_type="analysis",
                            namespace=params.get("namespace"),
                            start_time=str(params.get("start_time")),
                            end_time=str(params.get("end_time")),
                            metrics=json.dumps(
                                params.get("metrics") or [], ensure_ascii=False
                            ),
                            params_json=json.dumps(
                                _jsonify(params), ensure_ascii=False
                            ),
                            job_id=job_id,
                            status="success",
                            summary=summary_text,
                            result_json=json.dumps(
                                _jsonify(result), ensure_ascii=False
                            ),
                            error=None,
                        )
                        session.add(rc)
                    except Exception:
                        pass
                    # 同步：将分析结果中的时间线也落入统一记录表，便于 /rca/timelines/list 查询
                    try:
                        timeline_data = (result or {}).get("timeline")
                        if isinstance(timeline_data, list):
                            tl_result = {
                                "timeline": timeline_data,
                                "period": {
                                    "start": str(params.get("start_time")),
                                    "end": str(params.get("end_time")),
                                },
                            }
                            tl_rec = RCARecord(
                                record_type="timeline",
                                namespace=params.get("namespace"),
                                start_time=str(params.get("start_time")),
                                end_time=str(params.get("end_time")),
                                metrics=None,
                                params_json=json.dumps(
                                    {"events_count": len(timeline_data) if timeline_data else 0},
                                    ensure_ascii=False,
                                ),
                                job_id=job_id,
                                status="success",
                                summary=None,
                                result_json=json.dumps(_jsonify(tl_result), ensure_ascii=False),
                                error=None,
                            )
                            session.add(tl_rec)
                    except Exception:
                        pass
        except Exception:
            # 写结果表失败不影响主流程
            pass

        # 成功
        _update_job_status(job_id, status="success", progress=1.0, result=result)
        logger.info(f"RCA 任务成功完成: {job_id}")
        return result

    except Exception as e:
        logger.exception(f"RCA 任务执行失败: {job_id}")
        _update_job_status(job_id, status="error", progress=1.0, error=str(e))
        # 同步写入分表失败记录（尽力而为）
        try:
            job_type = (params or {}).get("job_type") or "analysis"
            with session_scope() as session:
                if job_type == "cross_correlation":
                    cc_rec = RCACorrelationRecord(
                        job_id=job_id,
                        record_type="cross_correlation",
                        namespace=params.get("namespace"),
                        start_time=str(params.get("start_time")),
                        end_time=str(params.get("end_time")),
                        metrics=json.dumps(
                            params.get("metrics") or [], ensure_ascii=False
                        ),
                        params_json=json.dumps(_jsonify(params), ensure_ascii=False),
                        status="error",
                        summary=None,
                        result_json=None,
                        error=str(e),
                    )
                    session.add(cc_rec)
                    try:
                        rc = RCARecord(
                            record_type="cross_correlation",
                            namespace=params.get("namespace"),
                            start_time=str(params.get("start_time")),
                            end_time=str(params.get("end_time")),
                            metrics=json.dumps(
                                params.get("metrics") or [], ensure_ascii=False
                            ),
                            params_json=json.dumps(
                                _jsonify(params), ensure_ascii=False
                            ),
                            job_id=job_id,
                            status="error",
                            summary=None,
                            result_json=None,
                            error=str(e),
                        )
                        session.add(rc)
                    except Exception:
                        pass
                elif job_type == "correlation":
                    corr_rec = RCASimpleCorrelationRecord(
                        job_id=job_id,
                        record_type="correlation",
                        namespace=params.get("namespace"),
                        start_time=str(params.get("start_time")),
                        end_time=str(params.get("end_time")),
                        metrics=json.dumps(
                            params.get("metrics") or [], ensure_ascii=False
                        ),
                        params_json=json.dumps(_jsonify(params), ensure_ascii=False),
                        status="error",
                        summary=None,
                        result_json=None,
                        error=str(e),
                    )
                    session.add(corr_rec)
                    try:
                        rc = RCARecord(
                            record_type="correlation",
                            namespace=params.get("namespace"),
                            start_time=str(params.get("start_time")),
                            end_time=str(params.get("end_time")),
                            metrics=json.dumps(
                                params.get("metrics") or [], ensure_ascii=False
                            ),
                            params_json=json.dumps(
                                _jsonify(params), ensure_ascii=False
                            ),
                            job_id=job_id,
                            status="error",
                            summary=None,
                            result_json=None,
                            error=str(e),
                        )
                        session.add(rc)
                    except Exception:
                        pass
                else:
                    analysis = RCAAnalysisRecord(
                        start_time=str(params.get("start_time")),
                        end_time=str(params.get("end_time")),
                        metrics=json.dumps(
                            params.get("metrics") or [], ensure_ascii=False
                        ),
                        namespace=params.get("namespace"),
                        service_name=None,
                        status="error",
                        summary=None,
                        result_json=None,
                        error=str(e),
                    )
                    session.add(analysis)
                    try:
                        rc = RCARecord(
                            record_type="analysis",
                            namespace=params.get("namespace"),
                            start_time=str(params.get("start_time")),
                            end_time=str(params.get("end_time")),
                            metrics=json.dumps(
                                params.get("metrics") or [], ensure_ascii=False
                            ),
                            params_json=json.dumps(
                                _jsonify(params), ensure_ascii=False
                            ),
                            job_id=job_id,
                            status="error",
                            summary=None,
                            result_json=None,
                            error=str(e),
                        )
                        session.add(rc)
                    except Exception:
                        pass
        except Exception:
            pass

        return {"error": str(e)}


@rca_huey.task()
def rca_execute_job(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """统一执行 RCA 任务（事件循环感知）。

    - 若当前线程已有事件循环（例如在 FastAPI 的请求处理协程中，且 immediate 模式）：
      使用后台线程执行完整任务，避免在主事件循环中执行大量CPU/IO计算造成阻塞。
    - 否则：直接使用 asyncio.run 执行完整流程（Huey worker / 普通线程）。
    """
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # 在独立后台线程中运行，线程内自建事件循环
            import threading

            def _runner():
                try:
                    asyncio.run(_rca_execute_job_async(job_id, params))
                except Exception:
                    logger.exception(f"RCA 任务后台线程执行失败: {job_id}")

            t = threading.Thread(
                target=_runner, name=f"rca-job-{job_id[:8]}", daemon=True
            )
            t.start()
            return {"status": "scheduled", "job_id": job_id, "mode": "thread"}
        else:
            return asyncio.run(_rca_execute_job_async(job_id, params))

    except Exception as e:
        logger.exception(f"RCA 任务调度/执行失败: {job_id}")
        _update_job_status(job_id, status="error", progress=1.0, error=str(e))
        return {"error": str(e)}


__all__ = ["rca_execute_job"]
