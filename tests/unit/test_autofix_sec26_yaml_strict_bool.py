"""SEC-26 第二层审查 P2-2：autofix 开关从 YAML 文本到解析结果，只有 true/True/TRUE 为开（全部离线，临时目录里的 YAML 文件）"""

import logging
from pathlib import Path

import pytest
import yaml

from app.config import base, settings
from app.config.settings import AutoFixConfig

LOGGER = "aiops.config.autofix"
SWITCH_ENV = ("AUTOFIX_AUTO_EXECUTE_ENABLED", "AUTOFIX_REPAIR_ENABLED", "AUTOFIX_BOOTSTRAP_ENABLED")
REPO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture()
def yaml_root(monkeypatch, tmp_path):
    """把配置根目录指到临时目录，ENV 指向不存在的环境，从而读取 tmp/config/config.yaml"""
    for key in SWITCH_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ENV", "sec26-p2-2-nonexistent")
    monkeypatch.setattr(base, "_get_root_dir", lambda: tmp_path)
    (tmp_path / "config").mkdir()

    def write(text: str) -> None:
        (tmp_path / "config" / "config.yaml").write_text(text, encoding="utf-8")
        monkeypatch.setattr(settings, "CONFIG", settings.load_config())
        monkeypatch.setattr(settings, "AUTOFIX_SWITCH_CONFIG", settings.load_config(strict_bool=True))

    return write


def _switch_warnings(caplog, path: str):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING and path in r.getMessage()]


@pytest.mark.parametrize(
    "literal,expected",
    [
        ("yes", False),
        ("on", False),
        ("On", False),
        ("True", True),
        ("TRUE", True),
        ("true", True),
        ("y", False),
        ("1", False),
        ("'yes'", False),
    ],
)
def test_switch_yaml_text_only_true_literals_enable(yaml_root, caplog, literal, expected) -> None:
    yaml_root(f"autofix:\n  repair_enabled: {literal}\n  auto_execute_enabled: {literal}\n  bootstrap_enabled: {literal}\n")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cfg = AutoFixConfig()
    assert (cfg.repair_enabled, cfg.auto_execute_enabled, cfg.bootstrap_enabled) == (expected, expected, expected)
    for field in ("repair_enabled", "auto_execute_enabled", "bootstrap_enabled"):
        # 开 → 不告警；其它值（yes/on/On/y/1/'yes'）一律关闭并告警
        assert bool(_switch_warnings(caplog, f"autofix.{field}")) is (not expected)


@pytest.mark.parametrize("literal", ["false", "False", "FALSE"])
def test_switch_yaml_text_false_literals_are_off_without_warning(yaml_root, caplog, literal) -> None:
    yaml_root(f"autofix:\n  repair_enabled: {literal}\n")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert AutoFixConfig().repair_enabled is False
    assert _switch_warnings(caplog, "autofix.repair_enabled") == []


@pytest.mark.parametrize("literal", ["no", "off", "Off", "NO"])
def test_switch_yaml_text_off_aliases_are_off_and_warned(yaml_root, caplog, literal) -> None:
    yaml_root(f"autofix:\n  repair_enabled: {literal}\n")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert AutoFixConfig().repair_enabled is False
    records = _switch_warnings(caplog, "autofix.repair_enabled")
    assert records and "YAML 1.1" in records[0].getMessage()


def test_yaml_1_is_off_and_warned_as_number(yaml_root, caplog) -> None:
    yaml_root("autofix:\n  repair_enabled: 1\n")
    assert settings.AUTOFIX_SWITCH_CONFIG["autofix"]["repair_enabled"] == 1
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert AutoFixConfig().repair_enabled is False
    assert "1" in _switch_warnings(caplog, "autofix.repair_enabled")[0].getMessage()


def test_env_still_overrides_yaml(yaml_root, monkeypatch) -> None:
    yaml_root("autofix:\n  repair_enabled: true\n")
    monkeypatch.setenv("AUTOFIX_REPAIR_ENABLED", "yes")
    assert AutoFixConfig().repair_enabled is False
    monkeypatch.setenv("AUTOFIX_REPAIR_ENABLED", "true")
    assert AutoFixConfig().repair_enabled is True


def test_global_config_parsing_is_unchanged(yaml_root) -> None:
    """严格 loader 只用于开关视图：全局 CONFIG 仍按 yaml.safe_load（YAML 1.1）解析，例如 app.debug: yes 仍为 True"""
    text = "app:\n  debug: yes\ndev:\n  auto_reload: on\nautofix:\n  repair_enabled: yes\n"
    yaml_root(text)
    assert settings.CONFIG == yaml.safe_load(text)
    assert settings.CONFIG["app"]["debug"] is True
    assert settings.CONFIG["dev"]["auto_reload"] is True
    assert settings.CONFIG["autofix"]["repair_enabled"] is True  # 全局视图不变，但开关不读它
    assert settings.AUTOFIX_SWITCH_CONFIG["autofix"]["repair_enabled"] == "yes"
    assert AutoFixConfig().repair_enabled is False


def test_strict_loader_does_not_mutate_safe_loader() -> None:
    assert yaml.safe_load("a: yes") == {"a": True}
    assert yaml.load("a: yes", Loader=base.StrictBoolSafeLoader) == {"a": "yes"}
    assert yaml.safe_load("a: yes") == {"a": True}


def test_load_yaml_rejects_non_safe_loader() -> None:
    with pytest.raises(TypeError):
        base._load_yaml(yaml.UnsafeLoader)


@pytest.mark.parametrize("path", sorted(REPO_CONFIG_DIR.glob("*.yaml")), ids=lambda p: p.name)
def test_repo_configs_parse_identically_under_both_loaders(path) -> None:
    """仓库自带的配置文件里没有 YAML 1.1 布尔别名：两种 loader 结果完全一致"""
    text = path.read_text(encoding="utf-8")
    assert yaml.load(text, Loader=base.StrictBoolSafeLoader) == yaml.safe_load(text)


@pytest.mark.parametrize("literal", ["'true'", '"TRUE"'])
def test_quoted_true_string_matches_env_rule(yaml_root, literal) -> None:
    """加引号的 'true' 是字符串，与环境变量规则一致（大小写不敏感）视为开启；见 docs/autofix_guide.md"""
    yaml_root(f"autofix:\n  repair_enabled: {literal}\n")
    assert AutoFixConfig().repair_enabled is True
