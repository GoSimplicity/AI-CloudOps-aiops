"""Offline service/response boundaries; Redis and models are explicit substitutes."""

import asyncio
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.decorators import api_response
from app.api.middleware.auth import setup_auth
from app.api.middleware.error_handler import setup_error_handlers
from app.api.routes import assistant
from app.common.exceptions import SessionAccessError, SessionRecoveryLocator, session_recovery_data
from app.common.identity import principal_scope, require_principal
from app.config.settings import config
from app.services.assistant_service import OptimizedAssistantService
from app.services.assistant_session_redis import MAX_ITEM_BYTES, RedisAssistantSessions, _session_key

SID = "00000000-0000-4000-8000-000000000001"
TURN = "11111111-1111-4111-8111-111111111111"
KEY = "offline-locator-service-key-32-characters"
EXPECTED = {"session_id": SID, "turn_id": TURN, "retry_safe": False}
PRIVATE = "private-source-token-question-canary"


class BoundarySessions(RedisAssistantSessions):
    def __init__(self, failure="size", unknown_failure=False):
        self.failure = failure
        self.unknown_failure = unknown_failure
        self.created = 0
        self.begin_calls = 0
        self.unknown_calls = 0
        self.state = "idle"
        self.saved = None

    async def create(self, _mode):
        self.created += 1
        return SID

    async def begin(self, sid, turn, _hash, _question, _mode):
        self.begin_calls += 1
        _session_key(sid)
        if require_principal() != "user:1":
            raise SessionAccessError(403, "无权访问此会话")
        if self.failure == "begin":
            raise SessionAccessError(503, PRIVATE)
        if self.saved is not None:
            return None, 0, [], self.saved
        if self.state == "unknown":
            raise SessionAccessError(409, "请求结果未知")
        self.state = "running"
        return "private-claim-token", 0, [], None

    async def complete(self, *args):
        if self.failure == "size":
            return await super().complete(*args)  # Actual pre-storage size guard.
        if self.failure == "lost_ack":
            self.saved = args[-1]
            self.state = "idle"
        raise SessionAccessError(503, PRIVATE)

    async def unknown(self, _sid, _token):
        self.unknown_calls += 1
        if self.unknown_failure:
            raise OSError(PRIVATE)
        if self.saved is None:
            self.state = "unknown"

    async def renew(self, *_args):
        raise SessionAccessError(503, PRIVATE)

    async def info(self, sid):
        _session_key(sid)
        if require_principal() != "user:1":
            raise SessionAccessError(403, "无权访问此会话")
        return {"success": True, "session_id": sid, "created_time": "", "last_activity": "", "message_count": 0, "mode": 1, "status": self.state, "active_turn_id": TURN, "active_question": PRIVATE}


def service_for(store):
    service = OptimizedAssistantService(sessions=store)
    service._restore_durable_history = AsyncMock()
    service._clear_durable_local_history = lambda *_args: None
    answer = "a" * (MAX_ITEM_BYTES + 1) if store.failure == "size" else "safe synthetic answer"
    service._handle_rag_mode = AsyncMock(return_value={"answer": answer, "session_id": SID, "turn_id": TURN})
    return service


@pytest.mark.parametrize("path", ["direct", "decorated"])
def test_safe_locator_survives_both_http_paths_without_unmasking_5xx(monkeypatch, path):
    monkeypatch.setattr(config, "env", "production")
    monkeypatch.setattr(config, "debug", True)
    app = FastAPI()
    setup_error_handlers(app)

    async def fail():
        error = SessionAccessError(503, PRIVATE)
        error.details = {"token": PRIVATE}
        raise error.with_recovery(SID, TURN)

    app.add_api_route("/direct", fail)
    app.add_api_route("/decorated", api_response("probe")(fail))
    with TestClient(app) as client:
        response = client.get("/" + path)
    assert response.status_code == 503
    assert response.json()["data"]["recovery"] == EXPECTED
    assert PRIVATE not in response.text
    assert "details" not in response.json()["data"]


def test_locator_has_only_validated_canonical_ids_and_never_mutates_shared_errors():
    original = SessionAccessError(503, PRIVATE)
    first = original.with_recovery(SID, TURN)
    other_sid = str(uuid.uuid4())
    second = original.with_recovery(other_sid, TURN)
    assert original.session_recovery is None
    assert session_recovery_data(first.session_recovery) == EXPECTED
    assert session_recovery_data(second.session_recovery)["session_id"] == other_sid
    assert session_recovery_data({**EXPECTED, "token": PRIVATE}) is None
    for value in ["invalid", TURN.upper(), " " + TURN]:
        # TURN happens to contain only digits; use an actual alphabetic UUID for
        # the uppercase/noncanonical case below.
        if value == TURN:
            continue
        assert SessionAccessError(503, "safe").with_recovery(value, TURN).session_recovery is None
    alpha = "abcdef01-abcd-4abc-8abc-abcdef012345"
    assert SessionAccessError(503, "safe").with_recovery(alpha.upper(), TURN).session_recovery is None
    locator = SessionRecoveryLocator(SID, TURN)
    object.__setattr__(locator, "session_id", PRIVATE)
    assert session_recovery_data(locator) is None


