"""Cross-process session checks using an owned, durable local Redis container."""

import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "tests/integration/assistant_session_redis_driver.py"


_DOCKER_ENDPOINT_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")


def _session_subprocess_run(arguments, **kwargs):
    __tracebackhide__ = True
    try:
        return subprocess.run(arguments, **kwargs)
    except subprocess.CalledProcessError as error:
        raise AssertionError(f"session subprocess failed (exit={int(error.returncode)})") from None
    except subprocess.TimeoutExpired:
        raise AssertionError("session subprocess timed out") from None
    except OSError:
        raise AssertionError("session subprocess unavailable") from None


def _driver_response(stdout, returncode):
    __tracebackhide__ = True
    if returncode != 0:
        raise AssertionError(f"isolated session driver failed (exit={int(returncode)})") from None
    try:
        value = json.loads(stdout.splitlines()[-1])
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, IndexError, AttributeError):
        raise AssertionError("isolated session driver response invalid") from None


def _driver_finish(process, timeout):
    __tracebackhide__ = True
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError("isolated session driver timed out") from None
    except OSError:
        raise AssertionError("isolated session driver unavailable") from None
    return _driver_response(stdout, process.returncode)


def _session_inspect_record(result, category):
    __tracebackhide__ = True
    try:
        records = json.loads(result.stdout)
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise ValueError
        return records[0]
    except (ValueError, TypeError, IndexError, AttributeError):
        raise AssertionError("session resource inspection invalid: " + category) from None


def _session_volume(docker, name, owner):
    result = docker.run("volume", "inspect", name, check=False)
    if result.returncode:
        return None
    record = _session_inspect_record(result, "volume")
    if record.get("Name") != name or record.get("Driver") != "local" or (record.get("Labels") or {}).get("cloudops.session") != owner or not record.get("Mountpoint") or not record.get("CreatedAt"):
        raise AssertionError("refusing a non-owned session volume")
    return record


def _cleanup_session_volume(docker, name, owner, original):
    current = _session_volume(docker, name, owner)
    if current is None:
        return
    # A failed create/first-inspect acknowledgement leaves no captured identity.
    # Recover it only from our exact random name+label+local driver, then read
    # again before deletion. Never turn an unproven resource into cleanup work.
    if original is None:
        original = current
        current = _session_volume(docker, name, owner)
        if current is None:
            return
    fields = ("Name", "Driver", "Mountpoint", "CreatedAt")
    if any(current.get(key) != original.get(key) for key in fields):
        raise AssertionError("refusing a replaced session volume")
    docker.run("volume", "rm", name, timeout=15)


class _SessionDocker:
    def __init__(self, endpoint, environment):
        self.endpoint = endpoint
        self.environment = environment

    def run(self, *arguments, **kwargs):
        kwargs.setdefault("check", True)
        kwargs.setdefault("text", True)
        kwargs.setdefault("timeout", 30)
        if "capture_output" not in kwargs:
            kwargs.setdefault("stdout", subprocess.PIPE)
            kwargs.setdefault("stderr", subprocess.PIPE)
        return _session_subprocess_run(["docker", "--host", self.endpoint, *arguments], env=self.environment, **kwargs)


def _local_session_docker():
    environment = dict(os.environ)
    if environment.get("DOCKER_HOST") or environment.get("DOCKER_CONTEXT"):
        raise RuntimeError("session fixture refuses Docker endpoint overrides")
    for key in _DOCKER_ENDPOINT_ENV:
        environment.pop(key, None)
    # Context inspection reads local CLI metadata; no daemon is contacted until
    # the endpoint is verified and pinned on every subsequent daemon command.
    result = _session_subprocess_run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], env=environment, capture_output=True, text=True, check=True, timeout=15)
    endpoint = result.stdout.strip()
    import stat
    from urllib.parse import urlsplit

    parsed = urlsplit(endpoint)
    if parsed.scheme != "unix" or parsed.netloc or parsed.query or parsed.fragment or not parsed.path.startswith("/"):
        raise RuntimeError("session fixture requires a local Unix socket")
    path = Path(parsed.path).resolve(strict=True)
    if not stat.S_ISSOCK(path.stat().st_mode):
        raise RuntimeError("session fixture endpoint is not a Unix socket")
    return _SessionDocker("unix://" + str(path), environment)


class _OwnedSessionRedis:
    def __init__(self, docker, name, full_id, owner):
        self.docker, self.name, self.full_id, self.owner = docker, name, full_id, owner

    def verify(self):
        result = self.docker.run("inspect", self.full_id, check=False)
        if result.returncode:
            return False
        record = _session_inspect_record(result, "container")
        if record.get("Id") != self.full_id or record.get("Name") != "/" + self.name or (record.get("Config", {}).get("Labels") or {}).get("cloudops.session") != self.owner:
            raise AssertionError("refusing a non-owned session container")
        return True

    def command(self, operation, *arguments, **kwargs):
        if operation not in {"exec", "port", "restart"} or not self.verify():
            raise AssertionError("owned session container unavailable")
        return self.docker.run(operation, self.full_id, *arguments, **kwargs)


