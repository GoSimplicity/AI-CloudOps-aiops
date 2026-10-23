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

    async def execute_plan(self, plan, **kwargs):
        self.executed = True
        self.trigger = kwargs.get("trigger")
        return {"status": "completed", "executed_actions": plan["candidate_actions"], "blocked_actions": [], "post_check": {"ready": True}}

    def namespace_refusal(self, namespace, actions):
        return None


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
def safe_plan(live_evidence: bool = True, action_type: str = "patch_image_pull_policy"):
    fault_type = "crash_loop_backoff" if action_type == "restart_deployment" else "image_pull_failure"
    risk = {"allowed": True, "risk_level": "low", "requires_human_confirmation": False}
    if live_evidence:
        # SEC-44：自动执行需要与故障类型匹配的实时证据（替身给出的假值）
        risk["live_evidence"] = {"source": "live", "fault_type": fault_type, "checked": True, "matched": True}
    return {
        "fault_type": fault_type,
        "candidate_actions": [
            {
                "action_id": action_type.replace("_", "-"),
                "action_type": action_type,
                "risk_assessment": risk,
            }
        ],
    }


@pytest.mark.asyncio
async def test_workflow_default_only_plans_even_low_risk_action() -> None:
    engine = FakeRemediationEngine(safe_plan())
    workflow = OpsMultiAgentWorkflow(remediation_engine=engine)

    result = await workflow.run({"problem_description": "deployment api ImagePullBackOff", "deployment": "api", "namespace": "default"})

    assert result["status"] == "needs_human_confirmation"
    assert result["next_action"] == "human_confirm"
    assert "Executor" not in result["agents_used"]
    assert engine.executed is False
    assert result["review"]["confirmation_required"] is True
    assert [action["action_id"] for action in result["blocked_actions"]] == ["patch-image-pull-policy"]
    assert result["plan"]["allowed_actions"] == []
    assert result["plan"]["candidate_actions"][0]["risk_assessment"]["allowed"] is False


@pytest.mark.asyncio
async def test_workflow_legacy_auto_execute_switch_runs_executor_for_safe_action() -> None:
    engine = FakeRemediationEngine(safe_plan(action_type="restart_deployment"))
    workflow = OpsMultiAgentWorkflow(remediation_engine=engine, auto_execute=True)

    result = await workflow.run({"problem_description": "deployment api CrashLoopBackOff", "deployment": "api", "namespace": "default"})

    assert result["status"] == "completed"
    assert result["next_action"] == "finish"
    assert result["agents_used"] == ["Coordinator", "Analyzer", "Planner", "Reviewer", "Executor"]
    assert engine.executed is True
    assert engine.trigger == "auto"


@pytest.mark.asyncio
async def test_workflow_auto_execute_never_runs_image_patch() -> None:
    """SEC-44 修正：image 补丁不在免确认白名单，auto 开启 + 证据匹配也不进 Executor"""
    engine = FakeRemediationEngine(safe_plan())
    workflow = OpsMultiAgentWorkflow(remediation_engine=engine, auto_execute=True)

    result = await workflow.run({"problem_description": "deployment api ImagePullBackOff", "deployment": "api", "namespace": "default"})

    assert result["status"] == "needs_human_confirmation"
    assert "Executor" not in result["agents_used"]
    assert engine.executed is False


@pytest.mark.asyncio
async def test_workflow_auto_execute_without_live_evidence_only_plans() -> None:
    """SEC-44：auto_execute=True 但计划没有实时证据（只有诊断文字）→ 不进 Executor，转人工确认"""
    engine = FakeRemediationEngine(safe_plan(live_evidence=False))
    workflow = OpsMultiAgentWorkflow(remediation_engine=engine, auto_execute=True)

    result = await workflow.run({"problem_description": "deployment api ImagePullBackOff", "deployment": "api", "namespace": "default", "plan_only": False}, plan_only=False)

    assert result["status"] == "needs_human_confirmation"
    assert "Executor" not in result["agents_used"]
    assert engine.executed is False
    assert result["review"]["confirmation_required"] is True


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
