"""Run owner-isolation and restart checks against an owned local Redis container."""

import argparse
import asyncio
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

parser = argparse.ArgumentParser()
parser.add_argument("--execute", action="store_true")
args = parser.parse_args()
if not args.execute:
    parser.error("必须显式使用 --execute")
os.environ.update({"ENV": "test", "PYTHON_DOTENV_DISABLED": "1", "KUBECONFIG": "/nonexistent", "K8S_CONFIG_PATH": "/nonexistent", "LLM_API_KEY": "", "AIOPS_API_KEY": "offline-service-key-with-32-characters"})
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI
import httpx
import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.api.middleware.error_handler import setup_error_handlers
from app.api.routes import autofix
from app.common.exceptions import AIOpsException
from app.common.identity import principal_scope
from app.services.autofix_service import AutoFixService
from app.services.workflow_approval_redis import _CLAIM, _TRANSITION, RedisWorkflowApprovals

ACTION = "restart-deployment"
OPERATION = "11111111-1111-4111-8111-111111111111"


def pending_result():
    action = {
        "action_id": ACTION,
        "action_type": "restart_deployment",
        "executable": True,
        "parameters": {},
        "namespace": "default",
        "deployment": "api",
        "risk_assessment": {"allowed": False, "requires_human_confirmation": True, "namespace_allowed": True, "permission_allowed": True, "resource_exists": True},
    }
    plan = {"namespace": "default", "deployment": "api", "candidate_actions": [action], "blocked_actions": [action], "allowed_actions": []}
    return {"status": "needs_human_confirmation", "plan": plan, "blocked_actions": [action], "review": {"approved": False}, "diagnosis": {"namespace": "default", "deployment": "api"}, "executed_actions": []}


