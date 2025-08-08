#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
智能查询重写器 - 提升检索召回率
"""

import re
import logging
from typing import List

logger = logging.getLogger("aiops.assistant.query_rewriter")


class QueryRewriter:
    """智能查询重写器，提升检索召回率"""

    def __init__(self):
        self.synonyms = {
            "部署": ["安装", "配置", "搭建", "建立"],
            "监控": ["观察", "跟踪", "检测", "巡检"],
            "故障": ["异常", "错误", "问题", "失败"],
            "性能": ["效率", "速度", "响应", "吞吐"],
            "日志": ["记录", "日志文件", "log", "审计"],
            "告警": ["报警", "警告", "提醒", "通知"],
            "自动化": ["自动", "自动执行", "批量"],
            "运维": ["ops", "运营", "维护", "管理"],
        }

    def expand_query(self, query: str) -> List[str]:
        """扩展查询，生成多个相关查询变体"""
        expanded_queries = [query]  # 原始查询

        # 1. 同义词替换 - 增加更多变体
        for word, synonyms in self.synonyms.items():
            if word in query:
                for synonym in synonyms[:3]:  # 增加同义词数量
                    expanded_queries.append(query.replace(word, synonym))

        # 2. 关键词提取和重组
        keywords = self._extract_keywords(query)
        if len(keywords) >= 2:
            # 关键词组合
            expanded_queries.append(" ".join(keywords))
            # 部分关键词组合
            if len(keywords) >= 3:
                expanded_queries.append(" ".join(keywords[:2]))
                expanded_queries.append(" ".join(keywords[-2:]))

        # 3. 添加语义相关的扩展
        if "部署" in query or "安装" in query:
            expanded_queries.extend(["配置方法", "安装步骤", "部署指南"])
        elif "监控" in query:
            expanded_queries.extend(["监控配置", "指标采集", "告警设置"])
        elif "故障" in query or "错误" in query:
            expanded_queries.extend(["问题排查", "故障诊断", "错误解决"])
        elif "性能" in query:
            expanded_queries.extend(["性能调优", "优化方法", "性能分析"])

        # 4. 去重并适当增加数量
        unique_queries = list(dict.fromkeys(expanded_queries))[:8]  # 增加查询数量
        return unique_queries

    def _extract_keywords(self, text: str) -> List[str]:
        """提取关键词"""
        stopwords = {
            "的",
            "和",
            "或",
            "是",
            "在",
            "有",
            "如何",
            "什么",
            "怎么",
            "为什么",
        }
        words = re.findall(r"\w+", text)
        keywords = [w for w in words if len(w) > 1 and w not in stopwords]
        return keywords[:3]  # 减少关键词数量
