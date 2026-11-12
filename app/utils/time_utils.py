#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 时间工具类
"""

import calendar
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from app.config.settings import config


class TimeUtils:
    """时间工具"""

    @classmethod
    def _get_holidays(cls) -> set:
        return set(config.time.holidays)

    @staticmethod
    def extract_time_features(timestamp: datetime) -> Dict[str, float | int | bool]:
        # 将时间转换为分钟
        minutes = timestamp.hour * 60 + timestamp.minute

        # 计算时间周期性特征
        sin_time = np.sin(2 * np.pi * minutes / 1440)  # 1440分钟 = 24小时
        cos_time = np.cos(2 * np.pi * minutes / 1440)

        # 周几特征 (0是周一，6是周日)
        day_of_week = timestamp.weekday()

        # 判断是否是周末
        is_weekend = day_of_week >= 5

        # 判断是否是工作时间 (工作日9点到17点)
        is_business_hour = (9 <= timestamp.hour <= 17) and not is_weekend

        # 判断是否是节假日
        date_key = timestamp.strftime("%m%d")
        holidays = TimeUtils._get_holidays()
        is_holiday = date_key in holidays

        # 获取月份信息和日期信息
        month = timestamp.month
        day = timestamp.day

        # 计算月份周期性特征
        sin_month = np.sin(2 * np.pi * month / 12)
        cos_month = np.cos(2 * np.pi * month / 12)

        # 判断是否是月初/月末
        is_month_start = day == 1
        days_in_month = calendar.monthrange(timestamp.year, timestamp.month)[1]
        is_month_end = day == days_in_month

        # 返回所有特征
        return {
            "sin_time": sin_time,
            "cos_time": cos_time,
            "hour": timestamp.hour,
            "minute": timestamp.minute,
            "day_of_week": day_of_week,
            "sin_day": np.sin(2 * np.pi * day_of_week / 7),
            "cos_day": np.cos(2 * np.pi * day_of_week / 7),
            "is_weekend": is_weekend,
            "is_business_hour": is_business_hour,
            "is_holiday": is_holiday,
            "month": month,
            "day": day,
            "sin_month": sin_month,
            "cos_month": cos_month,
            "is_month_start": is_month_start,
            "is_month_end": is_month_end,
        }

    @staticmethod
    def validate_time_range(
        start_time: datetime, end_time: datetime, max_range_minutes: int = 1440
    ) -> bool:
        """验证时间范围。没有时区的时间无法判断是否在未来，按无效范围返回 False。"""
        if start_time.tzinfo is None or start_time.tzinfo.utcoffset(start_time) is None:
            return False
        if end_time.tzinfo is None or end_time.tzinfo.utcoffset(end_time) is None:
            return False
        if start_time >= end_time:
            return False
        time_diff = (end_time - start_time).total_seconds() / 60
        if time_diff > max_range_minutes:
            return False
        # 检查是否是未来时间
        now = datetime.now(timezone.utc)
        if start_time > now or end_time > now:
            return False
        return True

    @staticmethod
    def resample_dataframe(df: pd.DataFrame, freq: str = "1T") -> pd.DataFrame:
        """重采样时间序列数据"""
        if df.empty:
            return df

        # 确保索引是时间类型
        if not isinstance(df.index, pd.DatetimeIndex):
            return df

        # 重采样并前向填充
        return df.resample(freq).mean().fillna(method="ffill")

    @staticmethod
    def get_time_windows(
        start_time: datetime, end_time: datetime, window_size_minutes: int = 5
    ) -> List[Tuple[datetime, datetime]]:
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
    def format_duration(seconds: float) -> str:
        """格式化持续时间"""
        if seconds < 60:
            return f"{seconds:.1f}秒"
        elif seconds < 3600:
            return f"{seconds / 60:.1f}分钟"
        else:
            return f"{seconds / 3600:.1f}小时"
