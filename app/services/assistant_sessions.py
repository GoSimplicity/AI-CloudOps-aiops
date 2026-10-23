"""单进程会话归属、闲置到期与并发控制，不以随机 ID 替代授权"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
import uuid

from app.common.constants import ServiceConstants
from app.common.exceptions import SessionAccessError
from app.common.identity import require_principal


@dataclass
class OwnedSession:
    owner: str
    mode: int
    last_access: float
    created_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_activity: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    message_count: int = 0
    revision: int = 0
    pending: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


    @asynccontextmanager
    async def turn(self) -> AsyncIterator[None]:
        if self.pending >= 4:
            raise SessionAccessError(429, "当前会话正在处理较多请求，请稍后重试")
        self.pending += 1
        try:
            async with self.lock:
                yield
        finally:
            self.pending -= 1


class AssistantSessions:
    def __init__(
        self, max_sessions: int = 1000, max_per_principal: int = 50,
        ttl_seconds: float = ServiceConstants.ASSISTANT_SESSION_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        on_expire: Callable[[str, OwnedSession], None] | None = None,
    ) -> None:
        self.sessions: dict[str, OwnedSession] = {}
        self.user_sessions: dict[tuple[str, int], str] = {}
        self.max_sessions = max_sessions
        self.max_per_principal = max_per_principal
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.on_expire = on_expire

    def _remove(self, session_id: str) -> None:
        session = self.sessions.pop(session_id)
        self.user_sessions = {key: sid for key, sid in self.user_sessions.items() if sid != session_id}
        if self.on_expire:
            self.on_expire(session_id, session)

    def cleanup_expired(self) -> None:
        now = self.clock()
        for sid, session in list(self.sessions.items()):
            if now - session.last_access >= self.ttl_seconds and not session.lock.locked() and session.pending == 0:
                self._remove(sid)

    def discard(self, session_id: str) -> None:
        principal = require_principal()
        session = self.sessions.get(session_id)
        if session is None:
            return
        if session.owner != principal:
            raise SessionAccessError(403, "无权访问此会话")
        self._remove(session_id)

    def clear(self) -> None:
        for sid in list(self.sessions):
            self._remove(sid)

    def create(self, mode: int) -> str:
        principal = require_principal()
        self.cleanup_expired()
        count = sum(session.owner == principal for session in self.sessions.values())
        if count >= self.max_per_principal or len(self.sessions) >= self.max_sessions:
            raise SessionAccessError(429, "会话数量达到限制，请复用会话或等待闲置会话过期")
        session_id = str(uuid.uuid4())
        self.sessions[session_id] = OwnedSession(owner=principal, mode=mode, last_access=self.clock())
        return session_id

    def get(self, session_id: str) -> OwnedSession:
        principal = require_principal()
        session = self.sessions.get(session_id)
        if session is None:
            raise SessionAccessError(404, "会话不存在或已失效，请创建新会话")
        if session.owner != principal:
            raise SessionAccessError(403, "无权访问此会话")
        if self.clock() - session.last_access >= self.ttl_seconds and not session.lock.locked() and session.pending == 0:
            self._remove(session_id)
            raise SessionAccessError(404, "会话已过期，请创建新会话")
        session.last_access = self.clock()
        session.last_activity = datetime.now(timezone.utc).isoformat()
        return session

    def info(self, session_id: str) -> dict[str, str | int | bool]:
        session = self.get(session_id)
        return {
            "success": True, "session_id": session_id, "created_time": session.created_time,
            "last_activity": session.last_activity, "message_count": session.message_count,
            "mode": session.mode, "status": "active",
        }
