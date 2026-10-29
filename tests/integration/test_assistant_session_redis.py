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


@pytest.fixture
def owned_redis():
    if os.getenv("CLOUDOPS_TEST_ASSISTANT_REDIS") != "true":
        pytest.skip("explicit isolated Redis test opt-in required")
    suffix = secrets.token_hex(8)
    name, volume = "cloudops-session-" + suffix, "cloudops-session-vol-" + suffix
    subprocess.run(["docker", "volume", "create", "--label", "cloudops.session=" + suffix, volume], check=True, stdout=subprocess.DEVNULL, timeout=15)
    try:
        subprocess.run(
            [
                "docker",
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
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )

        def ready():
            for _ in range(100):
                ping = subprocess.run(["docker", "exec", name, "redis-cli", "PING"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                if ping.returncode == 0 and ping.stdout.strip() == "PONG":
                    result = subprocess.check_output(["docker", "port", name, "6379/tcp"], text=True, timeout=10).strip()
                    match = re.fullmatch(r"127\.0\.0\.1:(\d+)", result)
                    if match:
                        return int(match.group(1))
                    raise AssertionError("fixture Redis was not bound to loopback")
                time.sleep(0.1)
            raise AssertionError("fixture Redis did not become ready")

        def run(operation, payload=None, port=None):
            env = {**os.environ, "ENV": "test", "PYTHON_DOTENV_DISABLED": "1", "CLOUDOPS_OWNED_REDIS_PORT": str(port or ready()), "PYTHONPATH": str(ROOT)}
            result = subprocess.run([sys.executable, str(DRIVER), operation], input=json.dumps(payload or {}), text=True, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            if result.returncode:
                raise AssertionError("isolated session driver failed: " + result.stderr[-3000:])
            return json.loads(result.stdout.splitlines()[-1])

        def spawn(operation, payload=None):
            env = {**os.environ, "ENV": "test", "PYTHON_DOTENV_DISABLED": "1", "CLOUDOPS_OWNED_REDIS_PORT": str(ready()), "PYTHONPATH": str(ROOT)}
            process = subprocess.Popen([sys.executable, str(DRIVER), operation], text=True, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            process.stdin.write(json.dumps(payload or {}))
            process.stdin.close()
            process.stdin = None
            return process

        yield name, ready, run, spawn
    finally:
        inspected = subprocess.run(["docker", "inspect", "--format", '{{index .Config.Labels "cloudops.session"}}', name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        if inspected.returncode == 0:
            if inspected.stdout.strip() != suffix:
                raise AssertionError("refusing to remove non-owned container")
            subprocess.run(["docker", "rm", "-f", name], check=True, stdout=subprocess.DEVNULL, timeout=30)
        inspected_volume = subprocess.check_output(["docker", "volume", "inspect", "--format", '{{index .Labels "cloudops.session"}}', volume], text=True, timeout=10).strip()
        if inspected_volume != suffix:
            raise AssertionError("refusing to remove non-owned volume")
        subprocess.run(["docker", "volume", "rm", volume], check=True, stdout=subprocess.DEVNULL, timeout=15)


def test_durable_session_restart_owner_and_real_rag_mcp_history(owned_redis):
    name, ready, run, _ = owned_redis
    first = run("bootstrap")
    subprocess.run(["docker", "restart", name], check=True, stdout=subprocess.DEVNULL, timeout=30)
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
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr[-3000:]
        assert json.loads(stdout.splitlines()[-1])["completed"]
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
    subprocess.run(["docker", "exec", name, "redis-cli", "CONFIG", "SET", "appendonly", "no"], check=True, stdout=subprocess.DEVNULL, timeout=10)
    assert run("aof_disabled")["denied"]


def test_evicting_policy_is_rejected_before_any_turn(owned_redis):
    name, _, run, _ = owned_redis
    subprocess.run(["docker", "exec", name, "redis-cli", "CONFIG", "SET", "maxmemory-policy", "allkeys-lru"], check=True, stdout=subprocess.DEVNULL, timeout=10)
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
        subprocess.run(["docker", "exec", name, "redis-cli", "CONFIG", "SET", "maxmemory-policy", "allkeys-lru"], check=True, stdout=subprocess.DEVNULL, timeout=10)
        process.wait(timeout=10)
        assert process.returncode != 0
        subprocess.run(["docker", "exec", name, "redis-cli", "CONFIG", "SET", "maxmemory-policy", "noeviction"], check=True, stdout=subprocess.DEVNULL, timeout=10)
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
