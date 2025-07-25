#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Prometheus服务模块 - 提供监控数据查询、时间序列数据获取和指标分析功能
"""

import logging
import requests
import pandas as pd
from datetime import datetime
from typing import List, Dict, Optional, Any
from app.config.settings import config

logger = logging.getLogger("aiops.prometheus")

class PrometheusService:
    def __init__(self):
        self.base_url = config.prometheus.url
        self.timeout = config.prometheus.timeout
        logger.info(f"初始化Prometheus服务: {self.base_url}")
    
    async def query_range(
        self, 
        query: str, 
        start_time: datetime, 
        end_time: datetime, 
        step: str = "1m"
    ) -> Optional[pd.DataFrame]:
        """查询Prometheus范围数据"""
        try:
            url = f"{self.base_url}/api/v1/query_range"
            params = {
                "query": query,
                "start": int(start_time.timestamp()),
                "end": int(end_time.timestamp()),
                "step": step
            }
            
            logger.debug(f"查询Prometheus: {query}")
            response = requests.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            
            data = response.json()
            if data["status"] != "success":
                logger.warning(f"Prometheus查询失败: {query}, 状态: {data.get('status', 'unknown')}")
                return None
            
            if not data["data"]["result"]:
                logger.warning(f"Prometheus查询无结果: {query}, 时间范围: {start_time} - {end_time}")
                return None
            
            logger.debug(f"Prometheus查询成功: {query}, 结果数: {len(data['data']['result'])}")
            
            # 处理多个时间序列
            all_series = []
            for result in data["data"]["result"]:
                if not result.get('values'):
                    continue
                    
                timestamps = [datetime.utcfromtimestamp(float(val[0])) for val in result['values']]
                values = []
                
                for val in result['values']:
                    try:
                        values.append(float(val[1]))
                    except (ValueError, TypeError):
                        values.append(0.0)
                
                series_df = pd.DataFrame({
                    'value': values
                }, index=pd.DatetimeIndex(timestamps))
                
                # 添加标签信息
                labels = result.get('metric', {})
                for label, value in labels.items():
                    series_df[f'label_{label}'] = value
                
                all_series.append(series_df)
            
            if all_series:
                # 合并所有时间序列
                combined_df = pd.concat(all_series, ignore_index=False)
                # 重采样到指定频率
                numeric_cols = combined_df.select_dtypes(include=['number']).columns
                if len(numeric_cols) > 0:
                    resampled = combined_df[numeric_cols].resample('1min').mean()
                    
                    # 处理标签列（字符串类型）
                    label_cols = [col for col in combined_df.columns if col.startswith('label_')]
                    for col in label_cols:
                        resampled[col] = combined_df[col].resample('1min').first()
                    
                    # 前向填充缺失值
                    return resampled.ffill()
                else:
                    logger.warning("没有数值列可用于重采样")
                    return None
            
            return None
            
        except requests.exceptions.Timeout:
            logger.error(f"Prometheus查询超时: {query}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Prometheus请求失败: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"查询Prometheus失败: {str(e)}", exc_info=True)
            return None
    
    async def query_instant(self, query: str, timestamp: Optional[datetime] = None) -> Optional[List[Dict]]:
        """查询Prometheus即时数据"""
        try:
            url = f"{self.base_url}/api/v1/query"
            params = {"query": query}
            
            if timestamp:
                params["time"] = timestamp.timestamp()
            
            logger.debug(f"即时查询Prometheus: {query}")
            response = requests.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            
            data = response.json()
            if data["status"] != "success" or not data["data"]["result"]:
                logger.warning(f"Prometheus即时查询无结果: {query}")
                return None
            
            return data["data"]["result"]
            
        except Exception as e:
            logger.error(f"Prometheus即时查询失败: {str(e)}")
            return None
    
    async def get_available_metrics(self) -> List[str]:
        """获取可用的监控指标"""
        try:
            url = f"{self.base_url}/api/v1/label/__name__/values"
            response = requests.get(url, timeout=self.timeout)
            response.raise_for_status()
            
            data = response.json()
            if data["status"] == "success":
                metrics = sorted(data["data"])
                logger.info(f"获取到 {len(metrics)} 个可用指标")
                return metrics
            
            return []
            
        except Exception as e:
            logger.error(f"获取可用指标失败: {str(e)}")
            return []
    
    def is_healthy(self) -> bool:
        """检查Prometheus健康状态"""
        try:
            url = f"{self.base_url}/-/healthy"
            response = requests.get(url, timeout=5)
            is_healthy = response.status_code == 200
            logger.debug(f"Prometheus健康状态: {is_healthy}")
            return is_healthy
        except Exception as e:
            logger.error(f"Prometheus健康检查失败: {str(e)}")
            return False
    
    async def get_metric_metadata(self, metric_name: str) -> Optional[Dict[str, Any]]:
        """获取指标元数据"""
        try:
            url = f"{self.base_url}/api/v1/metadata"
            params = {"metric": metric_name}
            
            response = requests.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            
            data = response.json()
            if data["status"] == "success" and data["data"]:
                return data["data"].get(metric_name, [{}])[0]
            
            return None
            
        except Exception as e:
            logger.error(f"获取指标元数据失败: {str(e)}")
            return None