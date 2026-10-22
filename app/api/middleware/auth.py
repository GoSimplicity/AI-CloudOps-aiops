"""Service-to-service authentication shared by the API and MCP server."""

import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config.settings import config


def setup_auth(app: FastAPI) -> None:
    api_key = config.security.api_key
    if config.env.lower() == "production" and len(api_key) < 32:
        raise ValueError("Production requires AIOPS_API_KEY with at least 32 characters")

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        # Liveness contains no dependency details and must work for orchestrator probes.
        if request.url.path == "/health" and request.method in {"GET", "HEAD"}:
            return await call_next(request)
        if api_key:
            supplied = request.headers.get("X-API-Key", "")
            if not supplied:
                scheme, _, credential = request.headers.get("Authorization", "").partition(" ")
                if scheme.lower() == "bearer":
                    supplied = credential
            if not secrets.compare_digest(supplied.encode(), api_key.encode()):
                return JSONResponse(
                    status_code=401,
                    content={"code": 401, "message": "Invalid or missing service API key", "data": None},
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)
