"""显式自建Redis验证发布准入与fencing；默认不连接任何服务。"""

import argparse
import asyncio
import json
import os
import subprocess
import time
import uuid

_DOCKER_ENDPOINT_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")


def _docker_output(arguments, environment):
    result = subprocess.run(["docker", *arguments], env=environment, capture_output=True, text=True, timeout=30, check=True)
    return result.stdout.strip()


def _local_docker():
    environment = dict(os.environ)
    if environment.get("DOCKER_HOST") or environment.get("DOCKER_CONTEXT"):
        raise RuntimeError("fixture refuses Docker endpoint environment overrides")
    for key in _DOCKER_ENDPOINT_ENV:
        environment.pop(key, None)
    endpoint = _docker_output(("context", "inspect", "--format", "{{.Endpoints.docker.Host}}"), environment)
    if not endpoint.startswith("unix:///") or endpoint == "unix:///":
        raise RuntimeError("fixture requires a local Unix-socket Docker context")

    def docker(*arguments):
        return _docker_output(("--host", endpoint, *arguments), environment)

    return docker


async def exercise(port):
    # 导入业务前禁用本机配置/真实模型凭据，只连接本脚本的随机loopback端口。
    os.environ.update({
        "ENV": "test", "PYTHON_DOTENV_DISABLED": "1", "LLM_API_KEY": "",
        "AIOPS_API_KEY": "offline-fixture-key-with-32-characters", "K8S_CONFIG_PATH": "/nonexistent", "KUBECONFIG": "/nonexistent",
    })
    import threading

    from langchain_core.documents import Document
    import numpy as np
    import redis

    from app.core.vector.generation_gc import GenerationConflict
    from app.core.vector.redis_vector_store import EnhancedRedisVectorStore
    from app.core.vector.stage_admission import StageAdmissionError

    class Embeddings:
        def embed_documents(self, documents):
            return [[1.0, 0.0] for _ in documents]

        def embed_query(self, _query):
            return [1.0, 0.0]

    name = "admission-" + uuid.uuid4().hex
    store = EnhancedRedisVectorStore({"host": "127.0.0.1", "port": port, "db": 0, "decode_responses": False}, name, Embeddings(), vector_dim=2)
    stages = []
    original_create = store.create_refresh_stage

    async def create():
        stage = await original_create()
        stages.append(stage)
        return stage

    store.create_refresh_stage = create
    pin = None
    checks = {}
    try:
        await store.add_documents([Document(page_content="protected original")])
        original_active = await store._read_active_generation()
        pin = await store._generation_gc.acquire_active()
        store.client.set(f"{name}:legacy-preserved", "owned historical marker")
        stage = await store.create_refresh_stage()
        # 真实MD处理器650块×2：输入不到1MiB、每次仍≤1000，但倒排键总量超限。
        source_bytes = 0
        for offset in (0, 650):
            content = "\n\n".join(
                "# H" + str(index) + "\n\n" + " ".join(
                    np.base_repr(index, base=36).lower().zfill(3) + np.base_repr(word, base=36).lower().zfill(2)
                    for word in range(80)
                ) + "\n\n- item"
                for index in range(offset, offset + 650)
            )
            source_bytes += len(content.encode("utf-8"))
            # 仅替换模型边界，不绕过真实MD分块、事务或发布准入。
            async def embedding(batch):
                return [np.array([1, 0], dtype=np.float32) for _ in batch]
            stage._batch_embed = embedding
            ids = await stage.add_md_documents([content], [{"source": f"fixture-{offset}.md"}])
            if len(ids) != 650:
                raise AssertionError("MD fixture did not produce the reviewed 650 blocks")
        physical_count = sum(1 for _ in store.client.scan_iter(match=f"{stage.collection_name}:*", count=1000))
        if physical_count <= 100_000:
            raise AssertionError("oversized fixture did not exceed admission budget")
        try:
            await store.publish_refresh_stage(stage)
        except StageAdmissionError:
            pass
        else:
            raise AssertionError("oversized stage was published")
        if stage._commit_started or await store._read_active_generation() != original_active:
            raise AssertionError("admission rejection changed active or unknown state")
        await store.discard_refresh_stage(stage)
        if any(store.client.scan_iter(match=f"{stage.collection_name}:*", count=500)):
            raise AssertionError("streaming GC left oversized rejected stage keys")
        await pin.check()
        if not any(store.client.scan_iter(match=f"{original_active}:doc:*", count=500)):
            raise AssertionError("rejected stage GC deleted protected reader data")
        await store.add_documents([Document(page_content="small update after GC")])
        if await store._read_active_generation() == original_active:
            raise AssertionError("small update after rejected-stage GC did not publish")
        if await store._generation_gc.collect(original_active):
            raise AssertionError("active reader generation was collected")
        await pin.close()
        pin = None
        if not await store._generation_gc.collect(original_active):
            raise AssertionError("retired reader generation was not collectable after release")
        if await store._generation_gc.collect(name) or store.client.get(f"{name}:legacy-preserved") is None:
            raise AssertionError("legacy fixed prefix was collected")
        checks["oversized_stage_physical_keys"] = physical_count
        checks["oversized_fixture_source_bytes"] = source_bytes
        checks["oversized_gc_then_small_update"] = True

        # 合法公开MD元数据必须通过受限校验，不能把pickle字节当UTF8。
        stage = await store.create_refresh_stage()
        await stage.add_md_documents(["# Safe heading\n\nA small valid document.\n\n- item"], [{"source": "owned-fixture.md"}])
        await store.publish_refresh_stage(stage)
        await store.discard_refresh_stage(stage)
        checks["public_markdown_metadata"] = True

        # 完整检查后、freeze前发生写入，checked revision必须失效。
        stage = await store.create_refresh_stage()
        await stage.add_documents([Document(page_content="before validation")])
        revision = await store._inspect_refresh_stage(stage)
        await stage.add_documents([Document(page_content="changed after validation")])
        try:
            await store._generation_gc.protect_unknown(stage._stage_lease, target_revision=revision)
        except GenerationConflict:
            pass
        else:
            raise AssertionError("freeze accepted a changed validated revision")
        await store.discard_refresh_stage(stage)
        checks["checked_revision_freeze_conflict"] = True

        # 写事务已WATCH并通过building检查，但尚未EXEC时freeze使其失效。
        stage = await store.create_refresh_stage()
        await stage.add_documents([Document(page_content="before freeze")])
        entered, release = threading.Event(), threading.Event()
        original_pipeline = stage.client.pipeline

        def paused_pipeline(*args, **kwargs):
            pipe = original_pipeline(*args, **kwargs)
            execute = pipe.execute

            def delayed(*args, **kwargs):
                entered.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("owned transaction barrier timeout")
                return execute(*args, **kwargs)

            pipe.execute = delayed
            return pipe

        stage.client.pipeline = paused_pipeline
        pending = asyncio.create_task(stage.add_documents([Document(page_content="late watched writer")]))
        try:
            if not await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), 5):
                raise AssertionError("late transaction did not reach EXEC barrier")
            revision = await store._inspect_refresh_stage(stage)
            await store._generation_gc.protect_unknown(stage._stage_lease, target_revision=revision)
            release.set()
            try:
                await pending
            except redis.WatchError:
                pass
            else:
                raise AssertionError("late watched write survived sealing")
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            stage.client.pipeline = original_pipeline
        late_id = stage._generate_doc_id("late watched writer")
        if store.client.exists(f"{stage.collection_name}:doc:{late_id}"):
            raise AssertionError("late writer generated sealed data")
        await store._generation_gc.publish_cas(stage._stage_lease)
        await store._generation_gc.confirm_published(stage._stage_lease)
        stage._commit_started = True
        try:
            await stage.add_documents([Document(page_content="write after publish")])
        except GenerationConflict:
            pass
        else:
            raise AssertionError("published stage accepted another write")
        await store.discard_refresh_stage(stage)
        checks["watched_and_post_publish_writes_rejected"] = True

        # 不参与协议的外部写入revision，也不能绕过publish CAS的sealed证据。
        stage = await store.create_refresh_stage()
        await stage.add_documents([Document(page_content="CAS guard")])
        revision = await store._inspect_refresh_stage(stage)
        await store._generation_gc.protect_unknown(stage._stage_lease, target_revision=revision)
        before = await store._read_active_generation()
        store.client.incr(f"{stage.collection_name}:revision")
        try:
            await store._generation_gc.publish_cas(stage._stage_lease)
        except GenerationConflict:
            pass
        else:
            raise AssertionError("publish CAS accepted changed sealed revision")
        if await store._read_active_generation() != before:
            raise AssertionError("conflicting CAS changed active generation")
        await store.discard_refresh_stage(stage)
        checks["publish_checks_sealed_revision"] = True

        for corrupt in ("unknown-mode", "missing-proof-and-revision"):
            stage = await store.create_refresh_stage()
            await stage.add_documents([Document(page_content="sealed proof completeness")])
            revision = await store._inspect_refresh_stage(stage)
            await store._generation_gc.protect_unknown(stage._stage_lease, target_revision=revision)
            if corrupt == "unknown-mode":
                store.client.hset(store._generation_gc.active_key, "sealed_present:" + stage.collection_name, "invalid")
            else:
                store.client.hdel(store._generation_gc.active_key, "sealed_revision:" + stage.collection_name)
                store.client.delete(f"{stage.collection_name}:revision")
            try:
                await store._generation_gc.publish_cas(stage._stage_lease)
            except GenerationConflict:
                pass
            else:
                raise AssertionError("publish accepted incomplete or unknown sealed proof")
            await store.discard_refresh_stage(stage)
        checks["incomplete_sealed_proof_rejected"] = True

        # 克隆合同保留“旧idx不复制”，但直接向新stage混入idx必须拒绝。
        source = await store._read_active_generation()
        store.client.set(f"{source}:idx:legacy", "old local position")
        reader = await store._generation_gc.acquire_active()
        async with reader:
            token = store._generation_scope.set((reader.generation, reader))
            try:
                stage = await store._clone_active_stage()
            finally:
                store._generation_scope.reset(token)
        if any(store.client.scan_iter(match=f"{stage.collection_name}:idx:*", count=100)):
            raise AssertionError("clone copied unsupported legacy index keys")
        await store.publish_refresh_stage(stage)
        await store.discard_refresh_stage(stage)
        stage = await store.create_refresh_stage()
        await stage.add_documents([Document(page_content="no legacy index in new stage")])
        store.client.set(f"{stage.collection_name}:idx:0", "legacy position")
        before = await store._read_active_generation()
        try:
            await store.publish_refresh_stage(stage)
        except StageAdmissionError:
            pass
        else:
            raise AssertionError("new stage accepted unsupported legacy index keys")
        if await store._read_active_generation() != before:
            raise AssertionError("invalid index changed active generation")
        await store.discard_refresh_stage(stage)
        checks["clone_skips_legacy_idx_new_stage_rejects_idx"] = True

        # 空目录刷新合法：target revision缺失也有完整sealed证明，之后仍可小写入。
        stage = await store.create_refresh_stage()
        await store.publish_refresh_stage(stage)
        await store.discard_refresh_stage(stage)
        if await store.get_document_count() != 0:
            raise AssertionError("valid empty stage did not publish an empty corpus")
        await store.add_documents([Document(page_content="small write after empty publication")])
        checks["empty_stage_then_small_write"] = True

        # 无提交证据的unknown仍不能被自动GC；只在显式审核放弃后回收。
        stage = await store.create_refresh_stage()
        await stage.add_documents([Document(page_content="unknown must survive")])
        revision = await store._inspect_refresh_stage(stage)
        await store._generation_gc.protect_unknown(stage._stage_lease, target_revision=revision)
        await stage._stage_lease.close()
        if await store._generation_gc.collect(stage.collection_name):
            raise AssertionError("unknown stage was collected automatically")
        await store._generation_gc.resolve_uncertain(stage.collection_name, "abandon", "owned-fixture", "verified fixture has never committed")
        if not await store._generation_gc.collect(stage.collection_name):
            raise AssertionError("explicitly abandoned stage could not be collected")
        checks["unknown_reader_legacy_protections"] = True
        return checks
    finally:
        if pin:
            await pin.close()
        for stage in stages:
            if not getattr(stage, "_discarded", False):
                try:
                    await stage._stage_lease.close()
                finally:
                    await stage.close()
            if stage.md_processor and stage.md_processor._executor:
                stage.md_processor._executor.shutdown(wait=True)
        await store.close()
        if store.md_processor and store.md_processor._executor:
            store.md_processor._executor.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="create and destroy an owned local Redis fixture")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run: no Redis connection. Use --execute for a disposable local fixture.")
        return 0
    docker = _local_docker()
    token = uuid.uuid4().hex
    container = docker("run", "-d", "--rm", "--pull=never", "--label", "cloudops.admission.fixture=" + token,
                       "-p", "127.0.0.1::6379", "redis:7.4.11-alpine", "redis-server", "--save", "", "--appendonly", "no")
    try:
        if docker("inspect", "--format", '{{index .Config.Labels "cloudops.admission.fixture"}}', container) != token:
            raise RuntimeError("owned fixture label differs")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if docker("exec", container, "redis-cli", "PING") == "PONG":
                    break
            except subprocess.CalledProcessError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("owned Redis readiness timeout")
        address = docker("port", container, "6379/tcp")
        if not address.startswith("127.0.0.1:"):
            raise RuntimeError("owned Redis must bind loopback")
        result = asyncio.run(exercise(int(address.rsplit(":", 1)[1])))
        print(json.dumps({"passed": True, **result}, sort_keys=True))
    finally:
        if docker("inspect", "--format", '{{index .Config.Labels "cloudops.admission.fixture"}}', container) != token:
            raise RuntimeError("refusing cleanup with mismatched ownership")
        docker("rm", "-fv", container)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
