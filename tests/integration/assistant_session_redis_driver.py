"""Isolated subprocess driver; network is restricted to its owned Redis port."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.documents import Document
from openai.types.chat import ChatCompletionMessage
import redis.asyncio as redis

from app.common.exceptions import SessionAccessError
from app.common.identity import principal_scope
from app.core.agents.enterprise_assistant import RAGAssistant
from app.mcp.mcp_client import MCPAssistant
from app.models import AssistantRequest
import app.services.assistant_service as assistant_module
from app.services.assistant_service import OptimizedAssistantService
import app.services.assistant_session_redis as session_module
from app.services.assistant_session_redis import RedisAssistantSessions

port = int(os.environ["CLOUDOPS_OWNED_REDIS_PORT"])
original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex
original_sendto = socket.socket.sendto
original_resolution = socket.getaddrinfo


def guarded_connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        if not isinstance(address, tuple) or address[0] != "127.0.0.1" or address[1] != port:
            raise RuntimeError("unexpected external network access")
    return original_connect(sock, address)


def guarded_resolution(host, service, *args, **kwargs):
    if host != "127.0.0.1" or int(service) != port:
        raise RuntimeError("unexpected DNS or network access")
    return original_resolution(host, service, *args, **kwargs)


def guarded_connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        if not isinstance(address, tuple) or address[0] != "127.0.0.1" or address[1] != port:
            raise RuntimeError("unexpected external network access")
    return original_connect_ex(sock, address)


def guarded_sendto(sock, *args):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        raise RuntimeError("unexpected external datagram")
    return original_sendto(sock, *args)


socket.socket.connect = guarded_connect
socket.socket.connect_ex = guarded_connect_ex
socket.socket.sendto = guarded_sendto
socket.getaddrinfo = guarded_resolution


class FakeLLM:
    def __init__(self, client, expected=False):
        self.client = client
        self.expected = expected
        self.calls = 0

    async def generate_response(self, messages, **kwargs):
        self.calls += 1
        await self.client.incr("fixture:model_calls")
        if self.expected:
            assert messages[0]["role"] == "system"
            assert "不" in messages[0]["content"]
            assert messages[1]["role"] == "user"
            assert "first-question" in messages[1]["content"]
            assert "first-answer" in messages[1]["content"]
            return "second-answer"
        return "first-answer"


def rag_service(store, client, expected=False):
    llm = FakeLLM(client, expected)
    rag = RAGAssistant(SimpleNamespace(), llm)
    docs = [Document(page_content="offline knowledge fixture", metadata={})]
    rag.retriever.retrieve = AsyncMock(return_value=docs)
    rag.reranker.rerank = AsyncMock(return_value=docs)
    service = OptimizedAssistantService(sessions=store)
    service._assistant = rag
    service._initialized = True
    return service


def mcp_service(store, client):
    native = object.__new__(MCPAssistant)
    native._history = {}
    native._max_history_turns = 6
    native.model = "offline"
    native.client = SimpleNamespace(get_available_tools=AsyncMock(return_value={"tools": [{"name": "calculate", "description": "fixture", "parameters": {"type": "object", "properties": {}}}]}))

    async def complete(**kwargs):
        await client.incr("fixture:mcp_calls")
        assert kwargs["messages"][0]["role"] == "system"
        assert "first-question" not in kwargs["messages"][0]["content"]
        assert kwargs["messages"][1]["role"] == "user"
        text = kwargs["messages"][1]["content"]
        assert "first-question" in text and "first-answer" in text
        assert "second-question" in text and "second-answer" in text
        assert kwargs["messages"][-1]["content"] == "mcp-question"
        return SimpleNamespace(choices=[SimpleNamespace(message=ChatCompletionMessage(role="assistant", content="mcp-answer"))])

    native.llm_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    mcp = SimpleNamespace(_mcp_assistant=native, initialize=AsyncMock(), _ensure_ready=AsyncMock())

    async def get_answer(question, session_id):
        return {"answer": await native.process_query(question, session_id), "success": True}

    mcp.get_answer = get_answer
    service = OptimizedAssistantService(sessions=store)
    service._get_mcp_service = AsyncMock(return_value=mcp)
    return service


class FaultStore(RedisAssistantSessions):
    def __init__(self, client):
        super().__init__(client)
        self.fault = True

    async def _eval(self, script, keys, args):
        result = await super()._eval(script, keys, args)
        if script == session_module._COMPLETE and self.fault:
            self.fault = False
            raise SessionAccessError(503, "fixture lost acknowledgement")
        return result


async def main():
    operation = sys.argv[1]
    payload = json.loads(sys.stdin.read() or "{}")
    client = redis.Redis(host="127.0.0.1", port=port, decode_responses=True, socket_timeout=7)
    store = RedisAssistantSessions(client)
    try:
        with principal_scope(payload.get("owner", "user:1")):
            if operation == "bootstrap":
                assert await store.health_check() is True
                service = rag_service(store, client)
                sid = (await service.create_session(AssistantRequest(question="fixture")))["session_id"]
                turn = str(uuid.uuid4())
                result = await service.get_answer("first-question", session_id=sid, turn_id=turn)
                assert result["answer"] == "first-answer" and result["turn_id"] == turn
                print(json.dumps({"session_id": sid, "turn_id": turn}))
            elif operation == "continue":
                service = rag_service(store, client, expected=True)
                turn = str(uuid.uuid4())
                result = await service.get_answer("second-question", session_id=payload["session_id"], turn_id=turn)
                assert result["answer"] == "second-answer"
                print(json.dumps({"session_id": payload["session_id"], "turn_id": turn}))
            elif operation == "replay":
                service = rag_service(store, client, expected=True)
                before = int(await client.get("fixture:model_calls") or 0)
                result = await service.get_answer("second-question", session_id=payload["session_id"], turn_id=payload["turn_id"])
                assert result["answer"] == "second-answer" and int(await client.get("fixture:model_calls")) == before
                try:
                    await service.get_answer("different-question", session_id=payload["session_id"], turn_id=payload["turn_id"])
                except SessionAccessError as error:
                    assert error.status_code == 409
                else:
                    raise AssertionError("turn ID accepted a different request")
                print(json.dumps({"replayed": True}))
            elif operation == "mcp":
                service = mcp_service(store, client)
                result = await service.get_answer("mcp-question", mode=2, session_id=payload["session_id"], turn_id=str(uuid.uuid4()))
                assert result["answer"] == "mcp-answer"
                print(json.dumps({"mcp": True}))
            elif operation == "inspect":
                info = await store.info(payload["session_id"])
                print(json.dumps({"status": info["status"], "revision": info["revision"], "history": info["conversation_history"], "active_turn_id": info["active_turn_id"], "active_question": info["active_question"]}))
            elif operation == "foreign":
                try:
                    await store.info(payload["session_id"])
                except SessionAccessError as error:
                    assert error.status_code == 403
                else:
                    raise AssertionError("foreign session accessible")
                print(json.dumps({"denied": True}))
            elif operation == "abandon":
                turn = str(uuid.uuid4())
                token, _, _, replay = await store.begin(payload["session_id"], turn, "fixture-hash", "abandoned-question", 1)
                assert token and replay is None
                await client.hset("aiops:assistant:session:v1:item:" + payload["session_id"], "lease_until", "0")
                info = await store.info(payload["session_id"])
                assert info["status"] == "unknown" and info["active_turn_id"] == turn and info["active_question"] == "abandoned-question"
                assert await client.ttl("aiops:assistant:session:v1:item:" + payload["session_id"]) == -1
                try:
                    await store.complete(payload["session_id"], token, 3, info["conversation_history"], "late", "late", 1, turn, {"answer": "late"})
                except SessionAccessError:
                    pass
                else:
                    raise AssertionError("expired worker committed")
                try:
                    await store.begin(payload["session_id"], str(uuid.uuid4()), "another", "later", 1)
                except SessionAccessError as error:
                    assert error.status_code == 409
                else:
                    raise AssertionError("unknown turn was retried")
                print(json.dumps({"unknown": True}))
            elif operation == "block":
                session_module.LEASE_MS = 400
                assistant_module._LEASE_HEARTBEAT_SECONDS = 0.1
                service = rag_service(store, client)

                async def gate(messages, **kwargs):
                    await client.incr("fixture:model_calls")
                    Path(payload["entered"]).write_text("entered")
                    while not Path(payload["release"]).exists():
                        await asyncio.sleep(0.05)
                    return "held-answer"

                service._assistant.llm_service.generate_response = gate
                result = await service.get_answer("held-question", session_id=payload["session_id"], turn_id=payload["turn_id"])
                assert result["answer"] == "held-answer"
                print(json.dumps({"completed": True}))
            elif operation == "busy":
                service = rag_service(store, client)
                before = int(await client.get("fixture:model_calls") or 0)
                try:
                    await service.get_answer("other-question", session_id=payload["session_id"], turn_id=str(uuid.uuid4()))
                except SessionAccessError as error:
                    assert error.status_code == 409
                else:
                    raise AssertionError("concurrent turn was accepted")
                assert int(await client.get("fixture:model_calls") or 0) == before
                print(json.dumps({"busy": True}))
            elif operation == "fail_commit":
                service = rag_service(FaultStore(client), client)
                turn = str(uuid.uuid4())
                before = int(await client.get("fixture:model_calls") or 0)
                try:
                    await service.get_answer("lost-ack-question", session_id=payload["session_id"], turn_id=turn)
                except SessionAccessError as error:
                    assert error.status_code == 503
                else:
                    raise AssertionError("missing durable acknowledgement accepted")
                assert int(await client.get("fixture:model_calls") or 0) == before + 1
                print(json.dumps({"session_id": payload["session_id"], "turn_id": turn}))
            elif operation == "replay_lost":
                service = rag_service(store, client)
                before = int(await client.get("fixture:model_calls") or 0)
                result = await service.get_answer("lost-ack-question", session_id=payload["session_id"], turn_id=payload["turn_id"])
                assert result["answer"] == "first-answer" and int(await client.get("fixture:model_calls") or 0) == before
                print(json.dumps({"replayed": True}))
            elif operation == "aof_disabled":
                assert await store.health_check() is False
                try:
                    await store.create(1)
                except SessionAccessError as error:
                    assert error.status_code == 503
                else:
                    raise AssertionError("storage without AOF accepted")
                assert int(await client.get("fixture:model_calls") or 0) == 0
                print(json.dumps({"denied": True}))
            elif operation == "unsafe_eviction":
                assert await store.health_check() is False
                try:
                    await store.create(1)
                except SessionAccessError as error:
                    assert error.status_code == 503
                else:
                    raise AssertionError("evicting storage accepted")
                assert int(await client.get("fixture:model_calls") or 0) == 0
                assert not await client.keys("aiops:assistant:session:v1:*")
                print(json.dumps({"denied": True}))
            elif operation == "corrupt_active":
                sid = await store.create(1)
                turn = str(uuid.uuid4())
                token, _, _, _ = await store.begin(sid, turn, "fixture-hash", "question", 1)
                assert token
                key = "aiops:assistant:session:v1:item:" + sid
                raw = json.loads(await client.hget(key, "turn:" + turn))
                raw["question"] = "x" * (session_module.MAX_ITEM_BYTES + 1)
                await client.hset(key, "turn:" + turn, json.dumps(raw))
                try:
                    await store.info(sid)
                except SessionAccessError as error:
                    assert error.status_code == 503
                else:
                    raise AssertionError("oversized active request accepted")
                print(json.dumps({"rejected": True}))
            elif operation == "bounds":
                sid = await store.create(1)
                for index in range(session_module.MAX_TURNS):
                    turn = str(uuid.uuid4())
                    question = f"question-{index}"
                    token, revision, history, replay = await store.begin(sid, turn, question, question, 1)
                    assert token and replay is None and revision == index
                    await store.complete(sid, token, revision, history, question, "answer", 1, turn, {"answer": "answer", "turn_id": turn})
                info = await store.info(sid)
                assert info["revision"] == 100 and info["message_count"] == 200 and len(info["conversation_history"]) == 25
                assert info["conversation_history"][0]["q"] == "question-75"
                try:
                    await store.begin(sid, str(uuid.uuid4()), "extra", "extra", 1)
                except SessionAccessError as error:
                    assert error.status_code == 429
                else:
                    raise AssertionError("turn quota exceeded")
                assert await client.ttl(session_module._owner_key("user:1")) > 0
                print(json.dumps({"bounded": True}))
            else:
                raise AssertionError("unknown fixture operation")
    finally:
        await store.close()


def asgi_test():
    from app.api.middleware.auth import setup_auth
    from app.api.routes import assistant as routes
    from app.config.settings import config

    api_key = "offline-assistant-key-with-32-characters"
    config.security.api_key = api_key
    client = redis.Redis(host="127.0.0.1", port=port, decode_responses=True, socket_timeout=7)
    store = RedisAssistantSessions(client)
    service = rag_service(store, client)

    async def get_service():
        return service

    routes.get_assistant_service = get_service
    app = FastAPI()
    setup_auth(app)
    app.include_router(routes.router, prefix="/assistant")
    headers = {"X-API-Key": api_key, "X-CloudOps-User-ID": "1"}
    with TestClient(app) as http:
        created = http.post("/assistant/session", headers=headers)
        assert created.status_code == 200
        sid = created.json()["data"]["session_id"]
        turn = str(uuid.uuid4())
        with http.websocket_connect("/assistant/stream", headers=headers) as ws:
            ws.send_json({"question": "first-question", "session_id": sid, "turn_id": turn})
            first = ws.receive_json()
            assert first["success"] and first["data"]["turn_id"] == turn
            ws.send_json({"question": "first-question", "session_id": sid, "turn_id": turn})
            replay = ws.receive_json()
            assert replay["success"] and replay["data"]["answer"] == first["data"]["answer"]
        assert service._assistant.llm_service.calls == 1
        result = http.post("/assistant/query", headers=headers, json={"question": "first-question", "session_id": sid, "turn_id": turn})
        assert result.status_code == 200 and result.json()["data"]["turn_id"] == turn
        assert service._assistant.llm_service.calls == 1
        info = http.get("/assistant/session/" + sid, headers=headers).json()["data"]
        assert info["revision"] == 1 and info["last_turn_id"] == turn and info["conversation_history"][0]["a"] == "first-answer"
        foreign = http.get("/assistant/session/" + sid, headers={**headers, "X-CloudOps-User-ID": "2"})
        assert foreign.status_code == 403
    print(json.dumps({"asgi": True}))


if sys.argv[1] == "asgi":
    asgi_test()
else:
    asyncio.run(main())
