#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
分页和搜索工具模块
提供列表数据的分页和搜索功能
"""

import math
from typing import Any, Dict, List, Optional, Tuple

from app.models.response_models import PaginationInfo


def apply_search_filter(items: List[Dict[str, Any]], search: Optional[str], search_fields: List[str]) -> List[Dict[str, Any]]:
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


def apply_pagination(
    items: List[Any], 
    page: int, 
    size: int
) -> Tuple[List[Any], PaginationInfo]:
    """
    对列表数据应用分页
    
    Args:
        items: 原始数据列表
        page: 页码（从1开始）
        size: 每页大小
    
    Returns:
        分页后的数据列表和分页信息
    """
    total = len(items)
    pages = math.ceil(total / size) if size > 0 else 0
    
    # 计算偏移量
    offset = (page - 1) * size
    
    # 获取当前页数据
    paginated_items = items[offset:offset + size]
    
    # 创建分页信息
    pagination_info = PaginationInfo(
        page=page,
        size=size,
        total=total,
        pages=pages,
        has_next=page < pages,
        has_prev=page > 1
    )
    
    return paginated_items, pagination_info


def validate_pagination_params(page: Optional[int], size: Optional[int]) -> Tuple[int, int]:
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
    search_fields: Optional[List[str]] = None
) -> Tuple[List[Dict[str, Any]], PaginationInfo]:
    """
    综合处理列表数据的搜索和分页
    
    Args:
        items: 原始数据列表
        page: 页码
        size: 每页大小
        search: 搜索关键词
        search_fields: 搜索字段列表
    
    Returns:
        处理后的数据列表和分页信息
    """
    # 验证分页参数
    page, size = validate_pagination_params(page, size)
    
    # 应用搜索过滤
    if search_fields is None:
        search_fields = []
    filtered_items = apply_search_filter(items, search, search_fields)
    
    # 应用分页
    paginated_items, pagination_info = apply_pagination(filtered_items, page, size)
    
    return paginated_items, pagination_info