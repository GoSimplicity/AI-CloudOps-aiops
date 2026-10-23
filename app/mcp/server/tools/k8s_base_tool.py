#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Kubernetes基础工具
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import logging
import os
import threading
from typing import Any, Dict, Optional

from kubernetes import client, config

from ..mcp_server import BaseTool

logger = logging.getLogger("aiops.mcp.k8s")

DEFAULT_CLIENT_SOURCE = "default"


class K8sToolInputError(ValueError):
    """消息为固定文本、可直接返回给调用方的 K8s 工具错误"""


class K8sBaseTool(BaseTool):
    """K8s工具基类"""

    def __init__(self, name: str, description: str):
        super().__init__(name, description)
        self._api_clients = None
        # SEC-33 P1-1：客户端与首次初始化时的配置来源（kubeconfig realpath 或 default）绑定
        self._client_source = None
        self._client_source_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=3)

    @staticmethod
    def _resolve_client_source(config_path: Any) -> Optional[str]:
        """None/空串表示默认配置；显式路径必须是存在的文件，绝不静默回退到其他集群"""
        if config_path is None or config_path == "":
            return None
        if not isinstance(config_path, str) or not os.path.isfile(config_path):
            raise K8sToolInputError("显式kubeconfig必须是存在的文件，拒绝回退到其他集群")
        return config_path

    def _initialize_clients(self, config_path: Optional[str] = None) -> Dict[str, Any]:
        config_path = self._resolve_client_source(config_path)
        source = os.path.realpath(config_path) if config_path is not None else DEFAULT_CLIENT_SOURCE
        with self._client_source_lock:
            if self._api_clients:
                if self._client_source != source:
                    raise K8sToolInputError("K8s工具已绑定首次配置，不能在请求中切换集群")
                return self._api_clients
            api_client = self._new_api_client(config_path)
            try:
                clients = self._create_api_clients(api_client)
            except Exception as error:
                api_client.close()
                logger.error("%s 创建Kubernetes API客户端失败: 错误类型=%s", self.name, type(error).__name__)
                logger.debug("%s 创建Kubernetes API客户端失败详情", self.name, exc_info=True)
                raise K8sToolInputError("无法创建Kubernetes API客户端") from None
            self._api_clients = clients
            self._client_source = source
            return clients

    def _new_api_client(self, config_path: Optional[str]) -> client.ApiClient:
        """独立 Configuration 加载 kubeconfig，不修改 kubernetes 全局默认配置"""
        configuration = client.Configuration()
        try:
            if config_path is not None:
                config.load_kube_config(config_file=config_path, client_configuration=configuration)
            elif os.path.exists(os.path.expanduser("~/.kube/config")):
                config.load_kube_config(client_configuration=configuration)
            else:
                config.load_incluster_config(client_configuration=configuration)
        except Exception as error:
            logger.error("%s 加载Kubernetes配置失败: 错误类型=%s", self.name, type(error).__name__)
            logger.debug("%s 加载Kubernetes配置失败详情", self.name, exc_info=True)
            raise K8sToolInputError("无法加载Kubernetes配置") from None
        return client.ApiClient(configuration=configuration)

    def _create_api_clients(self, api_client: client.ApiClient) -> Dict[str, Any]:
        """子类重写此方法，用绑定配置的 api_client 创建特定的API客户端"""
        return {"v1": client.CoreV1Api(api_client)}

    async def execute(self, parameters: Dict[str, Any]) -> Any:
        """统一的执行方法，包含超时处理"""
        from app.common.constants import ServiceConstants

        try:
            return await asyncio.wait_for(
                self._execute_internal(parameters),
                timeout=ServiceConstants.AUTOFIX_WORKFLOW_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": "操作超时",
                "outcome_unknown": True,
                "retry_safe": False,
                "message": f"{self.name}执行时间超过{ServiceConstants.AUTOFIX_WORKFLOW_TIMEOUT}秒，底层请求可能仍在执行，请核对实际状态，勿盲目重试。",
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
        except K8sToolInputError as e:
            return {
                "success": False,
                "error": "执行失败",
                "message": str(e),
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
        except Exception as e:
            # SEC-33 L2 P3-4：未知异常只返回通用信息，类型进 ERROR，原始文本只进 DEBUG
            logger.error("%s 执行失败: 错误类型=%s", self.name, type(e).__name__)
            logger.debug("%s 执行失败详情", self.name, exc_info=True)
            return {
                "success": False,
                "error": "执行失败",
                "message": "K8s工具执行失败",
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

    async def _execute_internal(self, parameters: Dict[str, Any]) -> Any:
        """子类必须实现的内部执行逻辑"""
        raise NotImplementedError("子类必须实现 _execute_internal 方法")

    @staticmethod
    def _calculate_age(creation_timestamp) -> str:
        """计算资源年龄 - 统一实现"""
        if not creation_timestamp:
            return "Unknown"

        age = (
            datetime.utcnow().replace(tzinfo=creation_timestamp.tzinfo)
            - creation_timestamp
        )
        days = age.days
        hours, remainder = divmod(age.seconds, 3600)
        minutes, _ = divmod(remainder, 60)

        if days > 0:
            return f"{days}d{hours}h"
        elif hours > 0:
            return f"{hours}h{minutes}m"
        else:
            return f"{minutes}m"

    async def cleanup(self) -> None:
        """停止待执行任务并关闭归属本工具的客户端"""
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        clients, self._api_clients = self._api_clients, None
        self._client_source = None
        closed = set()
        for value in (clients or {}).values():
            api_client = getattr(value, "api_client", value)
            if id(api_client) in closed or not callable(getattr(api_client, "close", None)):
                continue
            closed.add(id(api_client))
            await asyncio.to_thread(api_client.close)

    def __del__(self):
        """统一的资源清理"""
        if hasattr(self, "_executor") and self._executor:
            try:
                self._executor.shutdown(wait=False)
            except Exception:
                pass