@pytest.fixture
def owned_redis():
    if os.getenv("CLOUDOPS_TEST_ASSISTANT_REDIS") != "true":
        pytest.skip("explicit isolated Redis test opt-in required")
    docker = _local_session_docker()
    suffix = secrets.token_hex(8)
    name, volume = "cloudops-session-" + suffix, "cloudops-session-vol-" + suffix
    owned = None
    volume_before = None
    volume_attempted = False
    try:
        # Track the attempt before invoking create: the daemon may commit it
        # even if the CLI acknowledgement is lost.
        volume_attempted = True
        docker.run("volume", "create", "--label", "cloudops.session=" + suffix, volume, timeout=15)
        volume_before = _session_volume(docker, volume, suffix)
        if volume_before is None:
            raise AssertionError("new session volume missing")
        result = docker.run(
            "run",
            "-d",
            "--pull=never",
            "--name",
            name,
            "--label",
            "cloudops.session=" + suffix,
            "--mount",
            f"type=volume,src={volume},dst=/data",
            "-p",
            "127.0.0.1::6379",
            "redis:7.4.11-alpine",
            "redis-server",
            "--save",
            "",
            "--appendonly",
            "yes",
            "--appendfsync",
            "always",
        )
        full_id = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", full_id):
            raise AssertionError("Docker did not return a full container ID")
        owned = _OwnedSessionRedis(docker, name, full_id, suffix)
        if not owned.verify():
            raise AssertionError("new session container missing")

        def ready():
            for _ in range(100):
                ping = owned.command("exec", "redis-cli", "PING", check=False)
                if ping.returncode == 0 and ping.stdout.strip() == "PONG":
                    address = owned.command("port", "6379/tcp", timeout=10).stdout.strip()
                    match = re.fullmatch(r"127\.0\.0\.1:(\d+)", address)
                    if match:
                        return int(match.group(1))
                    raise AssertionError("fixture Redis was not bound to loopback")
                time.sleep(0.1)
            raise AssertionError("fixture Redis did not become ready")

        def run(operation, payload=None, port=None):
            env = {**os.environ, "ENV": "test", "PYTHON_DOTENV_DISABLED": "1", "CLOUDOPS_OWNED_REDIS_PORT": str(port or ready()), "PYTHONPATH": str(ROOT)}
            result = _session_subprocess_run([sys.executable, str(DRIVER), operation], input=json.dumps(payload or {}), text=True, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            return _driver_response(result.stdout, result.returncode)

        def spawn(operation, payload=None):
            env = {**os.environ, "ENV": "test", "PYTHON_DOTENV_DISABLED": "1", "CLOUDOPS_OWNED_REDIS_PORT": str(ready()), "PYTHONPATH": str(ROOT)}
            try:
                process = subprocess.Popen([sys.executable, str(DRIVER), operation], text=True, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError:
                raise AssertionError("isolated session driver unavailable") from None
            process.stdin.write(json.dumps(payload or {}))
            process.stdin.close()
            process.stdin = None
            return process

        yield owned, ready, run, spawn
    finally:
        # If create succeeded but its output was lost, discover only by the exact
        # random name+label, capture its full ID, then verify again by that ID.
        if owned is None:
            found = docker.run("inspect", name, check=False)
            if found.returncode == 0:
                record = _session_inspect_record(found, "create recovery")
                if record.get("Name") != "/" + name or (record.get("Config", {}).get("Labels") or {}).get("cloudops.session") != suffix or not re.fullmatch(r"[0-9a-f]{64}", record.get("Id", "")):
                    raise AssertionError("refusing an unowned create-recovery container")
                owned = _OwnedSessionRedis(docker, name, record["Id"], suffix)
        if owned is not None and owned.verify():
            docker.run("rm", "-f", owned.full_id)
        if volume_attempted:
            _cleanup_session_volume(docker, volume, suffix, volume_before)


def test_durable_session_restart_owner_and_real_rag_mcp_history(owned_redis):
    name, ready, run, _ = owned_redis
    first = run("bootstrap")
    name.command("restart", stdout=subprocess.DEVNULL, timeout=30)
    ready()
    second = run("continue", first)
    assert run("replay", second)["replayed"]
    assert run("mcp", second)["mcp"]
    info = run("inspect", first)
    assert info["revision"] == 3 and info["status"] == "idle"
    assert [turn["q"] for turn in info["history"]] == ["first-question", "second-question", "mcp-question"]
    assert run("foreign", {**first, "owner": "user:2"})["denied"]
    assert run("abandon", first)["unknown"]
    assert run("inspect", first)["status"] == "unknown"


def test_cross_process_lease_renewal_blocks_concurrent_turn(owned_redis, tmp_path):
    _, _, run, spawn = owned_redis
    first = run("bootstrap")
    entered, release = tmp_path / "entered", tmp_path / "release"
    import uuid

    process = spawn("block", {**first, "turn_id": str(uuid.uuid4()), "entered": str(entered), "release": str(release)})
    try:
        deadline = time.monotonic() + 15
        while not entered.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert entered.exists(), "fixture model did not start"
        time.sleep(0.8)  # exceeds the test-only 400ms lease; heartbeat must renew it
        assert run("busy", first)["busy"]
        release.write_text("release")
        completed = _driver_finish(process, timeout=30)
        assert completed["completed"]
        assert run("inspect", first)["revision"] == 2
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_lost_commit_ack_replays_stable_turn_without_model_call(owned_redis):
    _, _, run, _ = owned_redis
    first = run("bootstrap")
    uncertain = run("fail_commit", first)
    assert run("replay_lost", uncertain)["replayed"]
    assert run("inspect", first)["revision"] == 2


def test_no_aof_is_rejected_without_model_call(owned_redis):
    name, _, run, _ = owned_redis
    name.command("exec", "redis-cli", "CONFIG", "SET", "appendonly", "no", stdout=subprocess.DEVNULL, timeout=10)
    assert run("aof_disabled")["denied"]


def test_evicting_policy_is_rejected_before_any_turn(owned_redis):
    name, _, run, _ = owned_redis
    name.command("exec", "redis-cli", "CONFIG", "SET", "maxmemory-policy", "allkeys-lru", stdout=subprocess.DEVNULL, timeout=10)
    assert run("unsafe_eviction")["denied"]


def test_policy_change_during_running_turn_stops_and_marks_unknown(owned_redis, tmp_path):
    import uuid

    name, _, run, spawn = owned_redis
    first = run("bootstrap")
    entered, release = tmp_path / "entered", tmp_path / "release"
    process = spawn("block", {**first, "turn_id": str(uuid.uuid4()), "entered": str(entered), "release": str(release)})
    try:
        deadline = time.monotonic() + 15
        while not entered.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert entered.exists(), "fixture model did not start"
        name.command("exec", "redis-cli", "CONFIG", "SET", "maxmemory-policy", "allkeys-lru", stdout=subprocess.DEVNULL, timeout=10)
        process.wait(timeout=10)
        assert process.returncode != 0
        name.command("exec", "redis-cli", "CONFIG", "SET", "maxmemory-policy", "noeviction", stdout=subprocess.DEVNULL, timeout=10)
        time.sleep(0.6)
        assert run("inspect", first)["status"] == "unknown"
        assert run("busy", first)["busy"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_real_http_and_websocket_preserve_turn_id_and_owner(owned_redis):
    _, _, run, _ = owned_redis
    assert run("asgi")["asgi"]


def test_history_and_turn_id_storage_are_bounded(owned_redis):
    _, _, run, _ = owned_redis
    assert run("bounds")["bounded"]


def test_corrupt_active_request_is_not_exposed(owned_redis):
    _, _, run, _ = owned_redis
    assert run("corrupt_active")["rejected"]


def test_killed_worker_becomes_unknown_without_reexecution(owned_redis, tmp_path):
    import uuid

    _, _, run, spawn = owned_redis
    first = run("bootstrap")
    entered, release = tmp_path / "entered", tmp_path / "release"
    turn = str(uuid.uuid4())
    process = spawn("block", {**first, "turn_id": turn, "entered": str(entered), "release": str(release)})
    try:
        deadline = time.monotonic() + 15
        while not entered.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert entered.exists(), "fixture model did not start"
        process.kill()
        process.wait(timeout=10)
        time.sleep(0.6)  # wait for this test's 400ms lease to expire
        info = run("inspect", first)
        assert info["status"] == "unknown" and info["active_turn_id"] == turn
        assert info["active_question"] == "held-question" and info["revision"] == 1
        assert run("busy", first)["busy"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_implicit_first_turn_failure_exposes_safe_locator_without_reexecution(owned_redis):
    _, _, run, _ = owned_redis
    result = run("implicit_recovery")
    assert result == {"safe_locator": True, "unknown_visible": True, "not_reexecuted": True, "foreign_denied": True}


def test_real_http_and_websocket_failures_share_recovery_locator(owned_redis):
    _, _, run, _ = owned_redis
    assert run("recovery_asgi") == {"http_ws_locator": True, "unknown_visible": True, "not_reexecuted": True, "foreign_denied": True}


def test_begin_checks_foreign_owner_before_indexes_and_preserves_own_index_guard(owned_redis):
    _, _, run, _ = owned_redis
    assert run("begin_owner_order") == {"foreign_fresh_existing": True, "own_bad_indexes": True, "unchanged": True, "model_calls": 0}
