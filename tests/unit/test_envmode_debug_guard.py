"""生产环境 debug 防护：环境判定与 Go 后端 pkg/envmode 一致；生产开 debug 拒绝启动；生产下 5xx 不回显 details（全部离线）"""

from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.decorators import api_response
from app.api.middleware.error_handler import setup_error_handlers
from app.common.exceptions import ServiceUnavailableError
from app.config import envmode
from app.config.envmode import DebugInProductionError, debug_details_allowed, ensure_debug_safe, is_production, parse_env_mode
from app.config.settings import config

PRIVATE = "debug-guard-private-detail-91b2"
REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("production", "production"),
        ("prod", "production"),
        ("PRD", "production"),
        (" Production\n", "production"),
        ("development", "development"),
        ("dev", "development"),
        (" DEV ", "development"),
        ("test", "test"),
        ("testing", "test"),
        ("Testing", "test"),
        ("staging", None),
        ("", None),
        ("   ", None),
        (None, None),
        (1, None),
    ],
)
def test_parse_env_mode_matches_go_envmode_aliases(raw, expected) -> None:
    assert parse_env_mode(raw) == expected


@pytest.mark.parametrize("raw", ["production", "prod", "prd", "PROD", "staging", "", "unknown", None])
def test_is_production_includes_unknown_and_empty_fail_closed(raw) -> None:
    assert is_production(raw) is True


@pytest.mark.parametrize("raw", ["development", "dev", "test", "testing", " TEST "])
def test_dev_test_testing_are_not_production(raw) -> None:
    assert is_production(raw) is False


@pytest.mark.parametrize("env", ["production", "prod", "prd", "staging", "", None])
def test_debug_in_production_refuses_start(env) -> None:
    with pytest.raises(DebugInProductionError) as raised:
        ensure_debug_safe(env, True)
    assert "DEBUG=false" in str(raised.value)


@pytest.mark.parametrize("env", ["production", "prod", "staging", None])
def test_production_without_debug_starts(env) -> None:
    ensure_debug_safe(env, False)


@pytest.mark.parametrize("env", ["development", "dev", "test", "testing"])
def test_debug_allowed_in_dev_and_test(env) -> None:
    ensure_debug_safe(env, True)
    assert debug_details_allowed(env, True) is True


def test_long_env_value_not_echoed() -> None:
    long_value = "x" * 40
    with pytest.raises(DebugInProductionError) as raised:
        ensure_debug_safe(long_value, True)
    assert long_value not in str(raised.value)


async def test_main_app_lifespan_refuses_debug_in_production(monkeypatch) -> None:
    from app import main as app_main

    monkeypatch.setattr(config, "env", "production")
    monkeypatch.setattr(config, "debug", True)
    init = AsyncMock()
    monkeypatch.setattr(app_main.ServiceFactory, "initialize", init, raising=False)
    with pytest.raises(DebugInProductionError):
        async with app_main.lifespan(FastAPI()):
            pass
    init.assert_not_awaited()


async def test_mcp_lifespan_refuses_debug_in_production(monkeypatch) -> None:
    from app.mcp import main as mcp_main

    monkeypatch.setattr(mcp_main.config, "env", "prod")
    monkeypatch.setattr(mcp_main.config, "debug", True)
    server_cls = AsyncMock(side_effect=AssertionError("must not start MCP server"))
    monkeypatch.setattr(mcp_main, "MCPServer", server_cls, raising=False)
    with pytest.raises(DebugInProductionError):
        async with mcp_main.lifespan(FastAPI()):
            pass


def test_main_app_starts_with_debug_in_test_env() -> None:
    # 回归保护：测试环境（ENV=test，由 conftest 设置）debug 开启也正常启动
    assert config.env == "test"
    ensure_debug_safe(config.env, True)


def _client() -> TestClient:
    app = FastAPI()
    setup_error_handlers(app)

    @app.get("/probe")
    @api_response("probe")
    async def probe():
        raise ServiceUnavailableError("svc", details={"error": PRIVATE})

    return TestClient(app)


@pytest.mark.parametrize("env", ["production", "prod", "staging"])
def test_error_handler_hides_5xx_details_in_production_even_if_debug(monkeypatch, env) -> None:
    monkeypatch.setattr(config, "env", env)
    monkeypatch.setattr(config, "debug", True)
    response = _client().get("/probe")
    assert response.status_code == 503
    data = response.json()["data"]
    assert data["error_code"] == "SERVICE_UNAVAILABLE"
    assert "details" not in data
    assert PRIVATE not in response.text
    assert data["detail"] == "Internal server error"


@pytest.mark.parametrize("env", ["development", "test", "testing"])
def test_error_handler_keeps_5xx_details_in_dev_test_with_debug(monkeypatch, env) -> None:
    monkeypatch.setattr(config, "env", env)
    monkeypatch.setattr(config, "debug", True)
    response = _client().get("/probe")
    assert response.json()["data"]["details"] == {"error": PRIVATE}


def test_aliases_mirror_go_envmode_table() -> None:
    assert envmode._ALIASES == {
        "production": "production",
        "prod": "production",
        "prd": "production",
        "development": "development",
        "dev": "development",
        "test": "test",
        "testing": "test",
    }


def test_production_debug_rule_documented() -> None:
    doc = (REPO / "docs/production.md").read_text(encoding="utf-8")
    assert "生产环境开启 debug 拒绝启动" in doc
