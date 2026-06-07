#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from typing import Any, Dict, List

import pytest

from app.core.autofix.remediation import AutoFixRemediationEngine


class FakeK8sClient:
    def __init__(self, deployment: Dict[str, Any] | None = None, allowed: bool = True) -> None:
        self.deployment = deployment
        self.allowed = allowed
        self.restarted: List[tuple[str, str]] = []
        self.scaled: List[tuple[str, str, int]] = []
        self.patched: List[tuple[str, str, Dict[str, Any]]] = []
        self.rollout_checks: List[tuple[str, str]] = []

    async def get_deployment(self, name: str, namespace: str) -> Dict[str, Any] | None:
        return self.deployment

    async def can_i(self, verb: str, resource: str, namespace: str) -> bool:
        return self.allowed

    async def restart_deployment(self, name: str, namespace: str) -> bool:
        self.restarted.append((namespace, name))
        return True

    async def scale_deployment(self, name: str, replicas: int, namespace: str) -> bool:
        self.scaled.append((namespace, name, replicas))
        return True

    async def patch_deployment(self, name: str, patch: Dict[str, Any], namespace: str) -> bool:
        self.patched.append((namespace, name, patch))
        return True

    async def wait_for_deployment_rollout(self, name: str, namespace: str, timeout_seconds: int = 120) -> Dict[str, Any]:
        self.rollout_checks.append((namespace, name))
        return {"ready": True, "replicas": self.deployment["spec"]["replicas"]}


def deployment(name: str = "api", replicas: int = 1) -> Dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": "default"},
        "spec": {
            "replicas": replicas,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "api",
                            "image": "repo/api:bad",
                            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                        }
                    ]
                }
            },
        },
    }


@pytest.mark.asyncio
async def test_crashloop_diagnosis_generates_restart_action_with_risk_assessment() -> None:
    engine = AutoFixRemediationEngine(k8s_client=FakeK8sClient(deployment()))

    plan = await engine.build_plan(
        {
            "deployment": "api",
            "namespace": "default",
            "diagnosis": {
                "events": [{"reason": "BackOff", "message": "CrashLoopBackOff restarting failed container"}],
                "pods": [{"status": {"container_statuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}],
            },
        }
    )

    assert plan["fault_type"] == "crash_loop_backoff"
    assert [action["action_type"] for action in plan["candidate_actions"]] == ["inspect_logs", "inspect_configuration", "restart_deployment"]
    restart = plan["candidate_actions"][-1]
    assert restart["executable"] is True
    assert restart["risk_assessment"]["allowed"] is True
    assert restart["risk_assessment"]["rollback_available"] is True


@pytest.mark.asyncio
async def test_high_risk_multi_replica_restart_is_blocked_from_execution() -> None:
    fake_k8s = FakeK8sClient(deployment(replicas=3))
    engine = AutoFixRemediationEngine(k8s_client=fake_k8s)

    result = await engine.plan_and_execute(
        {
            "deployment": "api",
            "namespace": "default",
            "event": "CrashLoopBackOff",
            "diagnosis": {"events": [{"reason": "BackOff", "message": "CrashLoopBackOff"}]},
        }
    )

    assert result["status"] == "blocked"
    assert result["executed_actions"] == []
    assert fake_k8s.restarted == []
    assert result["blocked_actions"][0]["risk_assessment"]["risk_level"] == "high"
    assert "multiple replicas" in result["blocked_actions"][0]["risk_assessment"]["reasons"]


@pytest.mark.asyncio
async def test_image_pull_failure_executes_safe_image_pull_policy_patch_and_records_rollout() -> None:
    fake_k8s = FakeK8sClient(deployment())
    engine = AutoFixRemediationEngine(k8s_client=fake_k8s)

    result = await engine.plan_and_execute(
        {
            "deployment": "api",
            "namespace": "default",
            "event": "ImagePullBackOff failed to pull image",
            "diagnosis": {"events": [{"reason": "ImagePullBackOff", "message": "failed to pull image repo/api:bad"}]},
        }
    )

    assert result["status"] == "completed"
    assert result["executed_actions"][0]["action_type"] == "patch_image_pull_policy"
    assert fake_k8s.patched == [
        (
            "default",
            "api",
            {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "api",
                                    "imagePullPolicy": "IfNotPresent",
                                }
                            ]
                        }
                    }
                }
            },
        )
    ]
    assert result["post_check"]["ready"] is True


@pytest.mark.asyncio
async def test_resource_adjustment_plan_targets_actual_container_name() -> None:
    fake_deployment = deployment(name="api-deploy")
    fake_deployment["spec"]["template"]["spec"]["containers"][0]["name"] = "api-container"
    engine = AutoFixRemediationEngine(k8s_client=FakeK8sClient(fake_deployment))

    plan = await engine.build_plan(
        {
            "deployment": "api-deploy",
            "namespace": "default",
            "event": "FailedScheduling insufficient memory",
            "diagnosis": {"events": [{"reason": "FailedScheduling", "message": "0/3 nodes are available: insufficient memory"}]},
        }
    )

    resource_action = next(action for action in plan["candidate_actions"] if action["action_type"] == "adjust_resource_requests")

    assert resource_action["parameters"]["container"] == "api-container"
