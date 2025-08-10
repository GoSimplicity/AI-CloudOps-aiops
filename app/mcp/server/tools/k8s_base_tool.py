#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops K8s工具基类
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Kubernetes工具的基类，提供通用的Kubernetes操作功能
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from kubernetes import client, config

from app.utils.time_utils import iso_utc_now

from ..mcp_server import BaseTool

UTC_TZ = timezone.utc


class K8sBaseTool(BaseTool):
    """Kubernetes工具基类，提供通用功能"""

    def __init__(self, name: str, description: str):
        super().__init__(name, description)
        self._api_clients = None
        self._executor = ThreadPoolExecutor(max_workers=3)

    def _initialize_clients(
        self, config_path: Optional[str] = None
    ) -> Dict[str, client.ApiClient]:
        """初始化Kubernetes API客户端 - 统一实现"""
        if self._api_clients:
            return self._api_clients

        try:
            if config_path and os.path.exists(config_path):
                config.load_kube_config(config_file=config_path)
            elif os.path.exists(os.path.expanduser("~/.kube/config")):
                config.load_kube_config()
            else:
                config.load_incluster_config()

            self._api_clients = self._create_api_clients()
            return self._api_clients

        except Exception as e:
            raise Exception(f"无法加载Kubernetes配置: {str(e)}") from e

    def _create_api_clients(self) -> Dict[str, client.ApiClient]:
        """子类重写此方法来创建特定的API客户端"""
        return {"v1": client.CoreV1Api()}

    async def execute(self, parameters: Dict[str, Any]) -> Any:
        """统一的执行方法，包含超时处理"""
        try:
            return await asyncio.wait_for(
                self._execute_internal(parameters), timeout=60.0
            )
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": "操作超时",
                "message": f"{self.name}执行时间超过60秒，已中止操作。",
                "timestamp": iso_utc_now(),
            }
        except Exception as e:
            return {
                "success": False,
                "error": "执行失败",
                "message": str(e),
                "timestamp": iso_utc_now(),
            }

    async def _execute_internal(self, parameters: Dict[str, Any]) -> Any:
        """子类必须实现的内部执行逻辑"""
        raise NotImplementedError("子类必须实现 _execute_internal 方法")

    @staticmethod
    def _calculate_age(creation_timestamp) -> str:
        """计算资源年龄 - 统一实现"""
        if not creation_timestamp:
            return "Unknown"

        # 使用 UTC 计算年龄
        age = (datetime.now(UTC_TZ).replace(tzinfo=creation_timestamp.tzinfo) - creation_timestamp)
        days = age.days
        hours, remainder = divmod(age.seconds, 3600)
        minutes, _ = divmod(remainder, 60)

        if days > 0:
            return f"{days}d{hours}h"
        elif hours > 0:
            return f"{hours}h{minutes}m"
        else:
            return f"{minutes}m"

    def __del__(self):
        """统一的资源清理"""
        if hasattr(self, "_executor") and self._executor:
            try:
                self._executor.shutdown(wait=False)
            except Exception:
                pass
