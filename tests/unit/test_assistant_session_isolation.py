"""持服务密钥的网关身份与会话归属必须同时生效"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.middleware.auth import setup_auth
from app.api.routes import assistant
from app.config.settings import config
from app.core.agents.enterprise_assistant import RAGAssistant
from app.services.assistant_service import OptimizedAssistantService

KEY = "offline-session-service-key-32-characters"


@pytest.fixture
def session_client(monkeypatch):
    store = SimpleNamespace(collection_name="offline")
    rag = RAGAssistant(store, None)
    rag.graph = SimpleNamespace(ainvoke=AsyncMock(return_value={"answer": "private answer", "confidence": 0.9, "sources": []}))
    service = OptimizedAssistantService()
    service._initialized = True
    service._assistant = rag
    monkeypatch.setattr(assistant, "get_assistant_service", AsyncMock(return_value=service))
    monkeypatch.setattr(config.security, "api_key", KEY)
    app = FastAPI()
    setup_auth(app)
    app.include_router(assistant.router, prefix="/assistant")
    with TestClient(app) as client:
        yield client, service, rag


def headers(user=None):
    result = {"X-API-Key": KEY}
    if user is not None:
        result["X-CloudOps-User-ID"] = str(user)
    return result


@pytest.mark.parametrize("reader", ["2", None])
def test_known_foreign_session_is_not_readable_or_writable(session_client, reader):
    client, _, rag = session_client
    created = client.post("/assistant/session", headers=headers("1"))
    sid = created.json()["data"]["session_id"]
    assert client.get(f"/assistant/session/{sid}", headers=headers("1")).status_code == 200
    assert client.get(f"/assistant/session/{sid}", headers=headers(reader)).status_code == 403
    response = client.post("/assistant/query", headers=headers(reader), json={"question": "continue", "session_id": sid})
    assert response.status_code == 403
    rag.graph.ainvoke.assert_not_awaited()


def test_ownerless_legacy_and_unknown_sessions_are_not_adopted(session_client):
    client, _, rag = session_client
    legacy = "user_1_legacy-predictable"
    rag.context_manager.update_context(legacy, {"recent_queries": ["private legacy history"]})
    for sid in [legacy, "unknown-session"]:
        assert client.get(f"/assistant/session/{sid}", headers=headers("1")).status_code == 404
        response = client.post("/assistant/query", headers=headers("1"), json={"question": "continue", "session_id": sid})
        assert response.status_code == 404
    rag.graph.ainvoke.assert_not_awaited()


def test_body_user_id_cannot_select_another_principal(session_client):
    client, _, _ = session_client
    response = client.post("/assistant/query", headers=headers("1"), json={"question": "hello", "user_id": "2"})
    assert response.status_code == 200
    sid = response.json()["data"]["session_id"]
    assert client.get(f"/assistant/session/{sid}", headers=headers("2")).status_code == 403
    assert client.get(f"/assistant/session/{sid}", headers=headers("1")).status_code == 200


def test_identity_header_is_not_trusted_without_service_key(session_client):
    client, _, _ = session_client
    response = client.post("/assistant/session", headers={"X-CloudOps-User-ID": "1"})
    assert response.status_code == 401


def test_stream_rejects_known_foreign_session(session_client):
    client, _, rag = session_client
    sid = client.post("/assistant/session", headers=headers("1")).json()["data"]["session_id"]
    with client.websocket_connect("/assistant/stream", headers=headers("2")) as stream:
        stream.send_json({"question": "continue private conversation", "session_id": sid})
        response = stream.receive_json()
        assert response["success"] is False
        assert response["data"]["code"] == 403
        assert stream.receive()["code"] == 1008
    rag.graph.ainvoke.assert_not_awaited()


class AnswerCache:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, **kwargs):
        self.data[key] = value


def test_same_question_isolated_between_users_sessions_and_history(session_client):
    client, _, rag = session_client
    rag.cache_manager = AnswerCache()
    rag.graph.ainvoke.side_effect = [
        {"answer": answer, "confidence": 0.9, "sources": []}
        for answer in ["first user private answer", "second user answer", "first user other session", "new history answer"]
    ]
    first = client.post("/assistant/query", headers=headers("1"), json={"question": "same"}).json()["data"]
    second = client.post("/assistant/query", headers=headers("2"), json={"question": "same"}).json()["data"]
    other = client.post("/assistant/query", headers=headers("1"), json={"question": "same"}).json()["data"]
    next_turn = client.post("/assistant/query", headers=headers("1"), json={"question": "same", "session_id": first["session_id"]}).json()["data"]
    assert [item["answer"] for item in [first, second, other, next_turn]] == ["first user private answer", "second user answer", "first user other session", "new history answer"]
    assert rag.graph.ainvoke.await_count == 4


@pytest.mark.parametrize("identity", ["0", "-1", "01", "user:1", "1,2", ""])
def test_malformed_gateway_identity_is_rejected(session_client, identity):
    client, _, _ = session_client
    assert client.post("/assistant/session", headers=headers(identity)).status_code == 401


def test_internal_identity_is_an_owner_not_a_user_bypass(session_client):
    client, _, _ = session_client
    sid = client.post("/assistant/session", headers=headers()).json()["data"]["session_id"]
    assert client.get(f"/assistant/session/{sid}", headers=headers()).status_code == 200
    assert client.get(f"/assistant/session/{sid}", headers=headers("1")).status_code == 403


def test_stream_selects_only_public_protocol_and_keeps_owner_check(session_client):
    client, _, _ = session_client
    sid = client.post("/assistant/session", headers=headers("1")).json()["data"]["session_id"]
    with client.websocket_connect("/assistant/stream", headers=headers("1"), subprotocols=["cloudops-v1", "cloudops-auth.not-selected"]) as stream:
        assert stream.accepted_subprotocol == "cloudops-v1"
        stream.send_json({"question": "hello", "session_id": sid})
        assert stream.receive_json()["success"] is True


async def test_two_concurrent_principals_do_not_share_contextvars(session_client):
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from app.common.identity import INTERNAL_PRINCIPAL, conversation_context, principal_context

    client, _, rag = session_client
    ready = asyncio.Event()
    observations = []

    async def run_graph(*args, **kwargs):
        before = principal_context.get()
        observations.append(before)
        if len(observations) == 2:
            ready.set()
        await ready.wait()
        after = principal_context.get()
        assert after == before
        assert conversation_context.get()[0] == before
        return {"answer": before, "confidence": 0.9, "sources": []}

    rag.graph.ainvoke.side_effect = run_graph
    async with AsyncClient(transport=ASGITransport(app=client.app), base_url="http://offline") as http:
        first, second = await asyncio.wait_for(asyncio.gather(*[
            http.post("/assistant/query", headers=headers(user), json={"question": "same"}) for user in ["1", "2"]
        ]), timeout=2)
    assert first.json()["data"]["answer"] == "user:1"
    assert second.json()["data"]["answer"] == "user:2"
    assert principal_context.get() == INTERNAL_PRINCIPAL
    assert conversation_context.get() is None


async def test_legacy_user_helper_cannot_impersonate_and_ids_are_not_derived(session_client):
    from app.common.exceptions import SessionAccessError
    from app.common.identity import principal_scope

    _, service, _ = session_client
    with principal_scope("user:1"):
        first = await service.create_or_get_session("1")
        again = await service.create_or_get_session("1")
        assert first["session_id"] == again["session_id"]
        assert not first["session_id"].startswith("user_")
        with pytest.raises(SessionAccessError) as error:
            await service.create_or_get_session("2")
        assert error.value.status_code == 403
    with principal_scope("user:2"):
        with pytest.raises(SessionAccessError):
            await service.get_session_info(first["session_id"])


async def test_mcp_history_cannot_be_reused_by_another_principal():
    from app.common.identity import principal_scope
    from app.mcp.mcp_client import MCPAssistant

    assistant = object.__new__(MCPAssistant)
    assistant._history = {}
    assistant._max_history_turns = 6
    with principal_scope("user:1"):
        assistant._append_history_pair("known-session", "private question", "private answer")
        assert "private answer" in assistant._build_history_block("known-session")
    with principal_scope("user:2"):
        assert assistant._build_history_block("known-session") == ""


async def test_same_session_concurrent_turns_are_serialized(session_client):
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from app.common.identity import conversation_context

    client, _, rag = session_client
    sid = client.post("/assistant/session", headers=headers("1")).json()["data"]["session_id"]
    first_entered = asyncio.Event()
    release = asyncio.Event()
    revisions = []

    async def graph(*args, **kwargs):
        revision = conversation_context.get()[2]
        revisions.append(revision)
        first_entered.set()
        await release.wait()
        return {"answer": str(revision), "confidence": 0.9, "sources": []}

    rag.graph.ainvoke.side_effect = graph
    rag.cache_manager = AnswerCache()
    async with AsyncClient(transport=ASGITransport(app=client.app), base_url="http://offline") as http:
        first = asyncio.create_task(http.post("/assistant/query", headers=headers("1"), json={"question": "same", "session_id": sid}))
        await asyncio.wait_for(first_entered.wait(), timeout=2)
        second = asyncio.create_task(http.post("/assistant/query", headers=headers("1"), json={"question": "same", "session_id": sid}))
        await asyncio.sleep(0)
        assert revisions == [0]
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
    assert revisions == [0, 1]
    assert [response.json()["data"]["answer"] for response in responses] == ["0", "1"]


async def test_session_quota_ttl_and_active_lock_have_bounded_lifetimes():
    from app.common.exceptions import SessionAccessError
    from app.common.identity import principal_scope
    from app.services.assistant_sessions import AssistantSessions

    now = [0.0]
    removed = []
    sessions = AssistantSessions(max_sessions=2, max_per_principal=1, ttl_seconds=10, clock=lambda: now[0], on_expire=lambda sid, entry: removed.append(sid))
    with principal_scope("user:1"):
        first = sessions.create(1)
        with pytest.raises(SessionAccessError) as error:
            sessions.create(1)
        assert error.value.status_code == 429
        active = sessions.get(first)
    with principal_scope("user:2"):
        second = sessions.create(1)
    with principal_scope("user:3"):
        with pytest.raises(SessionAccessError) as error:
            sessions.create(1)
        assert error.value.status_code == 429
    await active.lock.acquire()
    now[0] = 11
    sessions.cleanup_expired()
    assert first in sessions.sessions and second not in sessions.sessions
    active.lock.release()
    sessions.cleanup_expired()
    assert not sessions.sessions
    assert set(removed) == {first, second}


async def test_cancelled_turn_releases_owner_lock_and_allows_next_turn(session_client):
    import asyncio

    from app.common.identity import conversation_context, principal_scope

    _, service, rag = session_client
    started = asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    rag.graph.ainvoke.side_effect = blocked
    with principal_scope("user:1"):
        created = await service.create_session(SimpleNamespace(mode=1))
        sid = created["session_id"]
        task = asyncio.create_task(service.get_answer("blocked", session_id=sid))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not service._sessions.get(sid).lock.locked()
        assert conversation_context.get() is None
        rag.graph.ainvoke.side_effect = None
        rag.graph.ainvoke.return_value = {"answer": "next turn", "confidence": 0.9, "sources": []}
        assert (await service.get_answer("next", session_id=sid))["answer"] == "next turn"


def test_duplicate_identity_headers_are_rejected(session_client):
    client, _, _ = session_client
    response = client.post("/assistant/session", headers=[("X-API-Key", KEY), ("X-CloudOps-User-ID", "1"), ("X-CloudOps-User-ID", "2")])
    assert response.status_code == 401


def test_development_without_service_key_does_not_trust_claimed_identity(session_client, monkeypatch):
    client, _, _ = session_client
    monkeypatch.setattr(config.security, "api_key", "")
    app = FastAPI()
    setup_auth(app)
    app.include_router(assistant.router, prefix="/assistant")
    response = TestClient(app).post("/assistant/session", headers={"X-CloudOps-User-ID": "1"})
    assert response.status_code == 401


async def test_expired_session_removes_context_checkpoint_and_mcp_history(session_client, monkeypatch):
    from unittest.mock import Mock

    from langgraph.checkpoint.memory import MemorySaver

    from app.common.identity import principal_scope
    from app.services.factory import ServiceFactory

    _, service, rag = session_client
    with principal_scope("user:1"):
        sid = (await service.create_session(SimpleNamespace(mode=1)))["session_id"]
        entry = service._sessions.get(sid)
    rag.context_manager.update_context(sid, {"recent_queries": ["private"]})
    checkpoint = MemorySaver()
    checkpoint.delete_thread = Mock(wraps=checkpoint.delete_thread)
    rag.graph.checkpointer = checkpoint
    history = {f"user:1:{sid}": [{"q": "private", "a": "private"}]}
    monkeypatch.setattr(ServiceFactory, "peek", lambda name: SimpleNamespace(_mcp_assistant=SimpleNamespace(_history=history)))
    service._sessions.clock = lambda: entry.last_access + service._sessions.ttl_seconds + 1
    service._sessions.cleanup_expired()
    assert sid not in service._sessions.sessions
    assert sid not in rag.context_manager.session_contexts
    assert not history
    checkpoint.delete_thread.assert_called_once_with(f"user:1:{sid}")


async def test_single_session_pending_requests_are_bounded_and_cancel_cleanly(session_client):
    import asyncio

    from app.common.exceptions import SessionAccessError
    from app.common.identity import principal_scope

    _, service, rag = session_client
    started = asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    rag.graph.ainvoke.side_effect = blocked
    tasks = []
    with principal_scope("user:1"):
        sid = (await service.create_session(SimpleNamespace(mode=1)))["session_id"]
        try:
            tasks.append(asyncio.create_task(service.get_answer("first", session_id=sid)))
            await started.wait()
            tasks.extend(asyncio.create_task(service.get_answer("queued", session_id=sid)) for _ in range(3))
            await asyncio.sleep(0)
            entry = service._sessions.get(sid)
            assert entry.pending == 4
            with pytest.raises(SessionAccessError) as error:
                await service.get_answer("too many", session_id=sid)
            assert error.value.status_code == 429
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert entry.pending == 0
        assert not entry.lock.locked()


def test_failed_implicit_session_does_not_consume_owner_quota(session_client):
    client, service, rag = session_client
    rag.graph.ainvoke.side_effect = RuntimeError("offline model")
    response = client.post("/assistant/query", headers=headers("1"), json={"question": "fail"})
    assert response.status_code == 502
    assert not service._sessions.sessions
    assert not rag.context_manager.session_contexts
