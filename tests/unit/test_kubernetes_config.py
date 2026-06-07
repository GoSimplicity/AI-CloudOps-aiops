#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import pytest

from app.services import kubernetes


def _service_without_auto_init(monkeypatch):
    monkeypatch.setattr(
        kubernetes.KubernetesService, "_try_init", lambda self: None
    )
    return kubernetes.KubernetesService()


def test_load_config_prefers_kubeconfig_env_without_explicit_k8s_config(
    monkeypatch, tmp_path
) -> None:
    service = _service_without_auto_init(monkeypatch)
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    loaded_paths = []

    monkeypatch.delenv("K8S_CONFIG_PATH", raising=False)
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    monkeypatch.setattr(kubernetes.config.k8s, "in_cluster", False)
    monkeypatch.setattr(
        kubernetes.config.k8s, "config_path", "./deploy/kubernetes/config"
    )
    monkeypatch.setattr(
        kubernetes.k8s_config,
        "load_kube_config",
        lambda config_file=None: loaded_paths.append(config_file),
    )
    monkeypatch.setattr(
        kubernetes.k8s_config,
        "load_incluster_config",
        lambda: pytest.fail("不应加载集群内配置"),
    )

    service._load_config()

    assert loaded_paths == [str(kubeconfig)]


def test_load_config_uses_explicit_k8s_config_path(monkeypatch, tmp_path) -> None:
    service = _service_without_auto_init(monkeypatch)
    explicit_config = tmp_path / "explicit-kubeconfig"
    explicit_config.write_text("apiVersion: v1\n", encoding="utf-8")
    loaded_paths = []

    monkeypatch.setenv("K8S_CONFIG_PATH", str(explicit_config))
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "ignored-kubeconfig"))
    monkeypatch.setattr(kubernetes.config.k8s, "in_cluster", False)
    monkeypatch.setattr(kubernetes.config.k8s, "config_path", str(explicit_config))
    monkeypatch.setattr(
        kubernetes.k8s_config,
        "load_kube_config",
        lambda config_file=None: loaded_paths.append(config_file),
    )

    service._load_config()

    assert loaded_paths == [str(explicit_config)]


def test_load_config_keeps_absolute_project_like_config_path(
    monkeypatch, tmp_path
) -> None:
    service = _service_without_auto_init(monkeypatch)
    absolute_config = tmp_path / "app" / "deploy" / "kubernetes" / "config"
    absolute_config.parent.mkdir(parents=True)
    absolute_config.write_text("apiVersion: v1\n", encoding="utf-8")
    loaded_paths = []

    monkeypatch.delenv("K8S_CONFIG_PATH", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setattr(kubernetes.config.k8s, "in_cluster", False)
    monkeypatch.setattr(kubernetes.config.k8s, "config_path", str(absolute_config))
    monkeypatch.setattr(
        kubernetes.k8s_config,
        "load_kube_config",
        lambda config_file=None: loaded_paths.append(config_file),
    )

    service._load_config()

    assert loaded_paths == [str(absolute_config)]
