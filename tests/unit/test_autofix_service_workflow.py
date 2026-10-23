#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import pytest

from app.api.routes import autofix as autofix_routes
from app.models import AutoFixWorkflowConfirmRequest
from app.services.autofix_service import AutoFixService


class FakeRemediationEngine:
    async def build_plan(self, diagnosis):
        return {
            "fault_type": "image_pull_failure",
            "candidate_actions": [
                {
                    "action_id": "patch-image-pull-policy",
                    "action_type": "patch_image_pull_policy",
                    "executable": True,
                    # SEC-44：自动执行需要实时证据（由真实引擎实时读取 Pod 得到，这里是替身给出的假值）
                    "risk_assessment": {
                        "allowed": True,
                        "risk_level": "low",
                        "requires_human_confirmation": False,
                        "live_evidence": {"source": "live", "fault_type": "image_pull_failure", "checked": True, "matched": True},
                    },
                }
            ],
        }

    async def execute_plan(self, plan, **_kwargs):
        return {
            "status": "completed",
            "executed_actions": plan["candidate_actions"],
            "blocked_actions": [],
            "post_check": {"ready": True},
        }

    def namespace_refusal(self, namespace, actions):
        return None


class FakeBlockedRemediationEngine:
    async def build_plan(self, diagnosis):
        return {
            "deployment": diagnosis.get("deployment"),
            "namespace": diagnosis.get("namespace"),
            "fault_type": "image_pull_failure",
            "candidate_actions": [
                {
                    "action_id": "patch-image-pull-policy",
                    "action_type": "patch_image_pull_policy",
                    "executable": True,
                    "risk_assessment": {
                        "allowed": False,
                        "risk_level": "high",
                        "requires_human_confirmation": True,
                        "namespace_allowed": True, "permission_allowed": True, "resource_exists": True,
                    },
                }
            ],
            "allowed_actions": [],
            "blocked_actions": [
                {
                    "action_id": "patch-image-pull-policy",
                    "action_type": "patch_image_pull_policy",
                    "executable": True,
                    "risk_assessment": {
                        "allowed": False,
                        "risk_level": "high",
                        "requires_human_confirmation": True,
                        "namespace_allowed": True, "permission_allowed": True, "resource_exists": True,
                    },
                }
            ],
        }

    async def execute_plan(self, plan, **_kwargs):
        return {
            "status": "completed",
            "executed_actions": [{**action, "status": "succeeded"} for action in plan["allowed_actions"]],
            "blocked_actions": plan["blocked_actions"],
            "post_check": {"ready": True},
        }

    def namespace_refusal(self, namespace, actions):
        return None


@pytest.mark.asyncio
async def test_autofix_service_executes_multi_agent_workflow() -> None:
    service = AutoFixService()
    service._initialized = True
    service._remediation_engine = FakeRemediationEngine()

    result = await service.execute_multi_agent_workflow(
        {
            "problem_description": "deployment api ImagePullBackOff",
            "deployment": "api",
            "namespace": "default",
        }
    )

    # SEC-26：默认只出计划，低风险动作也进入待确认
    assert result["status"] == "needs_human_confirmation"
    assert result["workflow_engine"] == "langgraph"
    assert result["agents_used"] == ["Coordinator", "Analyzer", "Planner", "Reviewer"]
    assert result["candidate_actions"][0]["risk_assessment"]["allowed"] is False
    assert result["approval"]["state"] == "pending"
    assert result["approval"]["pending_action_ids"] == ["patch-image-pull-policy"]


@pytest.mark.asyncio
async def test_autofix_service_legacy_auto_execute_switch(monkeypatch) -> None:
    from app.config.settings import config

    monkeypatch.setattr(config.autofix, "auto_execute_enabled", True)
    service = AutoFixService()
    service._initialized = True
    service._remediation_engine = FakeRemediationEngine()

    result = await service.execute_multi_agent_workflow(
        {"problem_description": "deployment api ImagePullBackOff", "deployment": "api", "namespace": "default"}
    )

    assert result["status"] == "completed"
    assert result["agents_used"] == ["Coordinator", "Analyzer", "Planner", "Reviewer", "Executor"]
    assert result["candidate_actions"][0]["risk_assessment"]["allowed"] is True


