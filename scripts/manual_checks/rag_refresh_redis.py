import argparse
import asyncio
import json
import os
from pathlib import Path
import pickle
import select
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

parser = argparse.ArgumentParser(description="仅运行隔离Redis知识库刷新回归")
parser.add_argument("--execute", action="store_true", help="创建并清理带唯一标签的临时Redis容器")
args = parser.parse_args()
if not args.execute:
    parser.error("必须显式指定 --execute")
# 在业务模块导入前强制隔离配置，避免读取本机部署环境
os.environ.update(
    {
        "ENV": "test",
        "PYTHON_DOTENV_DISABLED": "1",
        "K8S_CONFIG_PATH": "/nonexistent",
        "KUBECONFIG": "/nonexistent",
        "LLM_API_KEY": "",
        "AIOPS_API_KEY": "offline-service-key-with-32-characters",
    }
)
from langchain_core.documents import Document
import numpy as np
import redis

from app.core.agents.enterprise_assistant import RAGAssistant
from app.core.vector.generation_gc import _PUBLISH
from app.core.vector.redis_vector_store import EnhancedRedisVectorStore, RefreshPublishedCacheError, RefreshPublishOutcomeUnknown


async def main():
    port = int(os.environ['AUDIT_REDIS_PORT'])

    class Embedding:
        def embed_documents(self, texts):
            return [[1.0, 0.0] for _ in texts]

        def embed_query(self, text):
            return [1.0, 0.0]

    store = EnhancedRedisVectorStore({'host': '127.0.0.1', 'port': port, 'db': 0}, 'acceptance', Embedding(), 2, 'FLAT')

    def document_keys(collection):
        return set(store.client.scan_iter(match=f"{collection}:doc:*"))

    stage = None
    try:
        # 兼容历史固定前缀；发布前后旧数据必须保持可读
        # 注入旧版本固定前缀数据，验证无需离线重写即可兼容
        legacy_id = store._generate_doc_id("old")
        store.client.hset(f"acceptance:doc:{legacy_id}", mapping={"content": "old", "metadata": pickle.dumps({})})
        store.client.set(f"acceptance:vec:{legacy_id}", np.array([1, 0], dtype=np.float32).tobytes())
        store.client.sadd("acceptance:term:old", legacy_id)
        original = document_keys("acceptance")
        if len(original) != 1:
            raise AssertionError("legacy document missing")

        stage = await store.create_refresh_stage()
        await stage.add_documents([Document(page_content="staged")])
        await store.add_documents([Document(page_content="concurrent")])
        try:
            await store.publish_refresh_stage(stage)
        except RuntimeError:
            pass
        else:
            raise AssertionError("concurrent change was not rejected")
        if not original.issubset(document_keys("acceptance")):
            raise AssertionError("conflict corrupted legacy data")
        await store.discard_refresh_stage(stage)

        stage = await store.create_refresh_stage()
        await stage.add_documents([Document(page_content="new")])
        await store.publish_refresh_stage(stage)
        active = await store._read_active_generation()
        if active != stage.collection_name or len(document_keys(active)) != 1:
            raise AssertionError("active pointer did not switch")
        if not original.issubset(document_keys("acceptance")):
            raise AssertionError("publication removed the previous version")
        await store.discard_refresh_stage(stage)
        if len(document_keys(active)) != 1:
            raise AssertionError("cleanup removed the published version")
        restarted = EnhancedRedisVectorStore({'host': '127.0.0.1', 'port': port, 'db': 0}, 'acceptance', Embedding(), 2, 'FLAT')
        try:
            if await restarted.get_document_count() != 1 or not await restarted.similarity_search('restart', 1):
                raise AssertionError("fresh instance did not read the active generation")
        finally:
            await restarted.close()

        # 执行成功但响应丢失：保留可能已激活的stage与上一版本
        previous_active = active
        previous_documents = document_keys(previous_active)
        stage = await store.create_refresh_stage()
        await stage.add_documents([Document(page_content="lost-reply")])
        original_eval = store.client.eval

        def lost_reply(script, *args, **kwargs):
            result = original_eval(script, *args, **kwargs)
            if script == _PUBLISH:
                raise redis.ConnectionError("controlled lost response")
            return result

        store.client.eval = lost_reply
        try:
            try:
                await store.publish_refresh_stage(stage)
            except RefreshPublishOutcomeUnknown:
                pass
            else:
                raise AssertionError("lost response was not reported as unknown")
        finally:
            store.client.eval = original_eval
        active = await store._read_active_generation()
        unknown_generation = active
        if active != stage.collection_name:
            raise AssertionError("injected transaction did not reach Redis")
        await store.discard_refresh_stage(stage)
        if len(document_keys(active)) != 1 or document_keys(previous_active) != previous_documents:
            raise AssertionError("uncertain publication deleted a retained version")

        # 上传在旧版本开始、切换后提交：整批拒绝，调用方可重试
        stage = await store.create_refresh_stage()
        await stage.add_documents([Document(page_content="after-refresh")])
        entered, release = asyncio.Event(), asyncio.Event()
        original_clone = store._clone_active_stage

        async def paused_clone():
            cloned = await original_clone()
            entered.set()
            await release.wait()
            return cloned

        store._clone_active_stage = paused_clone
        docs = [Document(page_content="upload-" + str(i)) for i in range(101)]
        pending = asyncio.create_task(store.add_documents(docs))
        await entered.wait()
        await store.publish_refresh_stage(stage)
        release.set()
        try:
            await pending
        except RuntimeError:
            pass
        else:
            raise AssertionError("stale-generation write was not rejected")
        store._clone_active_stage = original_clone
        active = await store._read_active_generation()
        if len(document_keys(active)) != 1:
            raise AssertionError("stale write partially changed the active version")
        added = await store.add_documents(docs)
        active = await store._read_active_generation()
        if len(added) != 101 or len(document_keys(active)) != 102:
            raise AssertionError("retry did not atomically add all chunks")
        found = await store.similarity_search("query", 2)
        if not found:
            raise AssertionError("active generation vectors were not retrieved")
        await store.discard_refresh_stage(stage)

        # 快照扫描中发生租约续期不应使自身WATCH冲突
        heartbeat_stage = await store.create_refresh_stage()
        original_keys = store._collection_keys

        def renew_during_scan(collection, **kwargs):
            keys = original_keys(collection, **kwargs)
            lease = heartbeat_stage._stage_lease
            current_time = store.client.time()
            expires = current_time[0] * 1000 + current_time[1] // 1000 + 30_000
            store.client.zadd(lease.owner.lease_key(lease.generation), {lease.token: expires})
            return keys

        store._collection_keys = renew_during_scan
        try:
            await asyncio.to_thread(store._copy_active_snapshot, heartbeat_stage)
        finally:
            store._collection_keys = original_keys
            await store.discard_refresh_stage(heartbeat_stage)

        # 单独进程持有长读租约；超过64次更新后保留在途版本，进程崩溃后可回收
        child_code = """
import asyncio, sys
import redis
from app.core.vector.generation_gc import GenerationGC
async def run():
    gc = GenerationGC(redis.Redis(host='127.0.0.1', port=int(sys.argv[1])), 'acceptance', lease_ms=3000)
    lease = await gc.acquire_active()
    print(lease.generation, flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    await lease.check()
    await lease.close()
asyncio.run(run())
"""
        child = subprocess.Popen([sys.executable, '-c', child_code, str(port)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([child.stdout], [], [], 10)
            if not ready:
                raise AssertionError("reader subprocess did not acquire lease")
            held_generation = child.stdout.readline().strip()
            if held_generation != await store._read_active_generation():
                raise AssertionError("reader acquired unexpected version")
            for index in range(70):
                await store.add_documents([Document(page_content=f"gc-update-{index}")])
            if not document_keys(held_generation):
                raise AssertionError("long-running reader generation was removed")
            if not document_keys(unknown_generation):
                raise AssertionError("unknown publication generation was removed")
            if await store._generation_gc.collect(held_generation):
                raise AssertionError("live cross-process reader was not protected")
            # 仅终止本测试创建的子进程，验证租约过期后的崩溃恢复
            child.kill()
            await asyncio.to_thread(child.wait, 5)
            await asyncio.sleep(3.5)
            if not await store._generation_gc.collect(held_generation):
                raise AssertionError("crashed reader generation was not collected after expiry")
            if document_keys(held_generation):
                raise AssertionError("retired generation data remains")
        finally:
            if child.poll() is None:
                child.kill()
                await asyncio.to_thread(child.wait, 5)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream:
                    stream.close()

        # stage写事务在外层检查后停顿至租约过期，不得在GC后复活版本键
        store._generation_gc.lease_ms = 150
        stale = await store.create_refresh_stage()
        await stale.add_documents([Document(page_content="stale-stage")])
        lease = stale._stage_lease
        lease._heartbeat.cancel()
        await asyncio.gather(lease._heartbeat, return_exceptions=True)
        token = stale._generation_scope.set((stale.collection_name, lease))
        try:
            await lease.check()
            await asyncio.sleep(0.35)
            if not await store._generation_gc.collect(stale.collection_name):
                raise AssertionError("expired stage was not reclaimed")
            try:
                await asyncio.to_thread(stale._atomic_delete, [stale._generate_doc_id("stale-stage")])
            except Exception:
                pass
            else:
                raise AssertionError("late delete accepted an expired lease")
            if list(stale.client.scan_iter(match=f"{stale.collection_name}:*")):
                raise AssertionError("late delete resurrected a reclaimed generation")
        finally:
            stale._generation_scope.reset(token)
            await lease.close()
            await stale.close()
            store._generation_gc.lease_ms = 30000

        class FailingEmbedding:
            def embed_documents(self, texts):
                if any("fail-embedding" in text for text in texts):
                    raise RuntimeError("controlled embedding failure")
                return [[1.0, 0.0] for _ in texts]

            def embed_query(self, text):
                return [1.0, 0.0]

        store.embedding_model = FailingEmbedding()
        active = await store._read_active_generation()
        old_documents = document_keys(active)
        with tempfile.TemporaryDirectory(prefix="cloudops-rag-test-") as folder:
            Path(folder, "01-ok.txt").write_text("successful candidate", encoding="utf-8")
            Path(folder, "02-fail.txt").write_text("fail-embedding", encoding="utf-8")
            assistant = object.__new__(RAGAssistant)
            assistant.config = SimpleNamespace(rag=SimpleNamespace(knowledge_base_path=folder))
            assistant.vector_store = store
            assistant.clear_cache = AsyncMock(return_value={"success": True})
            result = await assistant.refresh_knowledge_base()
            if result["success"] or await store._read_active_generation() != active or document_keys(active) != old_documents:
                raise AssertionError("failed refresh did not preserve the prior active version")
        # 历史revision非整数：失败前不能写入活动数据或切换指针
        invalid = EnhancedRedisVectorStore({'host': '127.0.0.1', 'port': port, 'db': 0}, 'invalid', Embedding(), 2, 'FLAT')
        try:
            invalid.client.set('invalid:revision', 'not-integer')
            try:
                await invalid.add_documents([Document(page_content='invalid-version')])
            except Exception:
                pass
            else:
                raise AssertionError('invalid revision was accepted')
            if invalid.client.hget('invalid:active_generation', 'active') is not None or list(invalid.client.scan_iter(match='invalid:doc:*')):
                raise AssertionError('invalid revision changed active data')
        finally:
            await invalid.close()

        # 历史文档键类型错误：stage内MULTI可能局部生效，但活动版本必须完整保留
        wrongtype = EnhancedRedisVectorStore({'host': '127.0.0.1', 'port': port, 'db': 0}, 'wrongtype', Embedding(), 2, 'FLAT')
        try:
            doc_id = wrongtype._generate_doc_id('same-content')
            wrongtype.client.set(f'wrongtype:doc:{doc_id}', 'not-a-hash')
            try:
                await wrongtype.add_documents([Document(page_content='same-content')])
            except Exception:
                pass
            else:
                raise AssertionError('wrong-type document was accepted')
            if wrongtype.client.hget('wrongtype:active_generation', 'active') is not None or wrongtype.client.get(f'wrongtype:doc:{doc_id}') != b'not-a-hash':
                raise AssertionError('stage error damaged active data')
            if wrongtype.client.get('wrongtype:revision') is not None:
                raise AssertionError('stage error changed active revision')
        finally:
            await wrongtype.close()
        # 真实Redis发布成功后，缓存SCAN/DEL失败必须明确说明已发布
        cache_store = EnhancedRedisVectorStore({'host': '127.0.0.1', 'port': port, 'db': 0}, 'cachefixture', Embedding(), 2, 'FLAT')
        try:
            await cache_store.add_documents([Document(page_content='seed')])
            original_cache_client = cache_store.query_cache.client

            class BrokenCache:
                def __init__(self, mode):
                    self.mode = mode

                def scan(self, *args, **kwargs):
                    if self.mode == 'scan':
                        raise redis.ResponseError('controlled-cache-error')
                    return 0, [b'cached-key']

                def delete(self, *_args):
                    raise redis.ResponseError('controlled-cache-error')

            async def published_cache_failure(operation, mode):
                cache_store.query_cache.client = BrokenCache(mode)
                try:
                    try:
                        await operation()
                    except RefreshPublishedCacheError:
                        return
                    raise AssertionError('post-publication cache failure was hidden')
                finally:
                    cache_store.query_cache.client = original_cache_client

            await published_cache_failure(lambda: cache_store.add_documents([Document(page_content='added')]), 'scan')
            if await cache_store.get_document_count() != 2:
                raise AssertionError('add did not publish before cache failure')
            await published_cache_failure(lambda: cache_store.delete_documents([cache_store._generate_doc_id('seed')]), 'delete')
            if await cache_store.get_document_count() != 1:
                raise AssertionError('delete did not publish before cache failure')
            await published_cache_failure(cache_store.clear, 'scan')
            if await cache_store.get_document_count() != 0:
                raise AssertionError('clear did not publish before cache failure')
            discard = cache_store.discard_refresh_stage
            async def fail_after_cleanup(candidate):
                await discard(candidate)
                raise RuntimeError('controlled-release-error')
            cache_store.discard_refresh_stage = fail_after_cleanup
            try:
                try:
                    await cache_store.add_documents([Document(page_content='release-failure')])
                except RefreshPublishedCacheError:
                    pass
                else:
                    raise AssertionError('post-publication resource failure was hidden')
            finally:
                cache_store.discard_refresh_stage = discard
            if await cache_store.get_document_count() != 1:
                raise AssertionError('resource cleanup error hid a committed write')
        finally:
            await cache_store.close()

        # 单独集合验证管理CLI默认只读及显式有审计处置，不触碰业务集合
        admin = EnhancedRedisVectorStore({'host': '127.0.0.1', 'port': port, 'db': 0}, 'adminfixture', Embedding(), 2, 'FLAT')
        try:
            candidate = await admin._generation_gc.acquire_stage('adminfixture-refresh-' + uuid.uuid4().hex)
            await admin._generation_gc.protect_unknown(candidate)
            await candidate.close()
            env = {**os.environ, 'CLOUDOPS_RAG_ADMIN_REDIS_URL': f'redis://127.0.0.1:{port}/0'}
            cmd = [sys.executable, 'scripts/manual_checks/rag_generation_admin.py', '--collection', 'adminfixture']
            before = json.loads(subprocess.check_output(cmd + ['inspect'], env=env, text=True))
            if len(before['uncertain']) != 1 or before['uncertain'][0]['generation'] != candidate.generation:
                raise AssertionError('readonly unknown inspection failed')
            subprocess.run(cmd + ['resolve'], input=json.dumps({'generation': candidate.generation, 'action': 'abandon', 'reason': 'confirmed not published'}), env=env, capture_output=True, text=True, check=True)
            after = json.loads(subprocess.check_output(cmd + ['inspect'], env=env, text=True))
            if after['uncertain'] or after['capacity']['resolution_audits'] != 1:
                raise AssertionError('audited unknown resolution failed')
        finally:
            await admin.close()
        print("PASS legacy migration, CAS, leases, crash recovery, failed writes and audited unknown resolution")
    finally:
        await store.close()


if __name__ == '__main__':
    nonce = uuid.uuid4().hex
    container = subprocess.check_output(['docker', 'run', '-d', '--rm', '--pull=never', '--label', 'cloudops.rag.test=' + nonce, '-p', '127.0.0.1::6379', 'redis:7.4.11-alpine'], text=True).strip()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = subprocess.run(['docker', 'exec', container, 'redis-cli', 'ping'], capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip() == 'PONG':
                break
            time.sleep(0.1)
        else:
            raise RuntimeError('临时Redis未能在限定时间内就绪')
        port = subprocess.check_output(['docker', 'port', container, '6379/tcp'], text=True).strip().rsplit(':', 1)[1]
        os.environ['AUDIT_REDIS_PORT'] = port
        asyncio.run(main())
    finally:
        label = subprocess.check_output(['docker', 'inspect', '--format', '{{index .Config.Labels "cloudops.rag.test"}}', container], text=True).strip()
        if label != nonce:
            raise RuntimeError('container ownership mismatch')
        subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL, check=True)
