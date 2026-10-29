"""隔离 Redis 中验证 generation 租约、CAS 和回收互斥。"""
import asyncio
import json
import os
import select
import subprocess
import uuid

import pytest

from app.core.vector.generation_gc import GenerationConflict, GenerationGC, GenerationLeaseLost


class RedisCLI:
    """通过容器内部 redis-cli 保持默认测试的离线网络边界。"""
    def __init__(self, container):
        self.container = container

    def command(self, *args):
        args = [arg.decode() if isinstance(arg, bytes) else str(arg) for arg in args]
        result = subprocess.run(["docker", "exec", self.container, "redis-cli", "--json", *args], text=True, capture_output=True, timeout=10, check=True)
        if result.stdout.startswith("error:"):
            raise RuntimeError("Redis 执行失败")
        return json.loads(result.stdout)

    def eval(self, script, numkeys, *args):
        return self.command("EVAL", script, numkeys, *args)

    def zrem(self, key, member):
        return self.command("ZREM", key, member)

    def srem(self, key, member):
        return self.command("SREM", key, member)

    def delete(self, *keys):
        return self.command("DEL", *keys)

    def hgetall(self, key):
        values = self.command("HGETALL", key)
        return values if isinstance(values, dict) else dict(zip(values[::2], values[1::2]))

    def scan_iter(self, match, count):
        cursor = "0"
        while True:
            cursor, keys = self.command("SCAN", cursor, "MATCH", match, "COUNT", count)
            yield from keys
            if str(cursor) == "0":
                return

    def sscan_iter(self, key, count):
        cursor = "0"
        while True:
            cursor, keys = self.command("SSCAN", key, cursor, "COUNT", count)
            yield from keys
            if str(cursor) == "0":
                return


