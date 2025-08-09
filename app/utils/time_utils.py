#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 时间工具模块 - 时间处理工具函数
"""

import calendar
from datetime import datetime, timedelta, timezone

import pandas as pd


class TimeUtils:
    """时间相关的工具函数"""

    # 北京时区
    BEIJING_TZ = timezone(timedelta(hours=8))
    
    # 简化版节假日（仅包含主要节假日）
    HOLIDAYS = {
        "0101": True, "0102": True, "0103": True,  # 元旦
        "0501": True, "0502": True, "0503": True,  # 劳动节
        "1001": True, "1002": True, "1003": True,  # 国庆节
    }

    @classmethod
    def now_beijing(cls) -> datetime:
        """获取当前北京时间"""
        return datetime.now(cls.BEIJING_TZ)

    @classmethod
    def utc_to_beijing(cls, utc_dt: datetime) -> datetime:
        """将UTC时间转换为北京时间"""
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        return utc_dt.astimezone(cls.BEIJING_TZ)

    @classmethod
    def beijing_to_utc(cls, beijing_dt: datetime) -> datetime:
        """将北京时间转换为UTC时间"""
        if beijing_dt.tzinfo is None:
            beijing_dt = beijing_dt.replace(tzinfo=cls.BEIJING_TZ)
        return beijing_dt.astimezone(timezone.utc)

    @classmethod
    def is_business_hour(cls, timestamp: datetime) -> bool:
        """判断是否为工作时间"""
        hour = timestamp.hour
        weekday = timestamp.weekday()
        # 周一到周五的9-18点为工作时间
        return 0 <= weekday <= 4 and 9 <= hour <= 18

    @classmethod
    def is_weekend(cls, timestamp: datetime) -> bool:
        """判断是否为周末"""
        return timestamp.weekday() >= 5

    @classmethod
    def is_holiday(cls, timestamp: datetime) -> bool:
        """判断是否为节假日"""
        date_key = timestamp.strftime("%m%d")
        return cls.HOLIDAYS.get(date_key, False)

    @classmethod
    def get_time_features(cls, timestamp: datetime) -> dict:
        """提取时间特征"""
        hour = timestamp.hour
        day_of_week = timestamp.weekday()
        month = timestamp.month
        day = timestamp.day
        
        # 基本特征
        return {
            "hour": hour,
            "day_of_week": day_of_week,
            "month": month,
            "day": day,
            "is_weekend": cls.is_weekend(timestamp),
            "is_business_hour": cls.is_business_hour(timestamp),
            "is_holiday": cls.is_holiday(timestamp),
            "is_month_start": day <= 3,
            "is_month_end": day >= calendar.monthrange(timestamp.year, month)[1] - 2,
        }

    @staticmethod
    def format_duration(seconds: float) -> str:
        """格式化持续时间"""
        if seconds < 60:
            return f"{seconds:.1f}秒"
        elif seconds < 3600:
            return f"{seconds / 60:.1f}分钟"
        else:
            return f"{seconds / 3600:.1f}小时"

    @staticmethod
    def get_time_windows(
        start_time: datetime, end_time: datetime, window_size_minutes: int = 5
    ) -> list:
        """获取时间窗口列表"""
        windows = []
        current = start_time
        window_delta = timedelta(minutes=window_size_minutes)

        while current < end_time:
            window_end = min(current + window_delta, end_time)
            windows.append((current, window_end))
            current = window_end

        return windows

    @staticmethod
    def resample_dataframe(df: pd.DataFrame, freq: str = "1T") -> pd.DataFrame:
        """重采样时间序列数据"""
        if df.empty or not isinstance(df.index, pd.DatetimeIndex):
            return df
        return df.resample(freq).mean().fillna(method="ffill")