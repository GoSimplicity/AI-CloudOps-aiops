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
from ..write_gate import classify, has_custom_config_path
from .kubeconfig_policy import CONFIG_PATH_UNAVAILABLE_MESSAGE, KubeconfigRejected, load_custom_kubeconfig

logger = logging.getLogger("aiops.mcp.k8s")

DEFAULT_CLIENT_SOURCE = "default"


class K8sToolInputError(ValueError):
    """消息为固定文本、可直接返回给调用方的 K8s 工具错误"""


class K8sBaseTool(BaseTool):
    """K8s工具基类"""

    def __init__(self, name: str, description: str):
        super().__init__(name, description)
        # SEC-46：默认来源（~/.kube/config 或 in-cluster）的客户端单独保存，读写都可用
        self._api_clients = None
        # SEC-46：白名单内自定义 kubeconfig 的客户端按 realpath 缓存，只读
        self._custom_clients: Dict[str, Dict[str, Any]] = {}
        self._client_source_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=3)

    def _initialize_clients(self, config_path: Optional[str] = None) -> Dict[str, Any]:
        """None/空串 → 默认来源；其它取值必须通过 kubeconfig_policy 准入，绝不回退到默认来源"""
        if config_path is None or config_path == "":
            with self._client_source_lock:
                if not self._api_clients:
                    self._api_clients = self._build_clients(self._new_api_client(None))
                return self._api_clients
        try:
            source, document = load_custom_kubeconfig(config_path)
        except KubeconfigRejected as rejected:
            logger.warning("%s 拒绝 config_path: %s", self.name, rejected.reason)
            raise K8sToolInputError(CONFIG_PATH_UNAVAILABLE_MESSAGE) from None
        with self._client_source_lock:
            cached = self._custom_clients.get(source)
            if cached:
                return cached
            try:
                api_client = self._new_custom_api_client(document)
            except K8sToolInputError:
                raise K8sToolInputError(CONFIG_PATH_UNAVAILABLE_MESSAGE) from None
            clients = self._build_clients(api_client)
            self._custom_clients[source] = clients
            return clients

    def _build_clients(self, api_client: client.ApiClient) -> Dict[str, Any]:
        try:
            return self._create_api_clients(api_client)
        except Exception as error:
            api_client.close()
            logger.error("%s 创建Kubernetes API客户端失败: 错误类型=%s", self.name, type(error).__name__)
            logger.debug("%s 创建Kubernetes API客户端失败详情", self.name, exc_info=True)
            raise K8sToolInputError("无法创建Kubernetes API客户端") from None

    def _new_api_client(self, config_path: Optional[str]) -> client.ApiClient:
        """默认来源：独立 Configuration 加载 ~/.kube/config 或 in-cluster，不修改 kubernetes 全局默认配置。

        config_path 参数仅为兼容保留：自定义来源一律走 _new_custom_api_client（已通过准入的 dict）。
        """
        if config_path is not None:
            raise K8sToolInputError(CONFIG_PATH_UNAVAILABLE_MESSAGE)
        configuration = client.Configuration()
        try:
            if os.path.exists(os.path.expanduser("~/.kube/config")):
                config.load_kube_config(client_configuration=configuration)
            else:
                config.load_incluster_config(client_configuration=configuration)
        except Exception as error:
            logger.error("%s 加载Kubernetes配置失败: 错误类型=%s", self.name, type(error).__name__)
            logger.debug("%s 加载Kubernetes配置失败详情", self.name, exc_info=True)
            raise K8sToolInputError("无法加载Kubernetes配置") from None
        return client.ApiClient(configuration=configuration)

    def _new_custom_api_client(self, document: Dict[str, Any]) -> client.ApiClient:
        """自定义来源：只从已解析、已检查过 exec/auth-provider 的 dict 加载，不再按路径读文件；不回写文件"""
        configuration = client.Configuration()
        try:
            config.load_kube_config_from_dict(document, client_configuration=configuration, persist_config=False)
        except Exception as error:
            logger.error("%s 加载自定义kubeconfig失败: 错误类型=%s", self.name, type(error).__name__)
            logger.debug("%s 加载自定义kubeconfig失败详情", self.name, exc_info=True)
            raise K8sToolInputError(CONFIG_PATH_UNAVAILABLE_MESSAGE) from None
        return client.ApiClient(configuration=configuration)

    @staticmethod
    def _custom_source_is_read_only(tool_name: str, parameters: Any) -> bool:
        """SEC-46：带自定义 config_path 的请求只允许纯读（不含 Secret 读）；门禁之外的纵深防护"""
        if not isinstance(parameters, dict) or not has_custom_config_path(parameters):
            return True
        return classify(tool_name, parameters).kind == "read"

    def _create_api_clients(self, api_client: client.ApiClient) -> Dict[str, Any]:
        """子类重写此方法，用绑定配置的 api_client 创建特定的API客户端"""
        return {"v1": client.CoreV1Api(api_client)}

    async def execute(self, parameters: Dict[str, Any]) -> Any:
        """统一的执行方法，包含超时处理"""
        from app.common.constants import ServiceConstants

        if not self._custom_source_is_read_only(self.name, parameters):
            return {
                "success": False,
                "error": "执行失败",
                "message": CONFIG_PATH_UNAVAILABLE_MESSAGE,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
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
        with self._client_source_lock:
            default_clients, self._api_clients = self._api_clients, None
            custom_clients, self._custom_clients = self._custom_clients, {}
        closed = set()
        values = [value for clients in [default_clients or {}, *custom_clients.values()] for value in clients.values()]
        for value in values:
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
