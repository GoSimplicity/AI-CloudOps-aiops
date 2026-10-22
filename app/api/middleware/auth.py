"""Service-to-service authentication shared by HTTP and WebSocket endpoints."""

import secrets

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.config.settings import config


class ServiceAuthMiddleware:
    def __init__(self, app, api_key: str):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        public_health = scope["type"] == "http" and scope["path"] == "/health" and scope["method"] in {"GET", "HEAD"}
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
        await self.app(scope, receive, send)


def setup_auth(app: FastAPI) -> None:
    api_key = config.security.api_key
    if config.env.lower() == "production" and len(api_key) < 32:
        raise ValueError("Production requires AIOPS_API_KEY with at least 32 characters")
    app.add_middleware(ServiceAuthMiddleware, api_key=api_key)
