"""Exercise fixture routing/ownership with subprocess substitutes, never Docker."""

import json
from pathlib import Path
import socket
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from tests.integration import test_assistant_session_redis as fixture


@pytest.mark.parametrize("variable", ["DOCKER_HOST", "DOCKER_CONTEXT"])
def test_session_fixture_rejects_endpoint_overrides_before_any_cli(monkeypatch, variable):
    monkeypatch.setenv(variable, "untrusted")
    run = []
    monkeypatch.setattr(fixture.subprocess, "run", lambda *args, **kwargs: run.append(args))
    with pytest.raises(RuntimeError, match="overrides"):
        fixture._local_session_docker()
    assert run == []


@pytest.mark.parametrize("endpoint", ["tcp://127.0.0.1:2375", "ssh://host", "unix://host/socket"])
def test_session_fixture_rejects_nonlocal_contexts_without_daemon_calls(monkeypatch, endpoint):
    for key in fixture._DOCKER_ENDPOINT_ENV:
        monkeypatch.delenv(key, raising=False)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=endpoint, stderr="")

    monkeypatch.setattr(fixture.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="local Unix"):
        fixture._local_session_docker()
    assert len(calls) == 1 and calls[0][1:3] == ["context", "inspect"]


def test_session_fixture_pins_verified_socket_and_clean_environment(monkeypatch):
    for key in fixture._DOCKER_ENDPOINT_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    with tempfile.TemporaryDirectory(prefix="cloudops-session-guard-", dir="/tmp") as directory:
        path = Path(directory) / "docker.sock"
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(path))
            calls = []

            def run(args, **kwargs):
                calls.append((args, kwargs["env"]))
                return subprocess.CompletedProcess(args, 0, stdout="unix://" + str(path), stderr="")

            monkeypatch.setattr(fixture.subprocess, "run", run)
            docker = fixture._local_session_docker()
            expected_endpoint = "unix://" + str(path.resolve())
            monkeypatch.setenv("DOCKER_HOST", "tcp://not-the-fixture:2375")
            docker.run("version")
    assert calls[-1][0][:3] == ["docker", "--host", expected_endpoint]
    assert all(key not in calls[-1][1] for key in fixture._DOCKER_ENDPOINT_ENV)


def test_session_fixture_rejects_regular_file_endpoint(monkeypatch, tmp_path):
    for key in fixture._DOCKER_ENDPOINT_ENV:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "not-a-socket"
    path.write_text("synthetic")
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="unix://" + str(path), stderr="")

    monkeypatch.setattr(fixture.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="not a Unix socket"):
        fixture._local_session_docker()
    assert len(calls) == 1


@pytest.mark.parametrize("changed", ["Id", "Name", "label"])
def test_session_container_identity_mismatch_stops_mutation(changed):
    full_id = "a" * 64
    record = {"Id": full_id, "Name": "/fixture", "Config": {"Labels": {"cloudops.session": "owner"}}}
    if changed == "label":
        record["Config"]["Labels"]["cloudops.session"] = "other"
    else:
        record[changed] = "other"
    calls = []

    def run(*args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps([record]))

    owned = fixture._OwnedSessionRedis(SimpleNamespace(run=run), "fixture", full_id, "owner")
    with pytest.raises(AssertionError, match="non-owned"):
        owned.command("restart")
    assert calls == [("inspect", full_id)]


def test_session_container_mutation_targets_full_id():
    full_id = "a" * 64
    record = {"Id": full_id, "Name": "/fixture", "Config": {"Labels": {"cloudops.session": "owner"}}}
    calls = []

    def run(*args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps([record]))

    owned = fixture._OwnedSessionRedis(SimpleNamespace(run=run), "fixture", full_id, "owner")
    owned.command("restart")
    assert calls == [("inspect", full_id), ("restart", full_id)]


