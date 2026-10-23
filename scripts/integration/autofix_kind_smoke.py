#!/usr/bin/env python3
"""仅验收显式本机 kind；不调用模型、通知或操作其他命名空间"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.parse import urlsplit
import warnings

REPO = Path(__file__).resolve().parents[2]
CONTEXT = 'kind-cloudops-release'
OWNER_KEY = 'cloudops.local/autofix-owner'
NAME = 'owned-busybox'

logging.disable(logging.CRITICAL)
warnings.filterwarnings('ignore')
sys.path.insert(0, str(REPO))


class CheckFailed(Exception):
    pass


def ensure(condition: bool) -> None:
    if not condition:
        raise CheckFailed()


def guard_config(kubeconfig: Path) -> str:
    import yaml

    ensure(kubeconfig.is_file() and not kubeconfig.is_symlink())
    data = yaml.safe_load(kubeconfig.read_text())
    ensure(data.get('current-context') == CONTEXT)
    contexts = {item['name']: item['context'] for item in data.get('contexts', [])}
    ensure(CONTEXT in contexts)
    clusters = {item['name']: item['cluster'] for item in data.get('clusters', [])}
    selected = clusters[contexts[CONTEXT]['cluster']]
    parsed = urlsplit(selected.get('server', ''))
    ensure(parsed.scheme == 'https' and parsed.hostname in {'127.0.0.1', 'localhost'} and parsed.username is None and parsed.password is None)
    ensure(not selected.get('proxy-url') and selected.get('insecure-skip-tls-verify') is not True)
    users = {item['name']: item['user'] for item in data.get('users', [])}
    user = users[contexts[CONTEXT]['user']]
    ensure(not user.get('exec') and not user.get('auth-provider'))
    inspected = subprocess.run(['docker', 'inspect', 'cloudops-release-control-plane'], capture_output=True, text=True, timeout=10)
    ensure(inspected.returncode == 0)
    container = json.loads(inspected.stdout)[0]
    ensure(container['Config']['Labels'].get('io.x-k8s.kind.cluster') == 'cloudops-release')
    ports = container['NetworkSettings']['Ports'].get('6443/tcp') or []
    ensure(any(port['HostIp'] in {'127.0.0.1', '::1'} and port['HostPort'] == str(parsed.port) for port in ports))
    return selected['server'].rstrip('/')


async def run(kubeconfig: Path) -> dict:
    result = {
        'pass': False,
        'config_guard': False,
        'restart_plan_only': False,
        'restart_completed': False,
        'restart_observed': False,
        'bad_probe_api_accepted': False,
        'bad_probe_observed': False,
        'unready_top_status_failed': False,
        'namespace_cleaned': False,
    }
    service = None
    namespace_uid = None
    owner = secrets.token_hex(12)
    namespace = 'cloudops-autofix-e2e-' + owner[:12]
    ensure(namespace != 'cloudops-release-smoke' and namespace.startswith('cloudops-autofix-e2e-'))
    os.environ['K8S_NAMESPACE'] = namespace
    phase = 'configuration'
    try:
        expected_server = guard_config(kubeconfig)
        result['config_guard'] = True
        os.environ.update({'ENV': 'test', 'PYTHON_DOTENV_DISABLED': '1', 'K8S_CONFIG_PATH': str(kubeconfig), 'KUBECONFIG': str(kubeconfig), 'K8S_IN_CLUSTER': 'false'})
        from kubernetes.client.rest import ApiException

        from app.core.autofix.remediation import AutoFixRemediationEngine
        from app.services.kubernetes import KubernetesService

        phase = 'initialize'
        service = await asyncio.to_thread(KubernetesService)
        ensure(service.initialized)
        ensure(service.apps_v1.api_client.configuration.host.rstrip('/') == expected_server)
        ensure(service.core_v1.api_client.configuration.host.rstrip('/') == expected_server)

        async def sdk(operation, **kwargs):
            return await asyncio.to_thread(operation, _request_timeout=(3, 10), **kwargs)

        async def own_namespace():
            obj = await sdk(service.core_v1.read_namespace, name=namespace)
            ensure(obj.metadata.name == namespace and obj.metadata.labels.get(OWNER_KEY) == owner)
            if namespace_uid:
                ensure(obj.metadata.uid == namespace_uid)
            return obj

        async def deployment():
            obj = await sdk(service.apps_v1.read_namespaced_deployment, name=NAME, namespace=namespace)
            ensure(obj.metadata.namespace == namespace and obj.metadata.labels.get(OWNER_KEY) == owner)
            return obj

        phase = 'create_namespace'
        created = await sdk(service.core_v1.create_namespace, body={'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace, 'labels': {OWNER_KEY: owner}}})
        namespace_uid = created.metadata.uid
        await own_namespace()
        phase = 'create_deployment'
        body = {
            'apiVersion': 'apps/v1',
            'kind': 'Deployment',
            'metadata': {'name': NAME, 'namespace': namespace, 'labels': {OWNER_KEY: owner}},
            'spec': {
                'replicas': 1,
                'strategy': {'type': 'RollingUpdate', 'rollingUpdate': {'maxSurge': 1, 'maxUnavailable': 0}},
                'selector': {'matchLabels': {OWNER_KEY: owner}},
                'template': {
                    'metadata': {'labels': {OWNER_KEY: owner}},
                    'spec': {
                        'terminationGracePeriodSeconds': 1,
                        'automountServiceAccountToken': False,
                        'containers': [
                            {
                                'name': NAME,
                                'image': 'busybox:1.37.0',
                                'imagePullPolicy': 'Never',
                                'command': ['/bin/sh', '-c', "trap 'exit 0' TERM INT; while :; do sleep 1 & wait $!; done"],
                                'resources': {'requests': {'cpu': '10m', 'memory': '8Mi'}, 'limits': {'cpu': '100m', 'memory': '32Mi'}},
                            }
                        ],
                    },
                },
            },
        }
        await sdk(service.apps_v1.create_namespaced_deployment, namespace=namespace, body=body)

        async def strict_rollout(timeout=45):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                obj = await deployment()
                status = obj.status
                desired = obj.spec.replicas or 0
                if (
                    (status.observed_generation or 0) >= obj.metadata.generation
                    and (status.updated_replicas or 0) == desired == (status.replicas or 0)
                    and (status.ready_replicas or 0) >= desired
                    and (status.available_replicas or 0) >= desired
                ):
                    return obj
                await asyncio.sleep(0.25)
            raise CheckFailed()

        phase = 'initial_rollout'
        before = await strict_rollout()
        old_generation = before.metadata.generation
        engine = AutoFixRemediationEngine(service, allowed_namespaces=[namespace], rollout_timeout_seconds=30, auto_execute=True)
        diagnosis = {'deployment': NAME, 'namespace': namespace, 'diagnosis': {'summary': 'Controlled local CrashLoopBackOff diagnosis'}}
        phase = 'build_restart_plan'
        plan = await engine.build_plan(diagnosis)
        ensure(plan['deployment'] == NAME and plan['namespace'] == namespace and plan['fault_type'] == 'crash_loop_backoff')
        actions = plan['allowed_actions']
        ensure(len(actions) == 1 and actions[0]['action_type'] == 'restart_deployment')
        ensure(actions[0]['target'] == {'kind': 'Deployment', 'name': NAME, 'namespace': namespace})
        result['restart_plan_only'] = True
        await own_namespace()
        phase = 'execute_restart'
        success = await engine.execute_plan(plan)
        result['restart_completed'] = success.get('status') == 'completed' and success.get('post_check', {}).get('ready') is True
        ensure(result['restart_completed'])
        after = await strict_rollout()
        result['restart_generation_delta'] = after.metadata.generation - old_generation
        annotation = (after.spec.template.metadata.annotations or {}).get('kubectl.kubernetes.io/restartedAt')
        result['restart_observed'] = bool(annotation and after.metadata.generation > old_generation and (after.status.observed_generation or 0) >= after.metadata.generation)
        ensure(result['restart_observed'])

        phase = 'inject_owned_bad_probe'
        await own_namespace()
        accepted = await service.patch_deployment(
            NAME,
            {'spec': {'template': {'spec': {'containers': [{'name': NAME, 'readinessProbe': {'exec': {'command': ['/bin/sh', '-c', 'exit 1']}, 'periodSeconds': 1, 'timeoutSeconds': 1, 'failureThreshold': 1}}]}}}},
            namespace,
        )
        result['bad_probe_api_accepted'] = accepted is True
        ensure(accepted)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            observed = await deployment()
            pods = await sdk(service.core_v1.list_namespaced_pod, namespace=namespace, label_selector=OWNER_KEY + '=' + owner)
            bad_running = any(pod.spec.containers[0].readiness_probe is not None and pod.status.phase == 'Running' and any(status.ready is False for status in (pod.status.container_statuses or [])) for pod in pods.items)
            if bad_running and (observed.status.updated_replicas or 0) >= 1 and (observed.status.observed_generation or 0) >= observed.metadata.generation:
                result['bad_probe_observed'] = True
                result['bad_before_counts'] = {
                    'replicas': observed.status.replicas or 0,
                    'updated': observed.status.updated_replicas or 0,
                    'ready': observed.status.ready_replicas or 0,
                    'available': observed.status.available_replicas or 0,
                }
                break
            await asyncio.sleep(0.25)
        ensure(result['bad_probe_observed'])
        failed_engine = AutoFixRemediationEngine(service, allowed_namespaces=[namespace], rollout_timeout_seconds=1, auto_execute=True)
        phase = 'build_unready_plan'
        bad_plan = await failed_engine.build_plan(diagnosis)
        ensure([action['action_type'] for action in bad_plan['allowed_actions']] == ['restart_deployment'])
        phase = 'execute_unready_plan'
        started = time.monotonic()
        failed = await failed_engine.execute_plan(bad_plan)
        result['bad_elapsed_seconds'] = round(time.monotonic() - started, 3)
        result['bad_top_status'] = failed.get('status')
        result['bad_post_ready'] = failed.get('post_check', {}).get('ready')
        result['bad_action_api_accepted'] = failed.get('executed_actions', [{}])[0].get('status') == 'succeeded'
        result['unready_top_status_failed'] = failed.get('status') == 'failed' and failed.get('post_check', {}).get('ready') is False and result['bad_action_api_accepted']
        ensure(result['unready_top_status_failed'])
        result['pass'] = True
    except BaseException as error:
        result['pass'] = False
        result['failure_phase'] = phase
        result['error_type'] = type(error).__name__
    finally:
        if service and result['config_guard']:
            phase = 'cleanup'
            try:
                from kubernetes.client.rest import ApiException

                try:
                    obj = await asyncio.to_thread(service.core_v1.read_namespace, name=namespace, _request_timeout=(3, 10))
                except ApiException as error:
                    if error.status == 404:
                        result['namespace_cleaned'] = True
                    else:
                        raise
                else:
                    ensure(obj.metadata.name == namespace and obj.metadata.labels.get(OWNER_KEY) == owner)
                    ensure(namespace_uid is None or obj.metadata.uid == namespace_uid)
                    await asyncio.to_thread(service.core_v1.delete_namespace, name=namespace, body={'preconditions': {'uid': obj.metadata.uid}}, _request_timeout=(3, 10))
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline:
                        try:
                            await asyncio.to_thread(service.core_v1.read_namespace, name=namespace, _request_timeout=(3, 10))
                        except ApiException as error:
                            if error.status == 404:
                                result['namespace_cleaned'] = True
                                break
                            raise
                        await asyncio.sleep(0.25)
                    ensure(result['namespace_cleaned'])
            except BaseException as error:
                result['pass'] = False
                result['cleanup_error_type'] = type(error).__name__
            for api in [service.apps_v1, service.core_v1]:
                try:
                    if api:
                        api.api_client.close()
                except Exception:
                    pass
        result['pass'] = bool(result['pass'] and result['namespace_cleaned'])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='确认在自有本机kind创建并清理唯一测试命名空间')
    parser.add_argument('--kubeconfig', type=Path, required=True, help='明确指定自有kind-cloudops-release配置，禁止使用默认凭据')
    parser.add_argument('--result', type=Path, help='可选的新JSON结果文件，不覆盖已有文件')
    args = parser.parse_args()
    if not args.execute:
        parser.error('实际集群写入验收必须显式指定 --execute')
    if args.result and args.result.exists():
        parser.error('结果文件已存在，拒绝覆盖')
    try:
        result = asyncio.run(run(args.kubeconfig))
    except BaseException as error:
        result = {'pass': False, 'failure_phase': 'runner', 'error_type': type(error).__name__}
    if args.result:
        descriptor = os.open(args.result, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(result, stream, separators=(',', ':'), sort_keys=True)
            stream.write('\n')
    print(json.dumps(result, separators=(',', ':'), sort_keys=True))
    return 0 if result.get('pass') is True else 1


if __name__ == '__main__':
    raise SystemExit(main())
