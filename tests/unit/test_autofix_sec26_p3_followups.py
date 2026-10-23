"""SEC-26 第二层审查 P3-a / P3-b / P3-c 的回归测试（全部离线）"""

import logging
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import base, settings
from app.config.settings import AutoFixConfig, MCPConfig
from app.core.agents.ops_workflow import OpsMultiAgentWorkflow

LOGGER = "aiops.config.autofix"
MARKER = "p3c-leak-MARKER-7f3a"
SWITCHES = [
    ("AUTOFIX_AUTO_EXECUTE_ENABLED", "autofix.auto_execute_enabled", AutoFixConfig, "auto_execute_enabled"),
    ("AUTOFIX_REPAIR_ENABLED", "autofix.repair_enabled", AutoFixConfig, "repair_enabled"),
    ("AUTOFIX_BOOTSTRAP_ENABLED", "autofix.bootstrap_enabled", AutoFixConfig, "bootstrap_enabled"),
    ("MCP_WRITE_TOOLS_ENABLED", "mcp.write_tools_enabled", MCPConfig, "write_tools_enabled"),
    ("MCP_SECRET_READ_ENABLED", "mcp.secret_read_enabled", MCPConfig, "secret_read_enabled"),
]


@pytest.fixture
def no_switch_env(monkeypatch):
    for env_key, *_ in SWITCHES:
        monkeypatch.delenv(env_key, raising=False)


@pytest.fixture
def yaml_root(monkeypatch, tmp_path):
    """与 test_autofix_sec26_yaml_strict_bool 相同：配置根目录指到临时目录，从真实 YAML 文本加载"""
    monkeypatch.setenv("ENV", "sec26-p3-nonexistent")
    monkeypatch.setattr(base, "_get_root_dir", lambda: tmp_path)
    (tmp_path / "config").mkdir()

    def write(text: str) -> None:
        (tmp_path / "config" / "config.yaml").write_text(text, encoding="utf-8")
        monkeypatch.setattr(settings, "CONFIG", settings.load_config())
        monkeypatch.setattr(settings, "AUTOFIX_SWITCH_CONFIG", settings.load_config(strict_bool=True))

    return write


# ---------------------------------------------------------------- P3-a：executor 守卫直接测试


def _workflow():
    engine = Mock()
    engine.execute_plan = AsyncMock(return_value={"status": "completed"})
    return OpsMultiAgentWorkflow(engine, auto_execute=True), engine


async def test_executor_guard_respects_plan_only_even_when_auto_execute_true():
    workflow, engine = _workflow()
    state = await workflow._executor({"plan_only": True, "plan": {"plan_id": "p", "actions": [{"action_type": "restart_deployment"}]}})
    assert engine.execute_plan.await_count == 0
    assert state["status"] == "needs_human_confirmation"
    assert state["next_action"] == "human_confirm"
    assert "Executor" not in state["agents_used"]


async def test_executor_runs_engine_when_auto_execute_true_and_not_plan_only():
    # 对照组：守卫被整个删除或永远拒绝时也能被发现
    workflow, engine = _workflow()
    state = await workflow._executor({"plan_only": False, "plan": {"plan_id": "p"}})
    assert engine.execute_plan.await_count == 1
    assert state["status"] == "completed"


# ---------------------------------------------------------------- P3-b：YAML 不加引号的大小写混写


@pytest.mark.parametrize(("literal", "expected"), [("tRUE", True), ("tRue", True), ("TrUe", True), ("fALSE", False), ("FaLsE", False)])
@pytest.mark.parametrize(("env_key", "path", "cls", "attr"), SWITCHES)
def test_yaml_mixed_case_literal_becomes_string_and_follows_env_rule(
    yaml_root,
    no_switch_env,
    caplog,
    literal,
    expected,
    env_key,
    path,
    cls,
    attr,
):
    section, field = path.split(".")
    yaml_root(f"{section}:\n  {field}: {literal}\n")
    assert settings.AUTOFIX_SWITCH_CONFIG[section][field] == literal  # 严格解析下是字符串而不是 bool
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert getattr(cls(), attr) is expected
    assert [r for r in caplog.records if r.name == LOGGER and path in r.getMessage()] == []


# ---------------------------------------------------------------- P3-c：告警不输出值本身


@pytest.mark.parametrize(("env_key", "path", "cls", "attr"), SWITCHES)
def test_switch_warning_from_env_has_field_and_type_but_no_value(no_switch_env, monkeypatch, caplog, env_key, path, cls, attr):
    monkeypatch.setenv(env_key, MARKER)
    with caplog.at_level(logging.DEBUG):
        assert getattr(cls(), attr) is False
    records = [r for r in caplog.records if r.name == LOGGER and path in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert env_key in message and "类型 str" in message
    assert MARKER not in caplog.text and MARKER.lower() not in caplog.text


@pytest.mark.parametrize(("env_key", "path", "cls", "attr"), SWITCHES)
def test_switch_warning_from_yaml_has_no_value(no_switch_env, monkeypatch, caplog, env_key, path, cls, attr):
    section, field = path.split(".")
    monkeypatch.setattr(settings, "AUTOFIX_SWITCH_CONFIG", {section: {field: [MARKER]}})
    with caplog.at_level(logging.DEBUG):
        assert getattr(cls(), attr) is False
    records = [r for r in caplog.records if r.name == LOGGER and path in r.getMessage()]
    assert records and "类型 list" in records[0].getMessage()
    assert MARKER not in caplog.text


def test_yaml11_alias_hint_kept_without_value(no_switch_env, monkeypatch, caplog):
    monkeypatch.setattr(settings, "AUTOFIX_SWITCH_CONFIG", {"autofix": {"repair_enabled": "on"}})
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert AutoFixConfig().repair_enabled is False
    message = next(r.getMessage() for r in caplog.records if "autofix.repair_enabled" in r.getMessage())
    assert "YAML 1.1" in message and "'on'" not in message


def test_namespace_warnings_report_position_not_value(monkeypatch, caplog):
    monkeypatch.setenv("AUTOFIX_ALLOWED_NAMESPACES", f"ok,{MARKER}-*,{MARKER}")
    with caplog.at_level(logging.DEBUG):
        cfg = AutoFixConfig()
    assert cfg.allowed_namespaces == ["ok", f"{MARKER}-*", MARKER]  # 白名单本身按字面保留
    messages = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert any("第 2 项" in m and "不是通配符" in m for m in messages)
    assert any("第 2 项" in m and "大写" in m for m in messages)
    assert any("第 3 项" in m and "大写" in m for m in messages)
    assert MARKER not in caplog.text and MARKER.lower() not in caplog.text
