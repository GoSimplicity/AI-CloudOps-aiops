"""配置路径及错误边界：全部使用临时文件，不读取本机密钥或访问网络。"""

from pathlib import Path
import traceback

import pytest
import yaml

from app.config import base, settings

PRIVATE = "private-config-canary-9217"


@pytest.fixture
def config_root(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(base, "_get_root_dir", lambda: tmp_path)
    monkeypatch.setenv("ENV", "production")
    return tmp_path / "config"


@pytest.mark.parametrize(
    ("env", "filename"),
    [
        ("production", "config.production.yaml"),
        ("prod", "config.production.yaml"),
        ("PRD", "config.production.yaml"),
        (" Production\n", "config.production.yaml"),
        ("development", "config.yaml"),
        ("dev", "config.yaml"),
        (" DEV ", "config.yaml"),
        ("test", "config.test.yaml"),
        ("testing", "config.test.yaml"),
        (" TESTING ", "config.test.yaml"),
        ("staging", "config.staging.yaml"),
        ("", "config.yaml"),
    ],
)
def test_environment_aliases_choose_canonical_file(config_root, monkeypatch, env, filename):
    (config_root / "config.yaml").write_text("app:\n  port: 8111\n", encoding="utf-8")
    (config_root / "config.production.yaml").write_text("app:\n  port: 8222\n", encoding="utf-8")
    (config_root / "config.test.yaml").write_text("app:\n  port: 8333\n", encoding="utf-8")
    (config_root / "config.staging.yaml").write_text("app:\n  port: 8444\n", encoding="utf-8")
    monkeypatch.setenv("ENV", env)
    assert base._get_yaml_path() == config_root / filename
    assert settings.load_config()["app"]["port"] == {
        "config.yaml": 8111,
        "config.production.yaml": 8222,
        "config.test.yaml": 8333,
        "config.staging.yaml": 8444,
    }[filename]


def test_unset_environment_keeps_default_config(config_root, monkeypatch):
    monkeypatch.delenv("ENV")
    assert base._get_yaml_path() == config_root / "config.yaml"


@pytest.mark.parametrize("strict_bool", [False, True])
def test_missing_environment_file_falls_back_to_default(config_root, strict_bool):
    (config_root / "config.yaml").write_text("app:\n  port: 8111\n", encoding="utf-8")
    assert settings.load_config(strict_bool=strict_bool) == {"app": {"port": 8111}}


def test_missing_files_keep_environment_only_configuration(config_root, monkeypatch):
    assert settings.load_config() == {}
    monkeypatch.setattr(settings, "CONFIG", settings.load_config())
    monkeypatch.setenv("PORT", "8444")
    monkeypatch.setenv("DEBUG", "false")
    app_config = settings.AppConfig()
    assert app_config.port == 8444
    assert app_config.debug is False


@pytest.mark.parametrize("strict_bool", [False, True])
@pytest.mark.parametrize("text", ["", "# empty configuration\n", "{}\n"])
def test_empty_and_mapping_configuration_are_valid(config_root, strict_bool, text):
    (config_root / "config.production.yaml").write_text(text, encoding="utf-8")
    assert settings.load_config(strict_bool=strict_bool) == {}


@pytest.mark.parametrize("strict_bool", [False, True])
@pytest.mark.parametrize("text", ["- item\n", "false\n", "0\n", "''\n", "private-config-canary-9217\n"])
def test_non_mapping_root_is_rejected(config_root, strict_bool, text):
    (config_root / "config.production.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(base.ConfigLoadError, match="根节点必须是映射") as raised:
        settings.load_config(strict_bool=strict_bool)
    assert PRIVATE not in str(raised.value)


@pytest.mark.parametrize("strict_bool", [False, True])
@pytest.mark.parametrize("filename", ["config.production.yaml", "config.yaml"])
def test_invalid_yaml_never_falls_back_or_leaks_source(config_root, strict_bool, filename, caplog):
    (config_root / "config.yaml").write_text("app:\n  port: 8111\n", encoding="utf-8")
    (config_root / filename).write_text(f"private: {PRIVATE}\napp: [\n", encoding="utf-8")
    with pytest.raises(base.ConfigLoadError, match="无法解析 YAML") as raised:
        settings.load_config(strict_bool=strict_bool)
    formatted_error = "".join(traceback.format_exception(raised.value))
    assert PRIVATE not in formatted_error
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("strict_bool", [False, True])
def test_invalid_utf8_is_rejected(config_root, strict_bool):
    (config_root / "config.production.yaml").write_bytes(b"private: \xff")
    with pytest.raises(base.ConfigLoadError, match="UTF-8"):
        settings.load_config(strict_bool=strict_bool)


@pytest.mark.parametrize("strict_bool", [False, True])
@pytest.mark.parametrize("filename", ["config.production.yaml", "config.yaml"])
def test_unreadable_file_never_falls_back(config_root, monkeypatch, strict_bool, filename):
    original_read_text = Path.read_text
    attempted_paths = []

    def read_text(path, *args, **kwargs):
        attempted_paths.append(path)
        if path == config_root / filename:
            raise PermissionError(PRIVATE)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(base.ConfigLoadError, match="无法读取 YAML") as raised:
        settings.load_config(strict_bool=strict_bool)
    assert attempted_paths[-1] == config_root / filename
    assert PRIVATE not in "".join(traceback.format_exception(raised.value))


def test_environment_variables_do_not_hide_broken_yaml(config_root, monkeypatch):
    (config_root / "config.production.yaml").write_text("app: [\n", encoding="utf-8")
    monkeypatch.setenv("PORT", "8444")
    with pytest.raises(base.ConfigLoadError):
        settings.load_config()


def test_invalid_explicit_bool_outside_gate_is_configuration_error(config_root):
    (config_root / "config.production.yaml").write_text(f"private: {PRIVATE}\napp:\n  debug: !!bool maybe\n", encoding="utf-8")
    with pytest.raises(base.ConfigLoadError) as raised:
        settings.load_config()
    assert PRIVATE not in "".join(traceback.format_exception(raised.value))


def test_invalid_gate_bool_keeps_other_configuration_and_strict_warning_view(config_root):
    (config_root / "config.production.yaml").write_text("app:\n  port: 8222\nautofix:\n  repair_enabled: !!bool maybe\n", encoding="utf-8")
    assert settings.load_config() == {"app": {"port": 8222}, "autofix": {"repair_enabled": False}}
    strict = settings.load_config(strict_bool=True)
    assert strict["app"]["port"] == 8222
    assert isinstance(strict["autofix"]["repair_enabled"], base.InvalidExplicitBool)


@pytest.mark.parametrize(
    "text",
    [
        "autofix:\n  repair_enabled: &bad !!bool maybe\napp:\n  debug: *bad\n",
        "autofix:\n  repair_enabled: [!!bool maybe]\n",
        "extra:\n  autofix:\n    repair_enabled: !!bool maybe\n",
        "autofix: &gates\n  repair_enabled: !!bool maybe\nextra: *gates\n",
        "? !!bool maybe\n: value\n",
    ],
)
def test_invalid_bool_gate_exception_cannot_cover_other_paths(config_root, text):
    (config_root / "config.production.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(base.ConfigLoadError, match="非法显式布尔"):
        settings.load_config()


def test_normal_loader_keeps_yaml_aliases_and_cycles(config_root):
    (config_root / "config.production.yaml").write_text("app: &app\n  debug: yes\n  recursive: *app\nother: *app\n", encoding="utf-8")
    loaded = settings.load_config()
    assert loaded["app"]["debug"] is True
    assert loaded["app"] is loaded["other"]
    assert loaded["app"]["recursive"] is loaded["app"]


@pytest.mark.parametrize(
    "text",
    [
        "extra: !!set\n  ? !!bool private-config-canary-9217\n",
        "extra: !!omap\n  - debug: !!bool private-config-canary-9217\n",
        "extra: !!pairs\n  - debug: !!bool private-config-canary-9217\n",
        "autofix:\n  repair_enabled: &bad !!bool private-config-canary-9217\nextra: !!set\n  ? *bad\n",
        "autofix:\n  repair_enabled: &bad !!bool private-config-canary-9217\nextra: !!omap\n  - debug: *bad\n",
        "autofix:\n  repair_enabled: &bad !!bool private-config-canary-9217\nextra: !!pairs\n  - debug: *bad\n",
    ],
)
def test_yaml_tag_containers_cannot_hide_invalid_bool_values(config_root, text, caplog):
    (config_root / "config.production.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(base.ConfigLoadError, match="非法显式布尔") as raised:
        settings.load_config()
    assert PRIVATE not in "".join(traceback.format_exception(raised.value))
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize(
    "text",
    [
        "extra: !!set\n  ? first\n  ? true\n",
        "extra: !!omap\n  - first: yes\n  - second: 2\n",
        "extra: !!pairs\n  - first: on\n  - first: 2\n",
    ],
)
def test_normal_yaml_tag_containers_keep_safe_loader_contract(config_root, text):
    (config_root / "config.production.yaml").write_text(text, encoding="utf-8")
    assert settings.load_config() == yaml.safe_load(text)


def test_pydantic_configuration_source_propagates_file_error(config_root):
    (config_root / "config.production.yaml").write_text("app: [\n", encoding="utf-8")
    with pytest.raises(base.ConfigLoadError):
        settings.Settings(_env_file=None)
