"""Service-to-service authentication shared by HTTP and WebSocket endpoints."""

import re
import secrets

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.common.identity import INTERNAL_PRINCIPAL, PRINCIPAL_HEADER, principal_scope
from app.config.settings import config


class ServiceAuthMiddleware:
    def __init__(self, app, api_key: str):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        public_health = scope["type"] == "http" and scope["path"] == "/health" and scope["method"] in {"GET", "HEAD"}
        principal = None
        if self.api_key and not public_health:
            headers = Headers(scope=scope)
            supplied = headers.get("X-API-Key", "")
            if not supplied:
                scheme, _, credential = headers.get("Authorization", "").partition(" ")
                if scheme.lower() == "bearer":
                    supplied = credential
            if not secrets.compare_digest(supplied.encode(), self.api_key.encode()):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                response = JSONResponse(
                    status_code=401,
                    content={"code": 401, "message": "Invalid or missing service API key", "data": None},
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
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
    if config.env.lower() == "production" and len(api_key) < 32:
        raise ValueError("Production requires AIOPS_API_KEY with at least 32 characters")
    app.add_middleware(ServiceAuthMiddleware, api_key=api_key)