@pytest.fixture
def isolated_redis():
    if os.getenv("CLOUDOPS_TEST_GENERATION_GC") != "true":
        pytest.skip("requires explicitly enabled isolated Redis test")
    name = "cloudops-gc-test-" + uuid.uuid4().hex
    subprocess.run(["docker", "run", "-d", "--rm", "--pull=never", "--network", "none", "--name", name, "--label", "cloudops.gc.run=" + name,
                    "--mount", "type=tmpfs,destination=/data", "redis:7.4.11-alpine", "redis-server", "--save", "", "--appendonly", "no"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
    try:
        client = RedisCLI(name)
        for _ in range(20):
            try:
                if client.command("PING") == "PONG":
                    break
            except (OSError, subprocess.SubprocessError, ValueError):
                continue
        else:
            raise RuntimeError("隔离 Redis 未就绪")
        yield client
    finally:
        label = subprocess.check_output(["docker", "inspect", "--format", '{{index .Config.Labels "cloudops.gc.run"}}', name], text=True, timeout=10).strip()
        if label != name:
            raise RuntimeError("拒绝清理非本次 Redis")
        subprocess.run(["docker", "rm", "-f", name], check=True, stdout=subprocess.DEVNULL, timeout=10)


def generation():
    return "knowledge-refresh-" + uuid.uuid4().hex


async def test_published_generation_is_pinned_for_inflight_reader(isolated_redis):
    client = isolated_redis
    writer = GenerationGC(client, "knowledge", lease_ms=30000)
    other = GenerationGC(client, "knowledge", lease_ms=30000)
    first = await writer.acquire_stage(generation())
    client.command("SET", first.generation + ":doc:a", "first")
    await writer.protect_unknown(first)
    await writer.publish_cas(first)
    await writer.confirm_published(first)
    await first.close()
    reader = await other.acquire_active()
    second = await writer.acquire_stage(generation())
    client.command("SET", second.generation + ":doc:b", "second")
    await writer.protect_unknown(second)
    await writer.publish_cas(second)
    await writer.confirm_published(second)
    await second.close()
    assert await writer.collect(first.generation) is False
    await reader.check()
    assert client.command("GET", first.generation + ":doc:a") == "first"
    await reader.close()
    assert await writer.collect(first.generation) is True
    assert client.command("GET", first.generation + ":doc:a") is None
    assert client.command("GET", second.generation + ":doc:b") == "second"
    assert await writer.collect(second.generation) is False


async def test_unknown_commit_cannot_be_automatically_collected(isolated_redis):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    stage = await gc.acquire_stage(generation())
    client.command("SET", stage.generation + ":doc:a", "staged")
    await gc.protect_unknown(stage)
    await stage.close()
    assert await gc.collect(stage.generation) is False
    assert client.command("GET", stage.generation + ":doc:a") == "staged"
    with pytest.raises(RuntimeError):
        await gc.abort_stage(stage)
    await gc.abort_stage(stage, known_not_committed=True)
    assert await gc.collect(stage.generation) is True


async def test_expired_lease_cannot_publish_or_reappear(isolated_redis):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    stage = await gc.acquire_stage(generation())
    await gc.protect_unknown(stage)
    for item in stage.protected:
        client.command("ZADD", gc.lease_key(item), 0, stage.token)
    with pytest.raises(GenerationLeaseLost):
        await stage.check()
    with pytest.raises(GenerationLeaseLost):
        await gc.publish_cas(stage)
    assert await gc.collect(stage.generation) is False
    await gc.abort_stage(stage, known_not_committed=True)
    assert await gc.collect(stage.generation) is True
    with pytest.raises(Exception):
        await gc.acquire_stage(stage.generation)
    await stage.close()


async def test_source_revision_conflict_preserves_active_generation(isolated_redis):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    stage = await gc.acquire_stage(generation())
    await gc.protect_unknown(stage)
    client.command("INCR", "knowledge:revision")
    with pytest.raises(GenerationConflict):
        await gc.publish_cas(stage)
    assert client.command("HGET", gc.active_key, "active") is None
    await gc.abort_stage(stage, known_not_committed=True)
    await stage.close()
    assert await gc.collect(stage.generation) is True


async def test_live_reader_heartbeat_prevents_reclamation(isolated_redis):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=900)
    stage = await gc.acquire_stage(generation())
    await gc.protect_unknown(stage)
    await gc.publish_cas(stage)
    await gc.confirm_published(stage)
    await stage.close()
    reader = await gc.acquire_active()
    renewed = asyncio.Event()
    original = gc._lease_valid

    async def observe(lease, *, renew):
        result = await original(lease, renew=renew)
        if renew and lease is reader and result:
            renewed.set()
        return result

    gc._lease_valid = observe
    other = GenerationGC(client, "knowledge", lease_ms=30000)
    next_stage = await other.acquire_stage(generation())
    await other.protect_unknown(next_stage)
    await other.publish_cas(next_stage)
    await other.confirm_published(next_stage)
    await next_stage.close()
    await asyncio.wait_for(renewed.wait(), timeout=5)
    assert await other.collect(reader.generation) is False
    await reader.check()
    await reader.close()
    assert await other.collect(stage.generation) is True


async def test_interrupted_gc_can_resume_with_persistent_tombstone(isolated_redis):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    stage = await gc.acquire_stage(generation())
    client.command("SET", stage.generation + ":doc:a", "owned")
    await gc.abort_stage(stage)
    await stage.close()
    original = client.scan_iter
    def fail_scan(*args, **kwargs):
        raise RuntimeError("controlled scan failure")
        yield  # 此函数必须保持生成器接口
    client.scan_iter = fail_scan
    with pytest.raises(RuntimeError, match="controlled"):
        await gc.collect(stage.generation)
    client.scan_iter = original
    assert client.command("GET", gc.tombstone_key(stage.generation)) == "1"
    assert await gc.collect(stage.generation) is True
    assert client.command("GET", stage.generation + ":doc:a") is None
    assert client.command("GET", gc.tombstone_key(stage.generation)) == "1"


async def test_short_lease_cannot_shorten_long_lease_expiration(isolated_redis):
    client = isolated_redis
    long_gc = GenerationGC(client, "knowledge", lease_ms=30000)
    short_gc = GenerationGC(client, "knowledge", lease_ms=100)
    long_lease = await long_gc.acquire_active()
    key = long_gc.lease_key("knowledge")
    long_ttl = client.command("PTTL", key)
    short_lease = await short_gc.acquire_active()
    after = client.command("PTTL", key)
    assert after >= long_ttl - 2000
    await short_lease.close()
    await long_lease.check()
    await long_lease.close()


async def test_acquire_prunes_expired_abandoned_tokens_and_caps_stage_registry(isolated_redis):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    key = gc.lease_key("knowledge")
    client.command("ZADD", key, 0, "abandoned-token")
    lease = await gc.acquire_active()
    assert client.command("ZSCORE", key, "abandoned-token") is None
    assert client.command("PTTL", key) > 0
    await lease.close()
    entries = [generation() for _ in range(64)]
    client.command("SADD", gc.registry_key, *entries)
    with pytest.raises(Exception):
        await gc.acquire_stage(generation())
    assert client.command("SCARD", gc.registry_key) == 64


async def test_tombstone_metadata_is_bounded_and_expires_only_after_cleanup(isolated_redis):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    stage = await gc.acquire_stage(generation())
    client.command("SET", stage.generation + ":doc:a", "owned")
    await gc.abort_stage(stage)
    await stage.close()
    assert await gc.collect(stage.generation) is True
    tombstone = gc.tombstone_key(stage.generation)
    assert client.command("PTTL", tombstone) > 0
    assert client.command("SISMEMBER", gc._tombstones_key, stage.generation) == 1
    assert client.command("GET", stage.generation + ":doc:a") is None
    # 模拟完成安全等待后标记过期，旧租约仍不能再次发布
    client.command("DEL", tombstone)
    with pytest.raises(GenerationLeaseLost):
        await stage.check()
    with pytest.raises(GenerationLeaseLost):
        await gc.publish_cas(stage)


async def test_late_watched_writer_cannot_recreate_collected_generation(isolated_redis):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    stage = await gc.acquire_stage(generation())
    peer = subprocess.Popen(
        ["docker", "exec", "-i", client.container, "redis-cli", "--json"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    def command(line):
        peer.stdin.write(line + "\n")
        peer.stdin.flush()
        ready, _, _ = select.select([peer.stdout], [], [], 5)
        if not ready:
            raise RuntimeError("Redis 对端响应超时")
        return json.loads(peer.stdout.readline())
    try:
        assert command("WATCH " + " ".join(stage.watch_keys)) == "OK"
        await stage.check()
        # 模拟进程暂停至租约过期：GC先设置删除标记再清理
        for item in stage.protected:
            client.command("ZADD", gc.lease_key(item), 0, stage.token)
        assert await gc.collect(stage.generation) is True
        # 即使安全等待结束后删除标记到期，已有 WATCH 仍会拒绝迟到写入
        client.command("DEL", gc.tombstone_key(stage.generation))
        assert command("MULTI") == "OK"
        assert command("SET " + stage.generation + ":doc:late stale") == "QUEUED"
        assert command("EXEC") is None
        assert client.command("GET", stage.generation + ":doc:late") is None
        with pytest.raises(GenerationLeaseLost):
            await stage.check()
    finally:
        peer.terminate()
        peer.communicate(timeout=5)
        await stage.close()


async def test_committed_unknown_can_be_identified_after_later_publication(isolated_redis):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    first = await gc.acquire_stage(generation())
    await gc.protect_unknown(first)
    await gc.publish_cas(first)  # 模拟服务端已执行但响应丢失，未确认
    await first.close()
    second = await gc.acquire_stage(generation())
    await gc.protect_unknown(second)
    await gc.publish_cas(second)
    await gc.confirm_published(second)
    await second.close()
    entries = await gc.inspect_uncertain()
    assert entries == [{"generation": first.generation, "committed": True, "active": False, "live_leases": 0}]
    assert await gc.collect(first.generation) is False
    with pytest.raises(GenerationConflict):
        await gc.resolve_uncertain(first.generation, "abandon", "operator", "verified status")
    await gc.resolve_uncertain(first.generation, "acknowledge", "operator", "verified committed")
    assert await gc.collect(first.generation) is True
    assert (await gc.capacity_metrics())["resolution_audits"] == 1


async def test_unknown_uncommitted_requires_expired_lease_and_manual_audit(isolated_redis):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    stage = await gc.acquire_stage(generation())
    await gc.protect_unknown(stage)
    with pytest.raises(GenerationConflict):
        await gc.resolve_uncertain(stage.generation, "acknowledge", "operator", "verified status")
    with pytest.raises(GenerationLeaseLost):
        await gc.resolve_uncertain(stage.generation, "abandon", "operator", "verified status")
    await stage.close()
    assert await gc.collect(stage.generation) is False
    await gc.resolve_uncertain(stage.generation, "abandon", "operator", "verified not submitted")
    assert await gc.collect(stage.generation) is True
    assert (await gc.capacity_metrics())["resolution_audits"] == 1


async def test_sixty_four_unknown_versions_can_be_safely_resolved(isolated_redis):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    entries = [generation() for _ in range(64)]
    client.command("SADD", gc.registry_key, *entries)
    args = []
    for entry in entries:
        args.extend(["state:" + entry, "unknown"])
    client.command("HSET", gc.active_key, *args)
    assert (await gc.capacity_metrics())["unknown_uncommitted"] == 64
    with pytest.raises(Exception):
        await gc.acquire_stage(generation())
    await gc.resolve_uncertain(entries[0], "abandon", "operator", "verified no writer remains")
    assert await gc.collect(entries[0]) is True
    assert (await gc.capacity_metrics())["unknown_uncommitted"] == 63
    fresh = await gc.acquire_stage(generation())
    await gc.abort_stage(fresh)
    await fresh.close()
    assert await gc.collect(fresh.generation) is True


def test_admin_cli_inspects_and_resolves_with_os_identity(tmp_path):
    if os.getenv("CLOUDOPS_TEST_GENERATION_GC") != "true":
        pytest.skip("requires explicitly enabled isolated Redis test")
    import pwd
    import sys

    name = "cloudops-gc-cli-" + uuid.uuid4().hex
    subprocess.run(["docker", "run", "-d", "--rm", "--pull=never", "--name", name, "--label", "cloudops.gc.run=" + name,
                    "--mount", "type=tmpfs,destination=/data", "-p", "127.0.0.1::6379", "redis:7.4.11-alpine",
                    "redis-server", "--save", "", "--appendonly", "no"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
    try:
        client = RedisCLI(name)
        assert client.command("PING") == "PONG"
        address = subprocess.check_output(["docker", "port", name, "6379/tcp"], text=True, timeout=10).strip()
        assert address.startswith("127.0.0.1:")
        entry = generation()
        client.command("SADD", "knowledge:gc:generations", entry)
        client.command("HSET", "knowledge:active_generation", "state:" + entry, "unknown")
        env = {**os.environ, "CLOUDOPS_RAG_ADMIN_REDIS_URL": "redis://" + address + "/0"}
        prefix = [sys.executable, "-m", "app.core.vector.generation_gc", "--collection", "knowledge"]
        default_read = subprocess.run(prefix, env=env, text=True, capture_output=True, timeout=10, check=True)
        assert json.loads(default_read.stdout)["uncertain"][0]["generation"] == entry
        read = subprocess.run(prefix + ["inspect"], env=env, text=True, capture_output=True, timeout=10, check=True)
        assert json.loads(read.stdout)["uncertain"][0]["generation"] == entry
        reason = "verified-private-audit-reason"
        payload = json.dumps({"generation": entry, "action": "abandon", "reason": reason, "operator": "forged"})
        resolved = subprocess.run(prefix + ["resolve"], input=payload, env=env, text=True, capture_output=True, timeout=10, check=True)
        assert json.loads(resolved.stdout)["resolved"] is True
        assert reason not in resolved.stdout + resolved.stderr
        manifest = client.command("HGETALL", "knowledge:active_generation")
        values = manifest if isinstance(manifest, dict) else dict(zip(manifest[::2], manifest[1::2]))
        audits = [json.loads(value) for key, value in values.items() if key.startswith("resolution:")]
        assert len(audits) == 1
        assert audits[0]["operator"] == f"uid:{os.getuid()}:{pwd.getpwuid(os.getuid()).pw_name}"
        assert audits[0]["reason"] == reason
        export_path = str(tmp_path / "cli-audit.json")
        exported = subprocess.run(prefix + ["export"], input=json.dumps({"path": export_path}), env=env, text=True, capture_output=True, timeout=10, check=True)
        exported_data = json.loads(exported.stdout)
        assert exported_data["count"] == 1
        assert reason not in exported.stdout + exported.stderr
        archived = subprocess.run(prefix + ["archive"], input=json.dumps({"path": export_path, "sha256": exported_data["sha256"]}), env=env, text=True, capture_output=True, timeout=10, check=True)
        assert json.loads(archived.stdout)["removed"] == 1
        assert reason not in archived.stdout + archived.stderr
        assert client.command("HLEN", "knowledge:active_generation") == 1

        # 子进程使用真实Redis，注入EVAL已提交而客户端回包丢失
        uncertain = generation()
        client.command("SADD", "knowledge:gc:generations", uncertain)
        client.command("HSET", "knowledge:active_generation", "state:" + uncertain, "unknown")
        (tmp_path / "sitecustomize.py").write_text(
            "import redis\n"
            "old=redis.Redis.eval\n"
            "def fault(self, script, *args):\n"
            " result=old(self, script, *args)\n"
            " if 'local generation=ARGV[1]; local action=ARGV[2]' in script:\n"
            "  raise redis.exceptions.ConnectionError('private-upstream-canary')\n"
            " return result\n"
            "redis.Redis.eval=fault\n"
        )
        fault_env = {**env, "PYTHONPATH": str(tmp_path) + os.pathsep + os.getcwd()}
        uncertain_payload = json.dumps({"generation": uncertain, "action": "abandon", "reason": "verified private reason"})
        failed = subprocess.run(prefix + ["resolve"], input=uncertain_payload, env=fault_env, text=True, capture_output=True, timeout=10)
        assert failed.returncode == 1
        assert "结果未知" in failed.stderr and "勿盲目重试" in failed.stderr
        assert "private-upstream-canary" not in failed.stderr
        assert "verified private reason" not in failed.stderr
        inspected = subprocess.run(prefix + ["--generation", uncertain, "inspect"], env=env, text=True, capture_output=True, timeout=10, check=True)
        details = json.loads(inspected.stdout)["generation"]
        assert details["state"] == "aborted"
        assert details["resolutions"][0]["action"] == "abandon"
        assert "verified private reason" not in inspected.stdout
        invalid_reply = generation()
        client.command("SADD", "knowledge:gc:generations", invalid_reply)
        client.command("HSET", "knowledge:active_generation", "state:" + invalid_reply, "unknown")
        site = tmp_path / "sitecustomize.py"
        site.write_text(site.read_text().replace("redis.exceptions.ConnectionError", "redis.exceptions.InvalidResponse"))
        for cached in (tmp_path / "__pycache__").glob("sitecustomize*.pyc"):
            cached.unlink()
        fault_env["PYTHONDONTWRITEBYTECODE"] = "1"
        payload = json.dumps({"generation": invalid_reply, "action": "abandon", "reason": "verified private reason"})
        broken = subprocess.run(prefix + ["resolve"], input=payload, env=fault_env, text=True, capture_output=True, timeout=10)
        assert broken.returncode == 1
        assert "结果未知" in broken.stderr and "勿盲目重试" in broken.stderr
        assert "private-upstream-canary" not in broken.stderr
        checked = subprocess.run(prefix + ["--generation", invalid_reply, "inspect"], env=env, text=True, capture_output=True, timeout=10, check=True)
        assert json.loads(checked.stdout)["generation"]["state"] == "aborted"
    finally:
        label = subprocess.check_output(["docker", "inspect", "--format", '{{index .Config.Labels "cloudops.gc.run"}}', name], text=True, timeout=10).strip()
        if label != name:
            raise RuntimeError("拒绝清理非本次 Redis")
        subprocess.run(["docker", "rm", "-f", name], check=True, stdout=subprocess.DEVNULL, timeout=10)


async def test_audit_export_archive_keeps_concurrent_records_and_requires_confirmation(isolated_redis, tmp_path):
    import stat

    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    async def resolve_one():
        stage = await gc.acquire_stage(generation())
        await gc.protect_unknown(stage)
        await stage.close()
        await gc.resolve_uncertain(stage.generation, "abandon", "uid:7:test", "核对后未提交")
        return stage.generation

    await resolve_one()
    await resolve_one()
    destination = tmp_path / "audit.json"
    exported = await gc.export_audit(destination)
    assert exported["count"] == 2
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        await gc.export_audit(destination)
    await resolve_one()  # 导出后的新审计不得被删除
    with pytest.raises(ValueError):
        await gc.archive_audit(destination, "0" * 64)
    assert (await gc.capacity_metrics())["resolution_audits"] == 3
    assert (await gc.archive_audit(destination, exported["sha256"]))["removed"] == 2
    assert (await gc.capacity_metrics())["resolution_audits"] == 1
    receipts = destination.parent / ".cloudops-audit-archive" / "receipts.jsonl"
    entries = [json.loads(line) for line in receipts.read_text().splitlines()]
    assert [entry["phase"] for entry in entries] == ["begin", "completed"]
    assert all(entry["sha256"] == exported["sha256"] and entry["operator"].startswith("uid:") for entry in entries)
    assert stat.S_IMODE(receipts.stat().st_mode) == 0o600
    assert (await gc.archive_audit(destination, exported["sha256"]))["removed"] == 0


async def test_audit_archive_rejects_changed_record_without_deleting_batch(isolated_redis, tmp_path):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    for _ in range(2):
        stage = await gc.acquire_stage(generation())
        await gc.protect_unknown(stage)
        await stage.close()
        await gc.resolve_uncertain(stage.generation, "abandon", "uid:7:test", "verified no writer")
    destination = tmp_path / "audit.json"
    exported = await gc.export_audit(destination)
    snapshot = json.loads(destination.read_text())
    client.command("HSET", gc.active_key, snapshot["entries"][0]["field"], '{"tampered":true}')
    with pytest.raises(GenerationConflict):
        await gc.archive_audit(destination, exported["sha256"])
    assert (await gc.capacity_metrics())["resolution_audits"] == 2


async def test_audit_archive_can_resume_after_lost_redis_response(isolated_redis, tmp_path):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    fields = []
    for i in range(501):
        fields.extend(["resolution:" + format(i, "032x"), json.dumps({"record": i})])
    client.command("HSET", gc.active_key, *fields)
    destination = tmp_path / "audit.json"
    exported = await gc.export_audit(destination)
    original_eval = client.eval
    lost = False
    def lose_response(script, numkeys, *args):
        nonlocal lost
        result = original_eval(script, numkeys, *args)
        if not lost and script == __import__("app.core.vector.generation_gc", fromlist=["_ARCHIVE"])._ARCHIVE:
            lost = True
            raise ConnectionError("simulated lost reply")
        return result
    client.eval = lose_response
    with pytest.raises(ConnectionError):
        await gc.archive_audit(destination, exported["sha256"])
    client.eval = original_eval
    assert (await gc.capacity_metrics())["resolution_audits"] == 1
    assert (await gc.archive_audit(destination, exported["sha256"]))["removed"] == 1
    assert (await gc.capacity_metrics())["resolution_audits"] == 0


async def test_audit_changed_during_archive_keeps_durable_copy_for_resume(isolated_redis, tmp_path):
    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    fields = []
    for i in range(501):
        fields.extend(["resolution:" + format(i, "032x"), json.dumps({"record": i})])
    client.command("HSET", gc.active_key, *fields)
    source = tmp_path / "audit.json"
    exported = await gc.export_audit(source)
    original = source.read_bytes()
    original_eval = client.eval
    changed = False
    def mutate_after_commit(script, numkeys, *args):
        nonlocal changed
        result = original_eval(script, numkeys, *args)
        if not changed and script == __import__("app.core.vector.generation_gc", fromlist=["_ARCHIVE"])._ARCHIVE:
            changed = True
            source.write_text("tampered")
        return result
    client.eval = mutate_after_commit
    with pytest.raises(GenerationConflict):
        await gc.archive_audit(source, exported["sha256"])
    client.eval = original_eval
    archive = tmp_path / ".cloudops-audit-archive" / (exported["sha256"] + ".json")
    assert archive.read_bytes() == original
    assert archive.stat().st_mode & 0o777 == 0o600
    assert (await gc.capacity_metrics())["resolution_audits"] == 1
    source.write_bytes(original)
    result = await gc.archive_audit(source, exported["sha256"])
    assert result["removed"] == 1
    assert (await gc.capacity_metrics())["resolution_audits"] == 0


async def test_audit_archive_rejects_insecure_or_symlink_source(isolated_redis, tmp_path):
    import os

    gc = GenerationGC(isolated_redis, "knowledge", lease_ms=30000)
    stage = await gc.acquire_stage(generation())
    await gc.protect_unknown(stage)
    await stage.close()
    await gc.resolve_uncertain(stage.generation, "abandon", "uid:7:test", "verified no writer")
    source = tmp_path / "audit.json"
    exported = await gc.export_audit(source)
    os.chmod(source, 0o644)
    with pytest.raises(ValueError):
        await gc.archive_audit(source, exported["sha256"])
    os.chmod(source, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(source)
    with pytest.raises(OSError):
        await gc.archive_audit(link, exported["sha256"])
    assert (await gc.capacity_metrics())["resolution_audits"] == 1


def test_local_operator_falls_back_to_real_uid_when_no_passwd_entry(monkeypatch):
    import pwd

    from app.core.vector.generation_gc import _local_operator

    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: (_ for _ in ()).throw(KeyError("no entry")))
    assert _local_operator() == f"uid:{os.getuid()}"


@pytest.mark.parametrize("failure_name", ["ConnectionError", "InvalidResponse"])
async def test_resolve_lost_reply_is_uncertain_and_inspectable(isolated_redis, failure_name):
    from redis import exceptions as redis_errors

    from app.core.vector import generation_gc as module

    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    stage = await gc.acquire_stage(generation())
    await gc.protect_unknown(stage)
    await stage.close()
    original_eval = client.eval
    def lost_reply(script, numkeys, *args):
        result = original_eval(script, numkeys, *args)
        if script == module._RESOLVE:
            raise getattr(redis_errors, failure_name)("sensitive simulated transport")
        return result
    client.eval = lost_reply
    with pytest.raises(module.GenerationResolutionOutcomeUnknown):
        await gc.resolve_uncertain(stage.generation, "abandon", "uid:7:test", "verified no writer")
    client.eval = original_eval
    details = await gc.inspect_generation(stage.generation)
    assert details["state"] == "aborted"
    assert details["resolutions"][0]["action"] == "abandon"
    assert "verified no writer" not in str(details)
    with pytest.raises(GenerationConflict):
        await gc.resolve_uncertain(stage.generation, "abandon", "uid:7:test", "verified no writer")
    assert (await gc.capacity_metrics())["resolution_audits"] == 1


async def test_cancelled_acquire_prunes_abandoned_token_without_harming_live_reader(isolated_redis):
    import threading

    from app.core.vector import generation_gc as module

    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    live = await gc.acquire_active()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_eval = client.eval
    def paused_eval(script, numkeys, *args):
        result = original_eval(script, numkeys, *args)
        if script == module._ACQUIRE_ACTIVE:
            entered.set()
            release.wait(5)
            finished.set()
        return result
    client.eval = paused_eval
    pending = asyncio.create_task(gc.acquire_active())
    assert await asyncio.to_thread(entered.wait, 5)
    pending.cancel()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    client.eval = original_eval
    key = gc.lease_key("knowledge")
    tokens = client.command("ZRANGE", key, 0, -1)
    abandoned = next(token for token in tokens if token != live.token)
    client.command("ZADD", key, 0, abandoned)
    replacement = await gc.acquire_active()
    assert client.command("ZSCORE", key, abandoned) is None
    await live.check()
    await replacement.close()
    await live.close()


async def test_repeated_cancellation_waits_for_all_lease_releases(isolated_redis):
    import threading

    client = isolated_redis
    gc = GenerationGC(client, "knowledge", lease_ms=30000)
    stage = await gc.acquire_stage(generation())
    entered, release = threading.Event(), threading.Event()
    original_zrem = client.zrem
    def paused_zrem(key, token):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("local test")
        return original_zrem(key, token)
    client.zrem = paused_zrem
    pending = asyncio.create_task(stage.close())
    assert await asyncio.to_thread(entered.wait, 5)
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    client.zrem = original_zrem
    for item in stage.protected:
        assert client.command("ZSCORE", gc.lease_key(item), stage.token) is None
    await gc.abort_stage(stage)
    assert await gc.collect(stage.generation) is True


async def test_invalid_resolution_is_rejected_before_dispatch():
    from unittest.mock import Mock

    client = type("Client", (), {"eval": Mock()})()
    gc = GenerationGC(client, "knowledge")
    with pytest.raises(ValueError):
        await gc.resolve_uncertain("not-a-generation", "abandon", "operator", "valid reason")
    with pytest.raises(ValueError):
        await gc.resolve_uncertain(generation(), "invalid-action", "operator", "valid reason")
    with pytest.raises(ValueError):
        await gc.resolve_uncertain(generation(), "abandon", "operator", "no")
    client.eval.assert_not_called()
