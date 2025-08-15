#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 分页工具
"""

from typing import Any, Dict, List, Optional, Tuple


def apply_search_filter(
    items: List[Dict[str, Any]], search: Optional[str], search_fields: List[str]
) -> List[Dict[str, Any]]:
    """
    对列表数据应用搜索过滤

    Args:
        items: 原始数据列表
        search: 搜索关键词
        search_fields: 搜索字段列表（在这些字段中进行模糊匹配）

    Returns:
        过滤后的数据列表
    """
    if not search or not search.strip():
        return items

    search_lower = search.strip().lower()
    filtered_items = []

    for item in items:
        # 在指定字段中搜索
        if search_fields:
            for field in search_fields:
                field_value = item.get(field, "")
                if isinstance(field_value, str) and search_lower in field_value.lower():
                    filtered_items.append(item)
                    break
        # 如果没有指定字段，在所有字符串字段中搜索
        else:
            for value in item.values():
                if isinstance(value, str) and search_lower in value.lower():
                    filtered_items.append(item)
                    break

    return filtered_items


def apply_pagination(items: List[Any], page: int, size: int) -> Tuple[List[Any], int]:
    """
    对列表数据应用分页

    Args:
        items: 原始数据列表
        page: 页码（从1开始）
        size: 每页大小

    Returns:
        分页后的数据列表和总记录数
    """
    total = len(items)

    # 计算偏移量
    offset = (page - 1) * size

    # 获取当前页数据
    paginated_items = items[offset : offset + size]

    return paginated_items, total


def validate_pagination_params(
    page: Optional[int], size: Optional[int]
) -> Tuple[int, int]:
    """
    验证并标准化分页参数

    Args:
        page: 页码
        size: 每页大小

    Returns:
        验证后的页码和每页大小

    Raises:
        ValueError: 参数无效时抛出异常
    """
    # 设置默认值
    if page is None:
        page = 1
    if size is None:
        size = 20

    # 验证参数范围
    if page < 1:
        raise ValueError("页码必须大于等于1")
    if size < 1:
        raise ValueError("每页大小必须大于等于1")
    if size > 100:
        raise ValueError("每页大小不能超过100")

    return page, size


def process_list_with_pagination_and_search(
    items: List[Dict[str, Any]],
    page: Optional[int] = None,
    size: Optional[int] = None,
    search: Optional[str] = None,
    search_fields: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    综合处理列表数据的搜索和分页

    Args:
        items: 原始数据列表
        page: 页码
        size: 每页大小
        search: 搜索关键词
        search_fields: 搜索字段列表

    Returns:
        处理后的数据列表和总记录数
    """
    # 验证分页参数
    page, size = validate_pagination_params(page, size)

    # 应用搜索过滤
    if search_fields is None:
        search_fields = []
    filtered_items = apply_search_filter(items, search, search_fields)

    # 应用分页
    paginated_items, total = apply_pagination(filtered_items, page, size)

    return paginated_items, total