@pytest.mark.parametrize("unknown_failure", [False, True])
async def test_implicit_first_turn_failure_returns_locator_without_claiming_unknown_persisted(unknown_failure):
    store = BoundarySessions(unknown_failure=unknown_failure)
    service = service_for(store)
    with principal_scope("user:1"), pytest.raises(SessionAccessError) as raised:
        await service.get_answer("safe question", turn_id=TURN)
    assert raised.value.status_code == 503
    assert session_recovery_data(raised.value.session_recovery) == EXPECTED
    assert store.created == 1 and store.unknown_calls == 1
    service._handle_rag_mode.assert_awaited_once()
    assert set(session_recovery_data(raised.value.session_recovery)) == {"session_id", "turn_id", "retry_safe"}


@pytest.mark.parametrize("failure", ["foreign", "invalid", "begin", "invalid turn"])
async def test_pre_begin_or_unowned_errors_never_get_recovery(failure):
    store = BoundarySessions(failure="begin" if failure == "begin" else "size")
    service = service_for(store)
    owner = "user:2" if failure == "foreign" else "user:1"
    sid = "invalid" if failure == "invalid" else SID
    turn = "bad" if failure == "invalid turn" else TURN
    with principal_scope(owner), pytest.raises(SessionAccessError) as raised:
        await service.get_answer("safe question", session_id=sid, turn_id=turn)
    assert raised.value.session_recovery is None
    assert store.unknown_calls == 0
    service._handle_rag_mode.assert_not_awaited()


async def test_unknown_request_is_not_reexecuted_and_completed_lost_ack_can_only_replay():
    for failure in ["size", "lost_ack"]:
        store = BoundarySessions(failure)
        service = service_for(store)
        with principal_scope("user:1"):
            with pytest.raises(SessionAccessError) as raised:
                await service.get_answer("safe question", turn_id=TURN)
            assert session_recovery_data(raised.value.session_recovery) == EXPECTED
            if failure == "size":
                with pytest.raises(SessionAccessError) as blocked:
                    await service.get_answer("safe question", session_id=SID, turn_id=TURN)
                assert blocked.value.session_recovery is None
            else:
                replay = await service.get_answer("safe question", session_id=SID, turn_id=TURN)
                assert replay["answer"] == "safe synthetic answer"
        service._handle_rag_mode.assert_awaited_once()


async def test_caller_cancellation_stays_cancelled_and_does_not_auto_retry():
    store = BoundarySessions()
    service = service_for(store)
    entered = asyncio.Event()

    async def blocked(*_args):
        entered.set()
        await asyncio.Event().wait()

    service._handle_rag_mode = AsyncMock(side_effect=blocked)
    with principal_scope("user:1"):
        task = asyncio.create_task(service.get_answer("safe question", turn_id=TURN))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert store.unknown_calls == 1
    service._handle_rag_mode.assert_awaited_once()


async def test_heartbeat_loss_returns_safe_recovery(monkeypatch):
    import app.services.assistant_service as module

    monkeypatch.setattr(module, "_LEASE_HEARTBEAT_SECONDS", 0)
    store = BoundarySessions()
    service = service_for(store)

    async def blocked(*_args):
        await asyncio.Event().wait()

    service._handle_rag_mode = AsyncMock(side_effect=blocked)
    with principal_scope("user:1"), pytest.raises(SessionAccessError) as raised:
        await asyncio.wait_for(service.get_answer("safe question", turn_id=TURN), 1)
    assert raised.value.status_code == 503
    assert session_recovery_data(raised.value.session_recovery) == EXPECTED
    assert store.unknown_calls == 1


@pytest.mark.parametrize("transport", ["http", "ws"])
def test_actual_routes_expose_same_safe_recovery_without_source_details(monkeypatch, transport):
    monkeypatch.setattr(config, "env", "production")
    monkeypatch.setattr(config, "debug", True)
    monkeypatch.setattr(config.security, "api_key", KEY)
    store = BoundarySessions("lost_ack", unknown_failure=True)
    service = service_for(store)
    monkeypatch.setattr(assistant, "get_assistant_service", AsyncMock(return_value=service))
    app = FastAPI()
    setup_auth(app)
    setup_error_handlers(app)
    app.include_router(assistant.router, prefix="/assistant")
    headers = {"X-API-Key": KEY, "X-CloudOps-User-ID": "1"}
    with TestClient(app) as client:
        if transport == "http":
            response = client.post("/assistant/query", headers=headers, json={"question": "safe question", "turn_id": TURN})
            assert response.status_code == 503
            payload = response.json()
        else:
            with client.websocket_connect("/assistant/stream", headers=headers) as stream:
                stream.send_json({"question": "safe question", "turn_id": TURN})
                payload = stream.receive_json()
                assert stream.receive()["code"] == 1008
        assert payload["data"]["recovery"] == EXPECTED
        assert PRIVATE not in str(payload)
    service._handle_rag_mode.assert_awaited_once()
