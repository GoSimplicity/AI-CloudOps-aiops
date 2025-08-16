#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-CloudOps 智能运维平台
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: RCA 规则引擎
"""

from __future__ import annotations

from typing import Dict, List


class RuleEngine:
    """极简规则引擎占位实现。"""

    def __init__(self):
        self.rules: List[Dict] = []

    def load_builtin(self):
        """加载内置规则（简版）"""
        self.rules = [
            {
                "name": "CrashLoopBackOff",
                "event_reasons": ["BackOff", "CrashLoopBackOff"],
            },
            {
                "name": "ImagePullBackOff",
                "event_reasons": ["ErrImagePull", "ImagePullBackOff"],
            },
            {"name": "ProbeFailed", "event_reasons": ["Unhealthy", "ProbeError"]},
            {"name": "OOMKilled", "event_messages": ["OOMKilled", "out of memory"]},
        ]

    def evaluate(self, context: Dict) -> List[Dict]:
        """对上下文执行规则评估，返回命中证据列表。"""
        evidence: List[Dict] = []
        events = context.get("events") or []
        try:
            for ev in events:
                reason = str(ev.get("reason") or "").lower()
                message = str(ev.get("message") or "").lower()
                for rule in self.rules:
                    reasons = [r.lower() for r in rule.get("event_reasons", [])]
                    msgs = [m.lower() for m in rule.get("event_messages", [])]
                    hit = (reasons and any(r in reason for r in reasons)) or (
                        msgs and any(m in message for m in msgs)
                    )
                    if hit:
                        evidence.append(
                            {
                                "rule": rule["name"],
                                "reason": ev.get("reason"),
                                "message": ev.get("message"),
                                "namespace": ev.get("namespace"),
                                "first_timestamp": ev.get("first_timestamp"),
                                "last_timestamp": ev.get("last_timestamp"),
                            }
                        )
                        break
        except Exception:
            pass

        return evidence
