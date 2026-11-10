"""Service-to-service authentication shared by HTTP and WebSocket endpoints."""

import ipaddress
import logging
import re
import secrets
from typing import Any

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.common.identity import INTERNAL_PRINCIPAL, PRINCIPAL_HEADER, principal_scope
from app.config.envmode import is_production
from app.config.settings import config

# 空密钥不再等于关闭鉴权。非生产环境只允许回环绑定和回环来源；生产环境仍要求至少 32 字符。
EMPTY_KEY_PUBLIC_BIND_MESSAGE = (
    "AIOPS_API_KEY is empty, so this process can only bind to a loopback address. Refusing to start."
)
_AUTH_FAILURE = {"code": 401, "message": "Invalid or missing service API key", "data": None}


def is_loopback_host(host: Any) -> bool:
    """字面回环地址。不解析 DNS，避免把任意主机名当成本机。"""
    if not isinstance(host, str):
        return False
    value = host.strip().lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1].strip()
    value = value.split("%", 1)[0].strip()
    if value == "localhost":
        return True
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return False
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        parsed = mapped
    return bool(parsed.is_loopback)


def _peer_host(scope) -> Any:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return client[0]
    return None


def ensure_service_auth_configuration(env: Any, api_key: Any, host: Any) -> None:
    """禁止「没配密钥、又监听非回环地址」。生产环境另外要求至少 32 字符。"""
    if is_production(env):
        if not isinstance(api_key, str) or len(api_key) < 32:
            raise ValueError("Production requires AIOPS_API_KEY with at least 32 characters")
        return
    if isinstance(api_key, str) and api_key:
        return
    if is_loopback_host(host):
        return
    raise ValueError(EMPTY_KEY_PUBLIC_BIND_MESSAGE)


async def _deny_service_auth(scope, receive, send) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    response = JSONResponse(status_code=401, content=_AUTH_FAILURE, headers={"WWW-Authenticate": "Bearer"})
    await response(scope, receive, send)


class ServiceAuthMiddleware:
    def __init__(self, app, api_key: str):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        public_health = scope["type"] == "http" and scope["path"] == "/health" and scope["method"] in {"GET", "HEAD"}
        principal = None
        if not public_health and not self.api_key:
            if not is_loopback_host(_peer_host(scope)):
                await _deny_service_auth(scope, receive, send)
                return
        elif self.api_key and not public_health:
            headers = Headers(scope=scope)
            supplied = headers.get("X-API-Key", "")
            if not supplied:
                scheme, _, credential = headers.get("Authorization", "").partition(" ")
                if scheme.lower() == "bearer":
                    supplied = credential
            if not isinstance(self.api_key, str) or not secrets.compare_digest(supplied.encode(), self.api_key.encode()):
                await _deny_service_auth(scope, receive, send)
                return
            user_ids = headers.getlist(PRINCIPAL_HEADER)
            if user_ids:
                if len(user_ids) != 1 or re.fullmatch(r"[1-9][0-9]{0,18}", user_ids[0]) is None:
                    if scope["type"] == "websocket":
                        await send({"type": "websocket.close", "code": 1008})
                    else:
                        await JSONResponse(status_code=401, content={"code": 401, "message": "Invalid trusted user identity", "data": None})(scope, receive, send)
                    return
                principal = f"user:{user_ids[0]}"
            else:
                principal = INTERNAL_PRINCIPAL
        with principal_scope(principal):
            await self.app(scope, receive, send)


def setup_auth(app: FastAPI) -> None:
    api_key = config.security.api_key
    ensure_service_auth_configuration(config.env, api_key, config.host)
    if not is_production(config.env) and not (isinstance(api_key, str) and api_key):
        logging.getLogger("aiops").warning("AIOPS_API_KEY is empty; only loopback clients can call authenticated routes")
    app.add_middleware(ServiceAuthMiddleware, api_key=api_key)