@pytest.mark.parametrize("failure", ["create_lost_reply", "first_inspect_lost_reply"])
def test_session_volume_cleanup_survives_creation_ack_or_initial_inspect_failure(monkeypatch, failure):
    monkeypatch.setenv("CLOUDOPS_TEST_ASSISTANT_REDIS", "true")
    monkeypatch.setattr(fixture.secrets, "token_hex", lambda _size: "f" * 16)
    docker = fixture._SessionDocker("unix:///tmp/synthetic-session.sock", {})
    monkeypatch.setattr(fixture, "_local_session_docker", lambda: docker)
    volume = "cloudops-session-vol-" + "f" * 16
    record = {"Name": volume, "Driver": "local", "Mountpoint": "/synthetic/owned/data", "CreatedAt": "synthetic-created", "Labels": {"cloudops.session": "f" * 16}}
    calls = []
    inspect_count = 0
    exists = False

    def run(arguments, **_kwargs):
        nonlocal inspect_count, exists
        assert arguments[:3] == ["docker", "--host", docker.endpoint]
        command = arguments[3:]
        calls.append(command)
        if command[:2] == ["volume", "create"]:
            exists = True
            if failure == "create_lost_reply":
                raise subprocess.TimeoutExpired(arguments, 1, output="private-output-canary", stderr="private-error-canary")
            return subprocess.CompletedProcess(arguments, 0, stdout=volume, stderr="")
        if command[0] == "inspect":
            return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="not found")
        if command[:2] == ["volume", "inspect"]:
            inspect_count += 1
            if failure == "first_inspect_lost_reply" and inspect_count == 1:
                raise subprocess.TimeoutExpired(arguments, 1, output="private-output-canary", stderr="private-error-canary")
            return subprocess.CompletedProcess(arguments, 0 if exists else 1, stdout=json.dumps([record]), stderr="")
        if command[:2] == ["volume", "rm"]:
            exists = False
            return subprocess.CompletedProcess(arguments, 0, stdout=volume, stderr="")
        raise AssertionError("unexpected synthetic command")

    monkeypatch.setattr(fixture.subprocess, "run", run)
    generator = fixture.owned_redis.__wrapped__()
    with pytest.raises(AssertionError, match="timed out") as raised:
        next(generator)
    assert "private-" not in str(raised.value)
    assert not exists
    assert ["volume", "rm", volume] in calls
    assert inspect_count >= 2


@pytest.mark.parametrize("field", ["Name", "Driver", "owner", "Mountpoint", "CreatedAt"])
def test_session_volume_cleanup_does_not_delete_unproven_or_replaced_identity(field):
    original = {"Name": "owned", "Driver": "local", "Mountpoint": "/synthetic/a", "CreatedAt": "created-a", "Labels": {"cloudops.session": "owner"}}
    current = {**original, "Labels": dict(original["Labels"])}
    if field == "owner":
        current["Labels"]["cloudops.session"] = "foreign"
    else:
        current[field] = "foreign"
    calls = []

    def run(*arguments, **_kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout=json.dumps([current]))

    with pytest.raises(AssertionError, match="refusing"):
        fixture._cleanup_session_volume(SimpleNamespace(run=run), "owned", "owner", original)
    assert all(arguments[:2] != ("volume", "rm") for arguments in calls)


@pytest.mark.parametrize("code", [1, -9, 127])
def test_session_driver_failure_never_echoes_captured_output(code):
    import traceback

    canary = "private-driver-token-canary"
    with pytest.raises(AssertionError) as raised:
        fixture._driver_response(canary, code)
    rendered = "".join(traceback.format_exception(raised.type, raised.value, raised.tb))
    assert canary not in rendered
    assert str(code) in str(raised.value)


@pytest.mark.parametrize("kind", ["timeout", "nonzero", "oserror"])
def test_session_subprocess_exception_is_fixed_and_suppresses_original_text(monkeypatch, kind):
    import traceback

    canary = "private-subprocess-token-canary"

    def run(*_args, **_kwargs):
        if kind == "timeout":
            raise subprocess.TimeoutExpired([canary], 1, output=canary, stderr=canary)
        if kind == "nonzero":
            raise subprocess.CalledProcessError(17, [canary], output=canary, stderr=canary)
        raise OSError(canary)

    monkeypatch.setattr(fixture.subprocess, "run", run)
    with pytest.raises(AssertionError) as raised:
        fixture._session_subprocess_run(["synthetic"])
    rendered = "".join(traceback.format_exception(raised.type, raised.value, raised.tb))
    assert canary not in rendered


def test_session_driver_invalid_json_and_async_failure_do_not_echo_stderr():
    canary = "private-driver-token-canary"
    with pytest.raises(AssertionError, match="response invalid") as invalid:
        fixture._driver_response(canary, 0)
    assert canary not in str(invalid.value)
    process = SimpleNamespace(communicate=lambda **_kwargs: (canary, canary), returncode=19)
    with pytest.raises(AssertionError, match="exit=19") as failed:
        fixture._driver_finish(process, timeout=1)
    assert canary not in str(failed.value)


def test_session_docker_failure_stderr_is_captured_even_when_stdout_is_discarded(monkeypatch):
    observed = []

    def run(arguments, **kwargs):
        observed.append(kwargs)
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr(fixture.subprocess, "run", run)
    fixture._SessionDocker("unix:///tmp/synthetic-session.sock", {}).run("version", stdout=subprocess.DEVNULL)
    assert observed[0]["stdout"] == subprocess.DEVNULL
    assert observed[0]["stderr"] == subprocess.PIPE
