from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
import pytest

from app.api.middleware.auth import ensure_service_auth_configuration, setup_auth
from app.config.settings import config


def make_client(monkeypatch, key="test-key", env="test", peer=("testclient", 50000)):
    monkeypatch.setattr(config.security, "api_key", key)
    monkeypatch.setattr(config, "env", env)
    app = FastAPI()
    setup_auth(app)

    @app.api_route("/health", methods=["GET", "HEAD"])
    def health():
        return {"status": "ok"}

    @app.post("/tools/execute")
    def execute():
        return {"executed": True}

    return TestClient(app, client=peer)


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


NON_PRODUCTION_ENVS = ["development", "dev", " DEV ", "test", "testing", " TESTING "]
LOOPBACK_BINDS = ["127.0.0.1", "127.0.0.2", "::1", "localhost", "[::1]", " ::ffff:127.0.0.1 "]
PUBLIC_BINDS = ["0.0.0.0", "::", "[::]", "192.168.1.8", "example.dev", "", None]
LOOPBACK_PEERS = ["127.0.0.1", "127.0.0.2", "::1", "localhost", "::ffff:127.0.0.1"]
REMOTE_PEERS = ["10.1.2.3", "testclient", "0.0.0.0", "::", "8.8.8.8", "example.dev"]


@pytest.mark.parametrize("env", NON_PRODUCTION_ENVS)
@pytest.mark.parametrize("host", PUBLIC_BINDS)
def test_empty_key_refuses_to_bind_beyond_loopback(monkeypatch, env, host):
    monkeypatch.setattr(config, "host", host)
    with pytest.raises(ValueError, match="loopback") as raised:
        make_client(monkeypatch, "", env)
    message = str(raised.value)
    assert "Production requires" not in message
    assert "AIOPS_API_KEY" in message
    if isinstance(host, str) and host.strip():
        assert host.strip() not in message


@pytest.mark.parametrize("env", ["production", "prod", "staging", "", None])
@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "0.0.0.0"])
@pytest.mark.parametrize("key", ["", "x" * 31])
def test_production_like_envs_still_require_a_strong_key_on_loopback(monkeypatch, env, host, key):
    monkeypatch.setattr(config, "host", host)
    with pytest.raises(ValueError, match="Production requires AIOPS_API_KEY") as raised:
        make_client(monkeypatch, key, env)
    assert "loopback" not in str(raised.value)
    if key:
        assert key not in str(raised.value)


@pytest.mark.parametrize("env", NON_PRODUCTION_ENVS)
@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
@pytest.mark.parametrize("peer", LOOPBACK_PEERS)
def test_empty_key_allows_loopback_clients_only(monkeypatch, env, host, peer):
    monkeypatch.setattr(config, "host", host)
    client = make_client(monkeypatch, "", env, peer=(peer, 50000))
    assert client.get("/health").status_code == 200
    assert client.head("/health").status_code == 200
    assert client.post("/tools/execute").status_code == 200
    assert client.post("/tools/execute", headers={"X-CloudOps-User-ID": "1"}).status_code == 200


@pytest.mark.parametrize("env", NON_PRODUCTION_ENVS)
@pytest.mark.parametrize("peer", REMOTE_PEERS)
def test_empty_key_rejects_non_loopback_clients(monkeypatch, env, peer):
    monkeypatch.setattr(config, "host", "127.0.0.1")
    client = make_client(monkeypatch, "", env, peer=(peer, 50000))
    assert client.get("/health").status_code == 200
    assert client.head("/health").status_code == 200
    denied = client.post("/tools/execute", headers={"X-API-Key": "", "X-CloudOps-User-ID": "1"})
    assert denied.status_code == 401
    assert denied.json()["message"] == "Invalid or missing service API key"
    assert client.get("/docs").status_code == 401


@pytest.mark.parametrize("env", ["development", "test", "production", "prod", "staging"])
def test_configured_key_accepts_remote_x_api_key_and_bearer(monkeypatch, env):
    """Go 后端从其它地址带 X-API-Key 调用，不能被回环限制误伤。"""
    monkeypatch.setattr(config, "host", "0.0.0.0")
    key = "k" * 32
    client = make_client(monkeypatch, key, env, peer=("10.1.2.3", 50000))
    assert client.get("/health").status_code == 200
    assert client.post("/tools/execute").status_code == 401
    assert client.post("/tools/execute", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/tools/execute", headers={"X-API-Key": key}).json() == {"executed": True}
    assert client.post("/tools/execute", headers={"Authorization": f"Bearer {key}"}).json() == {"executed": True}


@pytest.mark.parametrize("env", NON_PRODUCTION_ENVS)
def test_short_nonempty_key_still_authenticates_on_a_public_bind(monkeypatch, env):
    monkeypatch.setattr(config, "host", "0.0.0.0")
    key = "short-dev-key"
    client = make_client(monkeypatch, key, env, peer=("10.1.2.3", 50000))
    assert client.post("/tools/execute").status_code == 401
    assert client.post("/tools/execute", headers={"X-API-Key": key}).status_code == 200


def test_entrypoint_bind_check_is_independent_of_configured_host():
    """MCP --host 可以和 config.host 不同，校验的是实际绑定地址。"""
    ensure_service_auth_configuration("development", "", "127.0.0.1")
    ensure_service_auth_configuration("testing", "short-dev-key", "0.0.0.0")
    ensure_service_auth_configuration("production", "p" * 32, "0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        ensure_service_auth_configuration(" DEV ", "", "0.0.0.0")
    with pytest.raises(ValueError, match="Production requires AIOPS_API_KEY"):
        ensure_service_auth_configuration("", "", "127.0.0.1")


@pytest.mark.parametrize("env", ["development", "test"])
@pytest.mark.parametrize("peer,allowed", [("127.0.0.1", True), ("::1", True), ("10.4.5.6", False)])
def test_empty_key_websocket_follows_the_same_loopback_rule(monkeypatch, env, peer, allowed):
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(config, "host", "localhost")
    client = make_client(monkeypatch, "", env, peer=(peer, 50000))

    @client.app.websocket("/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        await ws.send_text("open")
        await ws.close()

    if allowed:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_text() == "open"
        return
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws"):
            pass
    assert exc.value.code == 1008


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
