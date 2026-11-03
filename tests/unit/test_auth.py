from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
import pytest

from app.api.middleware.auth import setup_auth
from app.config.settings import config


def make_client(monkeypatch, key="test-key", env="test"):
    monkeypatch.setattr(config.security, "api_key", key)
    monkeypatch.setattr(config, "env", env)
    app = FastAPI()
    setup_auth(app)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/tools/execute")
    def execute():
        return {"executed": True}

    return TestClient(app)


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"Authorization": "Basic test-key"}, {"X-API-Key": "非ASCII"}])
def test_denies_unauthenticated_operations(monkeypatch, headers):
    client = make_client(monkeypatch)
    # HTTP header values are bytes on the wire.
    headers = {k: v.encode("utf-8") for k, v in headers.items()}
    assert client.post("/tools/execute", headers=headers).status_code == 401


@pytest.mark.parametrize("headers", [{"X-API-Key": "test-key"}, {"Authorization": "Bearer test-key"}])
def test_allows_service_credentials(monkeypatch, headers):
    assert make_client(monkeypatch).post("/tools/execute", headers=headers).json() == {"executed": True}


def test_public_health_does_not_exempt_operations(monkeypatch):
    client = make_client(monkeypatch)
    assert client.get("/health").status_code == 200
    assert client.post("/health").status_code == 401
    assert client.get("/docs").status_code == 401


PRODUCTION_LIKE_ENVS = ["production", "prod", "PRD", " Production\n", "staging", "", "   ", None, 123, False]


@pytest.mark.parametrize("env", PRODUCTION_LIKE_ENVS)
@pytest.mark.parametrize("key", ["", "private-short-key-canary", "x" * 31])
def test_production_requires_strong_key(monkeypatch, key, env, caplog):
    with pytest.raises(ValueError, match="AIOPS_API_KEY") as raised:
        make_client(monkeypatch, key, env)
    if key:
        assert key not in str(raised.value)
        assert key not in caplog.text


@pytest.mark.parametrize("env", PRODUCTION_LIKE_ENVS)
def test_production_aliases_require_credentials_for_private_requests(monkeypatch, env):
    key = "s" * 32
    client = make_client(monkeypatch, key, env)
    assert client.get("/health").status_code == 200
    assert client.post("/health").status_code == 401
    assert client.get("/docs").status_code == 401
    assert client.post("/tools/execute").status_code == 401
    assert client.post("/tools/execute", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/tools/execute", headers={"X-API-Key": key}).json() == {"executed": True}
    assert client.post("/tools/execute", headers={"Authorization": f"Bearer {key}"}).json() == {"executed": True}


@pytest.mark.parametrize("env", ["development", "dev", " DEV ", "test", "testing", " TESTING "])
def test_explicit_development_and_test_keep_empty_key_contract(monkeypatch, env):
    client = make_client(monkeypatch, "", env)
    assert client.post("/tools/execute").status_code == 200


@pytest.mark.parametrize("env", ["test", *PRODUCTION_LIKE_ENVS])
def test_websocket_requires_credentials(monkeypatch, env):
    from starlette.websockets import WebSocketDisconnect

    key = "s" * 32
    client = make_client(monkeypatch, key, env)

    @client.app.websocket("/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        await ws.send_text("authenticated")
        await ws.close()

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws"):
            pass
    assert exc.value.code == 1008
    with client.websocket_connect("/ws", headers={"X-API-Key": key}) as ws:
        assert ws.receive_text() == "authenticated"
