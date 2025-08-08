#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
解释器：将分析证据整理为对人友好的报告（占位实现）
"""

from __future__ import annotations

from typing import Dict


def format_explanation(result: Dict) -> Dict:
    """占位：返回传入结果并预留扩展字段。"""
    if not isinstance(result, dict):
        return {"summary": "invalid result"}
    result.setdefault("evidence", [])
    result.setdefault("timeline", [])
    result.setdefault("impact_scope", [])
    result.setdefault("suggestions", [])
    return result
