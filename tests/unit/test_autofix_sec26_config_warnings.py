"""SEC-26 P3-3/P3-4：autofix 开关只认 true、白名单大小写敏感且 * 非通配；启动配置可疑值打 Warn（caplog 验证，全部离线）"""

import logging

import pytest

from app.config import settings
from app.config.settings import AutoFixConfig
from app.core.autofix.remediation import AutoFixRemediationEngine

SWITCHES = {
    "AUTOFIX_AUTO_EXECUTE_ENABLED": "auto_execute_enabled",
    "AUTOFIX_REPAIR_ENABLED": "repair_enabled",
    "AUTOFIX_BOOTSTRAP_ENABLED": "bootstrap_enabled",
}
LOGGER = "aiops.config.autofix"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (*SWITCHES, "AUTOFIX_ALLOWED_NAMESPACES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(settings, "CONFIG", {})
    monkeypatch.setattr(settings, "AUTOFIX_SWITCH_CONFIG", {})


def warnings_from(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


@pytest.mark.parametrize("env_key,attr", list(SWITCHES.items()))
@pytest.mark.parametrize(
    "raw,expected,warns",
    [
        ("0", False, True),
        ("no", False, True),
        ("1", False, True),
        ("", False, True),
        ("yes", False, True),
        # 承接 SEC-26 s4：先 strip 再 lower，等于 true 才开启
        (" true", True, False),
        ("true\n", True, False),
        ("\tTRUE  ", True, False),
        (" false ", False, False),
        (" 1 ", False, True),
        ("TRUE", True, False),
        ("true", True, False),
        ("false", False, False),
        ("False", False, False),
    ],
)
def test_switch_env_only_true_enables(monkeypatch, caplog, env_key, attr, raw, expected, warns) -> None:
    monkeypatch.setenv(env_key, raw)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cfg = AutoFixConfig()
    assert getattr(cfg, attr) is expected
    records = [r for r in warnings_from(caplog) if env_key in r.getMessage()]
    assert bool(records) is warns
    if warns:
        assert repr(raw) in records[0].getMessage() and "false" in records[0].getMessage()


@pytest.mark.parametrize("yaml_value,expected,warns", [(True, True, False), (False, False, False), (1, False, True), (0, False, True), ("yes", False, True)])
def test_switch_yaml_non_bool_values_are_off_and_warned(monkeypatch, caplog, yaml_value, expected, warns) -> None:
    monkeypatch.setattr(settings, "AUTOFIX_SWITCH_CONFIG", {"autofix": {"repair_enabled": yaml_value}})
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cfg = AutoFixConfig()
    assert cfg.repair_enabled is expected
    assert bool([r for r in warnings_from(caplog) if "autofix.repair_enabled" in r.getMessage()]) is warns


def test_unset_switches_default_off_without_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cfg = AutoFixConfig()
    assert (cfg.auto_execute_enabled, cfg.repair_enabled, cfg.bootstrap_enabled) == (False, False, False)
    assert cfg.allowed_namespaces == []
    assert warnings_from(caplog) == []


def test_wildcard_namespace_is_warned_and_never_matches(monkeypatch, caplog) -> None:
    monkeypatch.setenv("AUTOFIX_ALLOWED_NAMESPACES", "*")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cfg = AutoFixConfig()
    assert cfg.allowed_namespaces == ["*"]
    messages = [r.getMessage() for r in warnings_from(caplog)]
    assert any("'*'" in m and "不是通配符" in m for m in messages)
    engine = AutoFixRemediationEngine(k8s_client=None, allowed_namespaces=cfg.allowed_namespaces)
    assert engine._namespace_allowed("default") is False
    assert engine._namespace_allowed("*") is True  # 仅字面匹配


def test_uppercase_namespace_is_warned_and_case_sensitive(monkeypatch, caplog) -> None:
    monkeypatch.setenv("AUTOFIX_ALLOWED_NAMESPACES", "Staging,dev")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cfg = AutoFixConfig()
    assert cfg.allowed_namespaces == ["Staging", "dev"]  # 不做大小写归一
    messages = [r.getMessage() for r in warnings_from(caplog)]
    assert len(messages) == 1 and "'Staging'" in messages[0] and "大小写敏感" in messages[0]
    engine = AutoFixRemediationEngine(k8s_client=None, allowed_namespaces=cfg.allowed_namespaces)
    assert engine._namespace_allowed("staging") is False
    assert engine._namespace_allowed("dev") is True


def test_yaml_list_with_wildcard_and_uppercase_warns_each(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "CONFIG", {"autofix": {"allowed_namespaces": ["prod-*", "QA", "ok"]}})
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        AutoFixConfig()
    messages = [r.getMessage() for r in warnings_from(caplog)]
    assert len(messages) == 2
    assert any("'prod-*'" in m for m in messages) and any("'QA'" in m for m in messages)


def test_clean_namespace_list_does_not_warn(monkeypatch, caplog) -> None:
    monkeypatch.setenv("AUTOFIX_ALLOWED_NAMESPACES", "staging, dev-1")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert AutoFixConfig().allowed_namespaces == ["staging", "dev-1"]
    assert warnings_from(caplog) == []
