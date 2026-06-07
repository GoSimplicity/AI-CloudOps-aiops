#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json

import pytest

from app.core.agents.ops_workflow import OpsMultiAgentWorkflow


class FakeRemediationEngine:
    def __init__(self, plan):
        self.plan = plan
        self.executed = False

    async def build_plan(self, diagnosis):
        return self.plan

    async def execute_plan(self, plan):
        self.executed = True
        return {"status": "completed", "executed_actions": plan["candidate_actions"], "blocked_actions": [], "post_check": {"ready": True}}


class FakeLLMClient:
    provider = "fake"
    model = "fake-model"

    def __init__(self):
        self.calls = []

    async def generate_response(
        self,
        messages,
        system_prompt=None,
        response_format=None,
        temperature=None,
        stream=False,
        max_tokens=None,
        use_task_model=False,
    ):
        self.calls.append(
            {
                "messages": messages,
                "system_prompt": system_prompt,
                "use_task_model": use_task_model,
            }
        )

        prompt = messages[-1]["content"]
        if "分析以下问题" in prompt:
            return json.dumps(
                {
                    "problem_type": "kubernetes_image_pull",
                    "components": ["deployment", "image_registry"],
                    "severity": "high",
                    "suggested_approach": "先验证镜像与拉取策略，再评估自动修复风险",
                    "required_agents": ["K8sFixer"],
                }
            )
        if "结构化Kubernetes故障分析" in prompt:
            return json.dumps(
                {
                    "fault_type": "image_pull_failure",
                    "observations": ["镜像拉取失败", "Deployment 当前不可用"],
                    "suspected_causes": ["镜像地址错误", "imagePullPolicy 不合适"],
                    "missing_evidence": ["镜像仓库可达性"],
                }
            )
        if "修复计划摘要" in prompt:
            return json.dumps(
                {
                    "summary": "优先检查镜像，再对低风险修复动作排序",
                    "priority_actions": ["patch_image_pull_policy"],
                }
            )
        if "Reviewer结论" in prompt:
            return json.dumps(
                {
                    "reason": "该动作变更镜像拉取策略，建议人工确认后执行",
                    "human_confirmation_note": "确认业务可接受临时回退策略",
                }
            )
        return "{}"


@pytest.mark.asyncio
async def test_workflow_runs_analyzer_planner_reviewer_executor_for_safe_action() -> None:
    engine = FakeRemediationEngine(
        {
            "fault_type": "image_pull_failure",
            "candidate_actions": [
                {
                    "action_id": "patch-image-pull-policy",
                    "action_type": "patch_image_pull_policy",
                    "risk_assessment": {"allowed": True, "risk_level": "low", "requires_human_confirmation": False},
                }
            ],
        }
    )
    workflow = OpsMultiAgentWorkflow(remediation_engine=engine)

    result = await workflow.run({"problem_description": "deployment api ImagePullBackOff", "deployment": "api", "namespace": "default"})

    assert result["status"] == "completed"
    assert result["next_action"] == "finish"
    assert result["agents_used"] == ["Coordinator", "Analyzer", "Planner", "Reviewer", "Executor"]
    assert engine.executed is True


@pytest.mark.asyncio
async def test_workflow_blocks_high_risk_plan_before_executor() -> None:
    engine = FakeRemediationEngine(
        {
            "fault_type": "crash_loop_backoff",
            "candidate_actions": [
                {
                    "action_id": "restart-deployment",
                    "action_type": "restart_deployment",
                    "risk_assessment": {"allowed": False, "risk_level": "high", "requires_human_confirmation": True},
                }
            ],
        }
    )
    workflow = OpsMultiAgentWorkflow(remediation_engine=engine)

    result = await workflow.run({"problem_description": "production api CrashLoopBackOff", "deployment": "api", "namespace": "prod"})

    assert result["status"] == "needs_human_confirmation"
    assert result["next_action"] == "human_confirm"
    assert "Executor" not in result["agents_used"]
    assert engine.executed is False


@pytest.mark.asyncio
async def test_workflow_uses_llm_to_enrich_analysis_and_review() -> None:
    engine = FakeRemediationEngine(
        {
            "fault_type": "image_pull_failure",
            "candidate_actions": [
                {
                    "action_id": "patch-image-pull-policy",
                    "action_type": "patch_image_pull_policy",
                    "risk_assessment": {
                        "allowed": False,
                        "risk_level": "high",
                        "requires_human_confirmation": True,
                    },
                }
            ],
        }
    )
    llm_client = FakeLLMClient()
    workflow = OpsMultiAgentWorkflow(remediation_engine=engine, llm_client=llm_client)

    result = await workflow.run(
        {
            "problem_description": "deployment api ImagePullBackOff，需要AI分析并给出修复建议",
            "deployment": "api",
            "namespace": "default",
            "event": "ImagePullBackOff failed to pull image",
        }
    )

    assert len(llm_client.calls) >= 3
    assert result["status"] == "needs_human_confirmation"
    assert result["diagnosis"]["ai_analysis"]["fault_type"] == "image_pull_failure"
    assert result["plan"]["ai_plan_summary"]["summary"] == "优先检查镜像，再对低风险修复动作排序"
    assert result["review"]["reason"] == "该动作变更镜像拉取策略，建议人工确认后执行"
