"""manual演练默认零进程，显式执行也不能被Docker环境覆盖到远端。"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def script():
    path = Path(__file__).resolve().parents[2] / "scripts/manual_checks/rag_stage_admission_redis.py"
    spec = importlib.util.spec_from_file_location("rag_stage_manual_safety_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("variable", ["DOCKER_HOST", "DOCKER_CONTEXT"])
def test_execute_refuses_endpoint_override_before_any_process(script, monkeypatch, variable):
    monkeypatch.setenv(variable, "remote-override-canary")
    monkeypatch.setattr("sys.argv", ["fixture", "--execute"])
    run = Mock(side_effect=AssertionError("override spawned a process"))
    monkeypatch.setattr(script.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="refuses Docker endpoint"):
        script.main()
    run.assert_not_called()


def test_default_dry_run_never_inspects_docker_even_with_overrides(script, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://192.0.2.1:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-fixture")
    monkeypatch.setattr("sys.argv", ["fixture"])
    run = Mock(side_effect=AssertionError("dry run spawned a process"))
    monkeypatch.setattr(script.subprocess, "run", run)
    assert script.main() == 0
    run.assert_not_called()


def test_local_endpoint_is_pinned_and_environment_mutation_cannot_redirect(script, monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/nonexistent-fixture-certificates")
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, dict(kwargs["env"])))
        return SimpleNamespace(stdout="unix:///tmp/owned-fixture.sock\n" if len(calls) == 1 else "owned result\n")

    monkeypatch.setattr(script.subprocess, "run", run)
    docker = script._local_docker()
    monkeypatch.setenv("DOCKER_HOST", "tcp://192.0.2.1:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-after-discovery")
    assert docker("version") == "owned result"
    assert calls[0][0] == ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
    assert calls[1][0] == ["docker", "--host", "unix:///tmp/owned-fixture.sock", "version"]
    assert all(not set(script._DOCKER_ENDPOINT_ENV).intersection(environment) for _, environment in calls)


@pytest.mark.parametrize("endpoint", ["ssh://host.example", "tcp://192.0.2.1:2375", "unix://host.example/socket", "unix:///"])
def test_nonlocal_discovery_never_runs_a_daemon_command(script, monkeypatch, endpoint):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    run = Mock(return_value=SimpleNamespace(stdout=endpoint))
    monkeypatch.setattr(script.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="local Unix-socket"):
        script._local_docker()
    assert run.call_count == 1
    assert run.call_args.args[0][1:3] == ["context", "inspect"]