def command(*values):
    return subprocess.run(values, check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def start_container(aof=True):
    name = "cloudops-autofix-" + uuid.uuid4().hex
    command(
        "docker",
        "run",
        "-d",
        "--pull=never",
        "--name",
        name,
        "--label",
        f"cloudops.autofix={name}",
        "-p",
        "127.0.0.1::6379",
        "redis:7.4.11-alpine",
        "redis-server",
        "--save",
        "",
        "--requirepass",
        "offline-test-password",
        "--appendonly",
        "yes" if aof else "no",
        "--appendfsync",
        "always",
    )
    return name


def port_for(name):
    address = command("docker", "port", name, "6379/tcp")
    if not address.startswith("127.0.0.1:"):
        raise RuntimeError("拒绝非本机 Redis")
    return int(address.rsplit(":", 1)[1])


async def ready(name):
    client = redis.Redis(host="127.0.0.1", port=port_for(name), password="offline-test-password", decode_responses=True, socket_timeout=3)
    for _ in range(50):
        try:
            if await client.ping():
                return client
        except Exception:
            await asyncio.sleep(0.1)
    await client.aclose()
    raise RuntimeError("隔离 Redis 未就绪")


def service(client):
    svc = AutoFixService(approvals=RedisWorkflowApprovals(client))
    svc._initialized = True
    svc._ops_workflow = SimpleNamespace(run=AsyncMock(side_effect=lambda *_a, **_k: deepcopy(pending_result())))
    svc._remediation_engine = SimpleNamespace(
        build_plan=AsyncMock(side_effect=lambda *_a: deepcopy(pending_result()["plan"])),
        execute_plan=AsyncMock(return_value={"status": "completed", "executed_actions": [{"action_id": ACTION, "status": "succeeded"}]}),
        namespace_refusal=lambda *_: None,
    )
    return svc


class LostReplyPipeline:
    def __init__(self, pipe, target):
        self.pipe = pipe
        self.target = target
        self.lose = False

    async def __aenter__(self):
        await self.pipe.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.pipe.__aexit__(*args)

    def config_get(self, *args):
        self.pipe.config_get(*args)
        return self

    def eval(self, script, numkeys, *args):
        if self.target == "claim" and script == _CLAIM:
            self.lose = True
        if self.target == "completed" and script == _TRANSITION and args[numkeys + 3] == "completed":
            self.lose = True
        self.pipe.eval(script, numkeys, *args)
        return self

    def execute_command(self, *args):
        self.pipe.execute_command(*args)
        return self

    async def execute(self):
        result = await self.pipe.execute()
        if self.lose:
            raise RedisConnectionError("simulated reply loss")
        return result


class LostReplyClient:
    def __init__(self, client, target):
        self.client, self.target = client, target

    def pipeline(self, **kwargs):
        return LostReplyPipeline(self.client.pipeline(**kwargs), self.target)


async def check(name):
    client = await ready(name)
    first = service(client)
    with principal_scope("user:1"):
        initial = await first.execute_multi_agent_workflow({"problem_description": "offline", "deployment": "api", "operation_id": OPERATION})
        plan_id = initial["plan"]["plan_id"]
        assert initial["status"] == "needs_human_confirmation"
        try:
            await first.execute_multi_agent_workflow({"problem_description": "offline", "deployment": "api", "operation_id": OPERATION})
        except AIOpsException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("duplicate operation accepted")
        try:
            await first.execute_multi_agent_workflow({"problem_description": "different", "deployment": "api", "operation_id": OPERATION})
        except AIOpsException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("operation ID reused with different input")
    await client.aclose()
    # SIGKILL prevents graceful shutdown; the AOF must recover the pending plan.
    command("docker", "kill", "--signal", "KILL", name)
    command("docker", "start", name)
    client = await ready(name)
    first, second = service(client), service(client)
    with principal_scope("user:2"):
        try:
            await first.confirm_multi_agent_workflow(plan_id, [ACTION])
        except AIOpsException as error:
            assert error.status_code == 403
        else:
            raise AssertionError("cross-owner access accepted")
    entered, resume = asyncio.Event(), asyncio.Event()

    async def execute(*_a, **_kw):
        entered.set()
        await resume.wait()
        return {"status": "completed", "executed_actions": [{"action_id": ACTION, "status": "succeeded"}]}

    first._remediation_engine.execute_plan.side_effect = execute
    with principal_scope("user:1"):
        task = asyncio.create_task(first.confirm_multi_agent_workflow(plan_id, [ACTION]))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            await second.confirm_multi_agent_workflow(plan_id, [ACTION])
        except AIOpsException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("concurrent claim accepted")
        resume.set()
        await task
        assert first._remediation_engine.execute_plan.await_count == 1
        assert second._remediation_engine.execute_plan.await_count == 0
        history = await first._approvals.history()
        assert any(item["plan_id"] == plan_id and item["state"] == "completed" for item in history)
        status = await first._approvals.get_operation(OPERATION)
        assert status["approval_state"] == "consumed" and not status["retry_safe"]
        assert status["execution_summary"]["executed_actions"][0]["action_id"] == ACTION
        assert status["created_at"] and status["updated_at"]
        # A claimed operation survives a crash and can never be claimed again.
        operation2 = "22222222-2222-4222-8222-222222222222"
        second_plan = await first.execute_multi_agent_workflow({"problem_description": "offline-2", "deployment": "api", "operation_id": operation2})
        plan2 = second_plan["plan"]["plan_id"]
        claim = await first._approvals.claim(plan2, [ACTION])
        await first._approvals.mark_effects_started(claim)
    await client.aclose()
    command("docker", "kill", "--signal", "KILL", name)
    command("docker", "start", name)
    client = await ready(name)
    recovered = service(client)
    with principal_scope("user:1"):
        try:
            await recovered.confirm_multi_agent_workflow(plan2, [ACTION])
        except AIOpsException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("crashed claim was replayed")
        assert recovered._remediation_engine.execute_plan.await_count == 0
        # Expired in-flight state is uncertain, never moved back to pending.
        await client.hset("aiops:autofix:approval:v1:plan:" + plan2, "expires_ms", "1")
        recovered_state = await recovered._approvals.get_operation(operation2)
        assert recovered_state["state"] == "uncertain" and not recovered_state["retry_safe"]
        assert recovered_state["claimed_action_ids"] == [ACTION]
        # The original automatic workflow is also protected before its first
        # side effect; a lost publication reply cannot trigger a second run.
        operation8 = "88888888-8888-4888-8888-888888888888"
        auto = service(LostReplyClient(client, "completed"))

        async def automatic(_request, *, plan_only):
            assert plan_only is False
            return {"status": "completed", "plan": {"candidate_actions": []}, "executed_actions": [{"action_id": ACTION, "status": "succeeded"}], "execution": {"status": "completed"}}

        auto._ops_workflow.run.side_effect = automatic
        try:
            await auto.execute_multi_agent_workflow({"problem_description": "offline-auto", "operation_id": operation8})
        except AIOpsException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("lost workflow reply was treated as success")
        assert auto._ops_workflow.run.await_count == 1
        try:
            await recovered.execute_multi_agent_workflow({"problem_description": "offline-auto", "operation_id": operation8})
        except AIOpsException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("automatic workflow was replayed")
        # A claim that commits but loses its reply must never call the executor.
        operation3 = "33333333-3333-4333-8333-333333333333"
        plan3 = (await recovered.execute_multi_agent_workflow({"problem_description": "offline-3", "deployment": "api", "operation_id": operation3}))["plan"]["plan_id"]
        lost_claim = service(LostReplyClient(client, "claim"))
        try:
            await lost_claim.confirm_multi_agent_workflow(plan3, [ACTION])
        except AIOpsException as error:
            assert error.status_code == 503
        else:
            raise AssertionError("lost claim reply was treated as success")
        assert lost_claim._remediation_engine.execute_plan.await_count == 0
        try:
            await recovered.confirm_multi_agent_workflow(plan3, [ACTION])
        except AIOpsException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("lost claim was replayed")
        # A completed side effect whose persistence reply is lost must not repeat.
        operation4 = "44444444-4444-4444-8444-444444444444"
        plan4 = (await recovered.execute_multi_agent_workflow({"problem_description": "offline-4", "deployment": "api", "operation_id": operation4}))["plan"]["plan_id"]
        lost_publish = service(LostReplyClient(client, "completed"))
        try:
            await lost_publish.confirm_multi_agent_workflow(plan4, [ACTION])
        except AIOpsException:
            pass
        else:
            raise AssertionError("lost publication reply was treated as success")
        assert lost_publish._remediation_engine.execute_plan.await_count == 1
        try:
            await recovered.confirm_multi_agent_workflow(plan4, [ACTION])
        except AIOpsException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("completed side effect was replayed")
        # A stale reader must not claim a new pending revision of the same plan.
        operation7 = "77777777-7777-4777-8777-777777777777"
        plan7 = (await recovered.execute_multi_agent_workflow({"problem_description": "offline-7", "deployment": "api", "operation_id": operation7}))["plan"]["plan_id"]
        stale = RedisWorkflowApprovals(client)
        original_get = stale.get

        async def stale_get(plan_id):
            snapshot = await original_get(plan_id)
            current = await recovered._approvals.claim(plan_id, [ACTION])
            revised = deepcopy(current.result)
            revised["diagnosis"]["revision"] = "new"
            await recovered._approvals.publish(current, revised)
            return snapshot

        stale.get = stale_get
        try:
            await stale.claim(plan7, [ACTION])
        except AIOpsException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("stale approval snapshot claimed a newer revision")
        assert (await recovered._approvals.get(plan7)).state == "pending"
        # Simulate a worker dying after an externally visible side effect and
        # before recording its outcome; recovery must never replay it.
        operation9 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        plan9 = (await recovered.execute_multi_agent_workflow({"problem_description": "offline-9", "deployment": "api", "operation_id": operation9}))["plan"]["plan_id"]
        crash_worker = """import asyncio,os,sys,redis.asyncio as redis
from app.common.identity import principal_scope
from app.services.workflow_approval_redis import RedisWorkflowApprovals
async def run():
 c=redis.Redis(host='127.0.0.1',port=int(sys.argv[1]),password='offline-test-password',decode_responses=True)
 with principal_scope('user:1'):
  store=RedisWorkflowApprovals(c);record=await store.claim(sys.argv[2],['restart-deployment'])
  await store.mark_effects_started(record);await c.incr('offline:fake-side-effect')
 os._exit(0)
asyncio.run(run())
"""
        crashed = await asyncio.create_subprocess_exec(sys.executable, "-c", crash_worker, str(port_for(name)), plan9, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(crashed.communicate(), 15)
            assert crashed.returncode == 0 and await client.get("offline:fake-side-effect") == "1"
        finally:
            if crashed.returncode is None:
                crashed.kill()
                await crashed.wait()
        try:
            await recovered.confirm_multi_agent_workflow(plan9, [ACTION])
        except AIOpsException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("crashed side effect was replayed")
        assert await client.get("offline:fake-side-effect") == "1"
        operation10 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        plan10 = (await recovered.execute_multi_agent_workflow({"problem_description": "offline-10", "operation_id": operation10}))["plan"]["plan_id"]
        await client.hset("aiops:autofix:approval:v1:plan:" + plan10, "state", "invalid-store-state")
        try:
            await recovered.confirm_multi_agent_workflow(plan10, [ACTION])
        except AIOpsException as error:
            assert error.status_code == 503
        else:
            raise AssertionError("corrupt stored state was accepted")
        await client.hset("aiops:autofix:approval:v1:plan:" + plan10, "state", "pending")
        # Separate Python processes exercise the same Redis claim, not a local lock.
        operation5 = "55555555-5555-4555-8555-555555555555"
        plan5 = (await recovered.execute_multi_agent_workflow({"problem_description": "offline-5", "deployment": "api", "operation_id": operation5}))["plan"]["plan_id"]
        worker = """import asyncio,sys,redis.asyncio as redis
from app.common.identity import principal_scope
from app.common.exceptions import AIOpsException
from app.services.workflow_approval_redis import RedisWorkflowApprovals
async def run():
 c=redis.Redis(host='127.0.0.1',port=int(sys.argv[1]),password='offline-test-password',decode_responses=True)
 try:
  with principal_scope('user:1'):
   try: await RedisWorkflowApprovals(c).claim(sys.argv[2],['restart-deployment']); print('won')
   except AIOpsException as e: print('conflict' if e.status_code==409 else 'unexpected')
 finally: await c.aclose()
asyncio.run(run())
"""
        workers = [await asyncio.create_subprocess_exec(sys.executable, "-c", worker, str(port_for(name)), plan5, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE) for _ in range(2)]
        try:
            outputs = await asyncio.wait_for(asyncio.gather(*(process.communicate() for process in workers)), 15)
            assert sorted(out.decode().strip() for out, _ in outputs) == ["conflict", "won"]
            assert all(process.returncode == 0 for process in workers)
        finally:
            for process in workers:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
    # Unsafe eviction must never authorize an executor call, even if Redis
    # accepts the script before the client inspects its configuration.
    await client.config_set("maxmemory-policy", "allkeys-lru")
    unsafe = service(client)
    with principal_scope("user:1"):
        try:
            await unsafe.execute_multi_agent_workflow({"problem_description": "unsafe-policy", "operation_id": "99999999-9999-4999-8999-999999999999"})
        except AIOpsException as error:
            assert error.status_code == 503
        else:
            raise AssertionError("unsafe eviction policy accepted")
        assert unsafe._ops_workflow.run.await_count == 0
    await client.config_set("maxmemory-policy", "noeviction")
    # Exercise actual ASGI routes with the same Redis and only synthetic identities.
    api_service = service(client)
    previous = autofix.autofix_service
    autofix.autofix_service = api_service
    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(autofix.router, prefix="/api/v1/autofix")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as http:
            with principal_scope("user:1"):
                op = "66666666-6666-4666-8666-666666666666"
                response = await http.post("/api/v1/autofix/workflow", json={"problem_description": "offline-asgi", "operation_id": op})
                assert response.status_code == 200
                plan = response.json()["data"]["plan"]["plan_id"]
                detail = await http.get("/api/v1/autofix/workflow/operations/" + op)
                assert detail.status_code == 200 and detail.json()["data"]["plan_id"] == plan
                history = await http.get("/api/v1/autofix/workflow/history")
                assert history.status_code == 200 and any(item["plan_id"] == plan for item in history.json()["data"]["items"])
                by_plan = await http.get("/api/v1/autofix/workflow/plans/" + plan)
                assert by_plan.status_code == 200 and by_plan.json()["data"]["state"] == "pending"
            with principal_scope("user:2"):
                foreign = await http.get("/api/v1/autofix/workflow/plans/" + plan)
                assert foreign.status_code == 403
    finally:
        autofix.autofix_service = previous
    await client.aclose()
    print("PASS kill/restart, owner isolation, durable CAS across processes, reply loss, ASGI and uncertain recovery")


async def no_aof(name):
    client = await ready(name)
    svc = service(client)
    with principal_scope("user:1"):
        try:
            await svc.execute_multi_agent_workflow({"problem_description": "offline", "operation_id": OPERATION})
        except AIOpsException as error:
            assert error.status_code == 503
        else:
            raise AssertionError("AOF-disabled store accepted")
        assert svc._ops_workflow.run.await_count == 0
        try:
            await svc._approvals.history()
        except AIOpsException as error:
            assert error.status_code == 503
        else:
            raise AssertionError("no-AOF history falsely appeared empty")
    await client.aclose()
    print("PASS no-AOF fail closed")


def remove(name):
    label = command("docker", "inspect", "--format", '{{index .Config.Labels "cloudops.autofix"}}', name)
    if label != name:
        raise RuntimeError("拒绝清理非本次 Redis")
    command("docker", "rm", "-f", name)


if __name__ == "__main__":
    names = []
    try:
        names.append(start_container())
        asyncio.run(check(names[-1]))
        names.append(start_container(False))
        asyncio.run(no_aof(names[-1]))
    finally:
        for name in names:
            remove(name)
