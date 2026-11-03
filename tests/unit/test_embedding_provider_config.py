"""嵌入配置与真实工厂分支保持一致；客户端全部替换，禁止真实模型调用。"""

import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from app.config import base, settings
from app.core.agents.enterprise_assistant import _create_embedding_model


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    # 不让本机 .env 的 provider、模型或凭据进入本测试的参数及失败输出。
    for key in list(os.environ):
        if key.startswith(("LLM_", "OLLAMA_", "RAG_")):
            monkeypatch.delenv(key)
    monkeypatch.setattr(settings, "CONFIG", {})
    monkeypatch.setattr(base, "_get_root_dir", lambda: tmp_path)
    monkeypatch.setenv("ENV", "development")
    directory = tmp_path / "config"
    directory.mkdir()

    def load_yaml(data):
        (directory / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        monkeypatch.setattr(settings, "CONFIG", settings.load_config())

    return load_yaml


@pytest.fixture
def embedding_clients(monkeypatch):
    openai = Mock(name="OpenAIEmbeddings")
    ollama = Mock(name="OllamaEmbeddings")
    for module_name, constructor_name, constructor in [
        ("langchain_openai", "OpenAIEmbeddings", openai),
        ("langchain_ollama", "OllamaEmbeddings", ollama),
    ]:
        module = ModuleType(module_name)
        setattr(module, constructor_name, constructor)
        monkeypatch.setitem(sys.modules, module_name, module)
    return SimpleNamespace(openai=openai, ollama=ollama)


@pytest.mark.parametrize("source", ["env", "yaml"])
@pytest.mark.parametrize(
    ("provider", "normalized", "model"),
    [
        ("openai", "openai", "Pro/BAAI/bge-m3"),
        (" OPENAI ", "OPENAI", "Pro/BAAI/bge-m3"),
        ("OpenAI # remote", "OpenAI", "Pro/BAAI/bge-m3"),
        ("ollama", "ollama", "nomic-embed-text"),
        (" OLLAMA ", "OLLAMA", "nomic-embed-text"),
        ("ollama # local", "ollama", "nomic-embed-text"),
        ("\tOllama\t# local\n", "Ollama", "nomic-embed-text"),
    ],
)
def test_provider_source_uses_same_normalization_for_llm_and_rag(isolated_config, monkeypatch, source, provider, normalized, model):
    if source == "yaml":
        isolated_config({"llm": {"provider": provider}})
    else:
        isolated_config({"llm": {"provider": "other-yaml-provider"}})
        monkeypatch.setenv("LLM_PROVIDER", provider)
    assert settings.LLMConfig().provider == normalized
    assert settings.RAGConfig().effective_embedding_model == model


def test_missing_provider_and_models_keep_defaults(isolated_config):
    assert settings.LLMConfig().provider == "openai"
    assert settings.RAGConfig().effective_embedding_model == "Pro/BAAI/bge-m3"


@pytest.mark.parametrize("provider", ["openai", "ollama"])
@pytest.mark.parametrize("explicit", ["custom-embedding-model", "", "  custom model # literal  "])
def test_explicit_embedding_env_overrides_yaml_without_rewriting_values(isolated_config, monkeypatch, provider, explicit):
    isolated_config({"llm": {"provider": provider}, "rag": {"openai_embedding_model": "yaml-openai", "ollama_embedding_model": "yaml-ollama"}})
    monkeypatch.setenv(f"RAG_{provider.upper()}_EMBEDDING_MODEL", explicit)
    monkeypatch.setenv("LLM_EMBEDDING_MODEL", "separate-llm-embedding-setting")
    llm = settings.LLMConfig()
    rag = settings.RAGConfig()
    assert llm.embedding_model == "separate-llm-embedding-setting"
    assert rag.effective_embedding_model == explicit


@pytest.mark.parametrize("provider", ["openai", "ollama"])
@pytest.mark.parametrize("explicit", ["custom-yaml-model", ""])
def test_explicit_yaml_embedding_is_selected_without_env_override(isolated_config, provider, explicit):
    isolated_config({"llm": {"provider": f" {provider.upper()} # comment", "embedding_model": "separate-llm-model"}, "rag": {f"{provider}_embedding_model": explicit}})
    assert settings.LLMConfig().embedding_model == "separate-llm-model"
    assert settings.RAGConfig().effective_embedding_model == explicit


@pytest.mark.parametrize("source", ["env", "yaml"])
@pytest.mark.parametrize("provider", [" OPENAI ", "OpenAI # remote", " OLLAMA ", "ollama # local"])
async def test_factory_uses_matching_client_and_provider_specific_model(isolated_config, embedding_clients, monkeypatch, source, provider):
    data = {
        "llm": {"api_key": "fixture-not-a-credential", "base_url": "https://provider.invalid/v1", "ollama_base_url": "http://127.0.0.1:11999/v1"},
        "rag": {"openai_embedding_model": "explicit-openai-embedding", "ollama_embedding_model": "explicit-ollama-embedding"},
    }
    if source == "yaml":
        data["llm"]["provider"] = provider
    else:
        monkeypatch.setenv("LLM_PROVIDER", provider)
    isolated_config(data)
    monkeypatch.setattr(settings, "config", SimpleNamespace(llm=settings.LLMConfig(), rag=settings.RAGConfig()))

    result = await _create_embedding_model()

    if provider.strip().lower().startswith("ollama"):
        assert result is embedding_clients.ollama.return_value
        embedding_clients.ollama.assert_called_once_with(model="explicit-ollama-embedding", base_url="http://127.0.0.1:11999")
        embedding_clients.openai.assert_not_called()
    else:
        assert result is embedding_clients.openai.return_value
        embedding_clients.openai.assert_called_once_with(model="explicit-openai-embedding", openai_api_key="fixture-not-a-credential", openai_api_base="https://provider.invalid/v1", request_timeout=10, max_retries=0)
        embedding_clients.ollama.assert_not_called()


@pytest.mark.parametrize("provider", ["openai", "ollama"])
async def test_factory_preserves_explicit_empty_embedding_model(isolated_config, embedding_clients, monkeypatch, provider):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv(f"RAG_{provider.upper()}_EMBEDDING_MODEL", "")
    monkeypatch.setattr(settings, "config", SimpleNamespace(llm=settings.LLMConfig(), rag=settings.RAGConfig()))
    await _create_embedding_model()
    selected = getattr(embedding_clients, provider)
    assert selected.call_args.kwargs["model"] == ""


@pytest.mark.parametrize("provider", ["", "unsupported # provider"])
async def test_unknown_provider_does_not_fall_back_to_a_client(isolated_config, embedding_clients, monkeypatch, provider):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setattr(settings, "config", SimpleNamespace(llm=settings.LLMConfig(), rag=settings.RAGConfig()))
    with pytest.raises(RuntimeError, match="Embedding model configuration is unavailable"):
        await _create_embedding_model()
    embedding_clients.openai.assert_not_called()
    embedding_clients.ollama.assert_not_called()


@pytest.mark.parametrize(("original", "current"), [("openai", "ollama"), ("ollama", "openai")])
@pytest.mark.parametrize("explicit", ["snapshot-selected-model", ""])
async def test_factory_uses_one_provider_snapshot_after_environment_changes(isolated_config, embedding_clients, monkeypatch, original, current, explicit):
    monkeypatch.setenv("LLM_PROVIDER", original)
    rag = settings.RAGConfig(**{f"{original}_embedding_model": explicit, f"{current}_embedding_model": "other-provider-model"})
    llm = settings.LLMConfig()
    monkeypatch.setattr(settings, "config", SimpleNamespace(llm=llm, rag=rag))
    monkeypatch.setenv("LLM_PROVIDER", current)

    # 已发布的动态属性仍读当前环境；工厂必须以已构造的 LLM 配置选择客户端和模型。
    assert rag.effective_embedding_model == "other-provider-model"
    assert llm.provider == original
    result = await _create_embedding_model()

    selected = getattr(embedding_clients, original)
    assert result is selected.return_value
    assert selected.call_args.kwargs["model"] == explicit
    getattr(embedding_clients, current).assert_not_called()


@pytest.mark.parametrize(("provider", "environment"), [("OPENAI", "ollama"), ("Ollama", "openai")])
@pytest.mark.parametrize("explicit", ["constructor-selected-model", ""])
async def test_explicit_llm_provider_constructor_selects_matching_embedding_model(isolated_config, embedding_clients, monkeypatch, provider, environment, explicit):
    monkeypatch.setenv("LLM_PROVIDER", environment)
    selected_provider = provider.lower()
    llm = settings.LLMConfig(provider=provider)
    rag = settings.RAGConfig(**{f"{selected_provider}_embedding_model": explicit, f"{environment}_embedding_model": "environment-only-model"})
    monkeypatch.setattr(settings, "config", SimpleNamespace(llm=llm, rag=rag))

    result = await _create_embedding_model()

    assert llm.provider == provider  # 不改展示值及显式构造参数的大小写。
    assert rag.effective_embedding_model == "environment-only-model"
    selected = getattr(embedding_clients, selected_provider)
    assert result is selected.return_value
    assert selected.call_args.kwargs["model"] == explicit
    getattr(embedding_clients, environment).assert_not_called()


async def test_explicit_unknown_provider_cannot_use_current_environment_client(isolated_config, embedding_clients, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "config", SimpleNamespace(llm=settings.LLMConfig(provider="unsupported"), rag=settings.RAGConfig()))
    with pytest.raises(RuntimeError, match="Embedding model configuration is unavailable"):
        await _create_embedding_model()
    embedding_clients.openai.assert_not_called()
    embedding_clients.ollama.assert_not_called()


@pytest.mark.parametrize("provider", ["openai", "ollama"])
async def test_factory_reports_generic_error_for_client_constructor_failure(isolated_config, embedding_clients, monkeypatch, provider):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setattr(settings, "config", SimpleNamespace(llm=settings.LLMConfig(), rag=settings.RAGConfig()))
    getattr(embedding_clients, provider).side_effect = ValueError("private-provider-fixture-error")
    with pytest.raises(RuntimeError) as raised:
        await _create_embedding_model()
    assert str(raised.value) == "Embedding model configuration is unavailable"
