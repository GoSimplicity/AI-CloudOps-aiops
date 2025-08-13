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

        # 在线程池中执行任务，避免阻塞请求线程
        self._executor.submit(self._run_job_safely, job_id, params)

        logger.info(f"RCA 任务已提交: {job_id}")
        return job_id

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """查询任务状态/结果。"""
        return self._load(job_id)

    # ----------------------------- 内部方法 ----------------------------- #

    def _run_job_safely(self, job_id: str, params: Dict[str, Any]):
        """执行任务并确保状态持久化。"""
        try:
            self._update(job_id, {"status": "running", "progress": 0.05})

            analyzer = RCAAnalyzer()

            # 进度提示：数据采集阶段
            self._update(job_id, {"progress": 0.2})

            # 执行分析（在工作线程中创建事件循环执行异步分析）
            result = asyncio.run(
                analyzer.analyze(
                    params["start_time"], params["end_time"], params.get("metrics")
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
            logger.info(f"RCA 任务成功完成: {job_id}")

        except Exception as e:
            logger.exception(f"RCA 任务执行失败: {job_id}")
            self._update(job_id, {"status": "failed", "error": str(e), "progress": 1.0})

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
