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
                    "risk_assessment": {"allowed": True, "risk_level": "low", "requires_human_confirmation": False},
                }
            ],
        }

    async def execute_plan(self, plan):
        return {
            "status": "completed",
            "executed_actions": plan["candidate_actions"],
            "blocked_actions": [],
            "post_check": {"ready": True},
        }


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
                    },
                }
            ],
        }

    async def execute_plan(self, plan):
        return {
            "status": "completed",
            "executed_actions": plan["allowed_actions"],
            "blocked_actions": plan["blocked_actions"],
            "post_check": {"ready": True},
        }


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

    assert result["status"] == "completed"
    assert result["workflow_engine"] == "langgraph"
    assert result["agents_used"] == ["Coordinator", "Analyzer", "Planner", "Reviewer", "Executor"]
    assert result["candidate_actions"][0]["risk_assessment"]["allowed"] is True


class FakeAutoFixService:
    def __init__(self) -> None:
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True

    async def execute_multi_agent_workflow(self, request):
        return {
            "status": "completed",
            "workflow_engine": "langgraph",
            "problem_description": request["problem_description"],
            "agents_used": ["Coordinator", "Analyzer", "Planner", "Reviewer", "Executor"],
        }

    async def confirm_multi_agent_workflow(self, plan_id, approved_action_ids):
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
    service._pending_workflows["autofix-default-api-123"] = {
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
                    "risk_assessment": {"allowed": False, "risk_level": "high"},
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
                    "risk_assessment": {"allowed": False, "risk_level": "high"},
                }
            ],
            "blocked_actions": [
                {
                    "action_id": "patch-image-pull-policy",
                    "action_type": "patch_image_pull_policy",
                    "executable": True,
                    "risk_assessment": {"allowed": False, "risk_level": "high"},
                }
            ],
        },
        "agents_used": ["Coordinator", "Analyzer", "Planner", "Reviewer"],
        "messages": [],
    }

    result = await service.confirm_multi_agent_workflow(
        "autofix-default-api-123",
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