class FakeAutoFixService:
    def __init__(self) -> None:
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True

    async def execute_multi_agent_workflow(self, request):
        await self.initialize()
        return {
            "status": "completed",
            "workflow_engine": "langgraph",
            "problem_description": request["problem_description"],
            "agents_used": ["Coordinator", "Analyzer", "Planner", "Reviewer", "Executor"],
        }

    async def confirm_multi_agent_workflow(self, plan_id, approved_action_ids):
        await self.initialize()
        return {
            "status": "completed",
            "workflow_engine": "langgraph",
            "plan_id": plan_id,
            "executed_actions": [
                {
                    "action_id": "patch-image-pull-policy",
                    "action_type": "patch_image_pull_policy",
                }
            ],
            "approved_action_ids": approved_action_ids,
            "blocked_actions": [],
        }


@pytest.mark.asyncio
async def test_workflow_route_returns_langgraph_execution_result(monkeypatch) -> None:
    fake_service = FakeAutoFixService()

    async def fake_get_autofix_service():
        return fake_service

    monkeypatch.setattr(autofix_routes, "get_autofix_service", fake_get_autofix_service)

    response = await autofix_routes.execute_workflow({"problem_description": "api ImagePullBackOff"})

    assert fake_service.initialized is True
    assert response["code"] == 0
    assert response["data"]["status"] == "completed"
    assert response["data"]["workflow_engine"] == "langgraph"


@pytest.mark.asyncio
async def test_autofix_service_confirms_human_review_actions() -> None:
    service = AutoFixService()
    service._initialized = True
    service._remediation_engine = FakeBlockedRemediationEngine()
    approval = service._approvals.reserve()
    workflow_result = {
        "status": "needs_human_confirmation",
        "next_action": "human_confirm",
        "diagnosis": {
            "deployment": "api",
            "namespace": "default",
            "diagnosis": {
                "deployment": "api",
                "namespace": "default",
                "problem_description": "api ImagePullBackOff",
            },
        },
        "review": {
            "approved": False,
            "blocked_actions": [
                {
                    "action_id": "patch-image-pull-policy",
                    "action_type": "patch_image_pull_policy",
                    "executable": True,
                    "risk_assessment": {"allowed": False, "risk_level": "high", "namespace_allowed": True, "permission_allowed": True, "resource_exists": True},
                }
            ],
            "allowed_actions": [],
        },
        "plan": {
            "plan_id": "autofix-default-api-123",
            "deployment": "api",
            "namespace": "default",
            "candidate_actions": [
                {
                    "action_id": "patch-image-pull-policy",
                    "action_type": "patch_image_pull_policy",
                    "executable": True,
                    "risk_assessment": {"allowed": False, "risk_level": "high", "namespace_allowed": True, "permission_allowed": True, "resource_exists": True},
                }
            ],
            "blocked_actions": [
                {
                    "action_id": "patch-image-pull-policy",
                    "action_type": "patch_image_pull_policy",
                    "executable": True,
                    "risk_assessment": {"allowed": False, "risk_level": "high", "namespace_allowed": True, "permission_allowed": True, "resource_exists": True},
                }
            ],
        },
        "agents_used": ["Coordinator", "Analyzer", "Planner", "Reviewer"],
        "messages": [],
    }

    service._approvals.publish(approval, workflow_result)
    result = await service.confirm_multi_agent_workflow(
        approval.plan_id,
        ["patch-image-pull-policy"],
    )

    assert result["status"] == "completed"
    assert result["next_action"] == "finish"
    assert result["executed_actions"][0]["action_id"] == "patch-image-pull-policy"
    assert "HumanConfirm" in result["agents_used"]
    assert "Executor" in result["agents_used"]


@pytest.mark.asyncio
async def test_confirm_workflow_route_returns_execution_result(monkeypatch) -> None:
    fake_service = FakeAutoFixService()

    async def fake_get_autofix_service():
        return fake_service

    monkeypatch.setattr(autofix_routes, "get_autofix_service", fake_get_autofix_service)

    response = await autofix_routes.confirm_workflow(
        AutoFixWorkflowConfirmRequest(
            plan_id="autofix-default-api-123",
            approved_action_ids=["patch-image-pull-policy"],
        )
    )

    assert fake_service.initialized is True
    assert response["code"] == 0
    assert response["data"]["status"] == "completed"
    assert response["data"]["plan_id"] == "autofix-default-api-123"
