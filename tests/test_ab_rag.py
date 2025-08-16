#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 测试：test_ab_rag
"""

# A/B sampling benchmark for RAG pipeline
# This is not a strict test; it prints simple metrics for manual inspection.

import json
import time
from typing import Dict, Any

from fastapi.testclient import TestClient

from app.main import app
from app.config.settings import config

client = TestClient(app)

QUESTIONS = {
    "overview": "请用简洁语言介绍一下AI-CloudOps平台的架构与核心能力",
    "troubleshoot": "Pod 一直 CrashLoopBackOff 应该如何排查？",
    "deployment": "如何部署平台并接入 Prometheus 进行监控？",
}


def _post_chat(q: str) -> Dict[str, Any]:
    payload = {"query": q, "mode": 1}
    resp = client.post("/api/v1/assistant/chat", json=payload)
    return resp.json()


def _reinit():
    client.post("/api/v1/assistant/reinitialize")


def _set_rag_cfg(hybrid: bool, reranker: bool):
    # Toggle in-memory config and reinitialize assistant
    try:
        setattr(config.rag, "hybrid_enabled", bool(hybrid))
        setattr(config.rag, "reranker_enabled", bool(reranker))
    except Exception:
        pass
    _reinit()


def run_round(name: str):
    print(f"\n=== ROUND: {name} ===")
    rows = []
    for key, q in QUESTIONS.items():
        start = time.time()
        data = _post_chat(q)
        elapsed = time.time() - start
        try:
            ans = data.get("data", {}).get("response", "")
            conf = data.get("data", {}).get("confidence", 0)
        except Exception:
            ans, conf = "", 0
        rows.append(
            {
                "topic": key,
                "len": len(ans),
                "confidence": conf,
                "time": round(elapsed, 2),
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def test_ab_sampling():
    # Baseline (current settings)
    _set_rag_cfg(hybrid=False, reranker=False)
    run_round("baseline")
    # Hybrid+Reranker
    _set_rag_cfg(hybrid=True, reranker=True)
    run_round("hybrid+reranker")
