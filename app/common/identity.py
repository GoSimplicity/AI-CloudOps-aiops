"""服务认证后的可信主体与请求级会话作用域"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import json
from typing import Optional

from app.common.exceptions import SessionAccessError

PRINCIPAL_HEADER = "X-CloudOps-User-ID"
INTERNAL_PRINCIPAL = "service:internal"
principal_context: ContextVar[Optional[str]] = ContextVar("cloudops_principal", default=None)
conversation_context: ContextVar[Optional[tuple[str, str, int]]] = ContextVar("cloudops_conversation", default=None)


@contextmanager
def principal_scope(principal: Optional[str]) -> Iterator[None]:
    token = principal_context.set(principal)
    try:
        yield
    finally:
        principal_context.reset(token)


def require_principal() -> str:
    principal = principal_context.get()
    if principal is None:
        raise SessionAccessError(401, "缺少可信调用身份")
    return principal


def cache_identity(session_id: Optional[str] = None, history: object | None = None) -> str:
    """底层直接内部调用使用独立服务主体，浏览器主体由鉴权中间件建立"""
    return json.dumps(
        [principal_context.get() or INTERNAL_PRINCIPAL, session_id, conversation_context.get(), history],
        ensure_ascii=False, sort_keys=True, default=str,
    )
