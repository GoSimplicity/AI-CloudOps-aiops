#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.core.agents.assistant.models.base import SessionData


class SessionManager:
    """会话管理器"""

    def __init__(self):
        self.sessions: Dict[str, SessionData] = {}
        self._session_lock = threading.Lock()

    def create_session(self) -> str:
        """创建新会话"""
        session_id = str(uuid.uuid4())
        session_data = SessionData(
            session_id=session_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            history=[],
            metadata={},
            context_summary="",
        )

        with self._session_lock:
            self.sessions[session_id] = session_data

        return session_id

    def get_session(self, session_id: str) -> Optional[SessionData]:
        """获取会话数据"""
        return self.sessions.get(session_id)

    def get_history(self, session_id: str) -> List[Dict]:
        """获取会话历史（兼容上层接口）。"""
        session = self.get_session(session_id)
        return session.history if session else []

    def add_message_to_history(self, session_id: str, role: str, content: str) -> str:
        """添加消息到会话历史"""
        if session_id not in self.sessions:
            session_id = self.create_session()

        with self._session_lock:
            session = self.sessions[session_id]
            session.history.append(
                {
                    "role": role,
                    "content": content,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

            # 限制历史长度
            max_history = 20  # 减少历史长度
            if len(session.history) > max_history:
                session.history = session.history[-max_history:]

            # 更新上下文摘要
            if len(session.history) >= 4:
                session.context_summary = self._generate_context_summary(
                    session.history[-4:]
                )

        return session_id

    def _generate_context_summary(self, history: List[Dict]) -> str:
        """生成对话上下文摘要"""
        try:
            user_messages = [
                msg["content"] for msg in history if msg.get("role") == "user"
            ]
            all_text = " ".join(user_messages)

            keywords = []
            for word in ["部署", "监控", "故障", "性能", "配置", "安装"]:
                if word in all_text:
                    keywords.append(word)

            return f"对话主题: {', '.join(keywords)}" if keywords else "一般咨询"
        except Exception:
            return "一般咨询"

    def clear_session_history(self, session_id: str) -> bool:
        """清空会话历史"""
        if session_id in self.sessions:
            with self._session_lock:
                self.sessions[session_id].history = []
                self.sessions[session_id].context_summary = ""
            return True
        return False

    # 兼容别名
    def clear_history(self, session_id: str) -> bool:
        return self.clear_session_history(session_id)

    def get_all_sessions(self) -> Dict[str, SessionData]:
        """获取所有会话"""
        return self.sessions.copy()

    def clear_all_sessions(self):
        """清空所有会话"""
        with self._session_lock:
            self.sessions.clear()

    def get_session_count(self) -> int:
        """获取会话数量"""
        return len(self.sessions)
