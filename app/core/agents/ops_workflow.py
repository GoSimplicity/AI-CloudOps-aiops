#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于LangGraph的多智能体运维协作工作流
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import logging
from typing import Any, Dict, List, Optional

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from app.core.interfaces.llm_client import LLMClient, NullLLMClient

logger = logging.getLogger("aiops.agents.ops_workflow")


class OpsWorkflowState(TypedDict, total=False):
    """LangGraph状态图中的共享状态。"""

    problem_description: str
    deployment: str
    namespace: str
    event: str
    diagnosis: Dict[str, Any]
    plan: Dict[str, Any]
    review: Dict[str, Any]
    execution: Dict[str, Any]
    status: str
    next_action: str
    agents_used: List[str]
    messages: List[Dict[str, Any]]
    timestamp: str


class OpsMultiAgentWorkflow:
    """多智能体协作工作流。

    Coordinator 负责识别任务与调度；Analyzer 规整实时诊断上下文；Planner 生成修复计划；
    Reviewer 执行风险门禁；Executor 只执行 Reviewer 放行的动作。
    """

    def __init__(
        self,
        remediation_engine: Any,
        llm_client: Optional[LLMClient] = None,
        max_iterations: int = 6,
    ) -> None:
        self.remediation_engine = remediation_engine
        self.llm_client: LLMClient = llm_client or NullLLMClient()
        self.max_iterations = max_iterations
        self.graph = self._build_graph()

    async def run(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """执行多智能体工作流并返回可审计结果。"""
        initial_state: OpsWorkflowState = {
            "problem_description": str(request.get("problem_description") or request.get("event") or ""),
            "deployment": str(request.get("deployment") or ""),
            "namespace": str(request.get("namespace") or "default"),
            "event": str(request.get("event") or request.get("problem_description") or ""),
            "diagnosis": deepcopy(request.get("diagnosis") or {}),
            "agents_used": [],
            "messages": [],
            "status": "running",
            "next_action": "coordinate",
            "timestamp": datetime.now().isoformat(),
        }
        result = await self.graph.ainvoke(initial_state)
        return self._finalize(result)

    def _build_graph(self):
        workflow = StateGraph(OpsWorkflowState)
        workflow.add_node("coordinator", self._coordinator)
        workflow.add_node("analyzer", self._analyzer)
        workflow.add_node("planner", self._planner)
        workflow.add_node("reviewer", self._reviewer)
        workflow.add_node("executor", self._executor)

        workflow.set_entry_point("coordinator")
        workflow.add_conditional_edges(
            "coordinator",
            self._route_from_coordinator,
            {
                "analyzer": "analyzer",
                "planner": "planner",
                "finish": END,
            },
        )
        workflow.add_edge("analyzer", "planner")
        workflow.add_edge("planner", "reviewer")
        workflow.add_conditional_edges(
            "reviewer",
            self._route_from_reviewer,
            {
                "executor": "executor",
                "analyzer": "analyzer",
                "human_confirm": END,
                "finish": END,
            },
        )
        workflow.add_edge("executor", END)
        return workflow.compile()

    async def _coordinator(self, state: OpsWorkflowState) -> OpsWorkflowState:
        state = self._copy_state(state)
        self._mark_agent(state, "Coordinator", "识别任务类型并选择下一步")

        if not state.get("problem_description"):
            state["status"] = "invalid"
            state["next_action"] = "finish"
            state["messages"].append({"agent": "Coordinator", "level": "error", "content": "缺少problem_description"})
            return state

        ai_context = await self._analyze_problem_context(state)
        if ai_context:
            suggested_approach = ai_context.get("suggested_approach")
            if suggested_approach:
                state["messages"].append(
                    {
                        "agent": "Coordinator",
                        "content": suggested_approach,
                        "level": "info",
                        "timestamp": datetime.now().isoformat(),
                    }
                )

        if not state.get("diagnosis"):
            state["next_action"] = "analyze"
        else:
            state["next_action"] = "plan"
        return state

    async def _analyzer(self, state: OpsWorkflowState) -> OpsWorkflowState:
        state = self._copy_state(state)
        self._mark_agent(state, "Analyzer", "分析指标、事件、日志和用户输入")

        diagnosis = deepcopy(state.get("diagnosis") or {})
        diagnosis.setdefault("deployment", state.get("deployment"))
        diagnosis.setdefault("namespace", state.get("namespace", "default"))
        if state.get("event"):
            diagnosis.setdefault("events", [{"reason": "UserProvidedEvent", "message": state["event"]}])
        diagnosis.setdefault("problem_description", state.get("problem_description", ""))
        ai_analysis = await self._generate_ai_analysis(diagnosis)
        if ai_analysis:
            diagnosis["ai_analysis"] = ai_analysis
            ai_note = self._compact_ai_note(
                ai_analysis,
                ["fault_type", "observations", "suspected_causes", "missing_evidence"],
            )
            if ai_note:
                state["messages"].append(
                    {
                        "agent": "Analyzer",
                        "content": ai_note,
                        "level": "info",
                        "timestamp": datetime.now().isoformat(),
                    }
                )

        state["diagnosis"] = {
            "deployment": state.get("deployment"),
            "namespace": state.get("namespace", "default"),
            "event": state.get("event"),
            "diagnosis": diagnosis,
        }
        if ai_analysis:
            state["diagnosis"]["ai_analysis"] = ai_analysis
        state["next_action"] = "plan"
        return state

    async def _planner(self, state: OpsWorkflowState) -> OpsWorkflowState:
        state = self._copy_state(state)
        self._mark_agent(state, "Planner", "生成候选修复动作和执行计划")
        plan = await self.remediation_engine.build_plan(state.get("diagnosis") or {})
        ai_plan_summary = await self._summarize_plan_with_ai(state, plan)
        if ai_plan_summary:
            plan["ai_plan_summary"] = ai_plan_summary
            ai_note = self._compact_ai_note(
                ai_plan_summary,
                ["summary", "priority_actions", "recommended_order"],
            )
            if ai_note:
                state["messages"].append(
                    {
                        "agent": "Planner",
                        "content": ai_note,
                        "level": "info",
                        "timestamp": datetime.now().isoformat(),
                    }
                )
        state["plan"] = plan
        state["next_action"] = "review"
        return state

    async def _reviewer(self, state: OpsWorkflowState) -> OpsWorkflowState:
        state = self._copy_state(state)
        self._mark_agent(state, "Reviewer", "检查风险评估和自动执行边界")

        plan = state.get("plan") or {}
        executable_actions = [action for action in plan.get("candidate_actions", []) if action.get("executable", True)]
        blocked_actions = [action for action in executable_actions if not action.get("risk_assessment", {}).get("allowed")]
        allowed_actions = [action for action in executable_actions if action.get("risk_assessment", {}).get("allowed")]
        ai_review = await self._review_with_ai(state, blocked_actions, allowed_actions)

        review_reason = self._extract_review_reason(
            ai_review,
            "存在高风险或未授权动作，需要人工确认",
        )
        if blocked_actions:
            review = {
                "approved": False,
                "reason": review_reason,
                "blocked_actions": blocked_actions,
                "allowed_actions": allowed_actions,
            }
            if ai_review:
                review["ai_review"] = ai_review
            state["status"] = "needs_human_confirmation"
            state["next_action"] = "human_confirm"
        elif allowed_actions:
            review = {
                "approved": True,
                "reason": self._extract_review_reason(
                    ai_review,
                    "候选动作风险可控，允许自动执行",
                ),
                "blocked_actions": [],
                "allowed_actions": allowed_actions,
            }
            if ai_review:
                review["ai_review"] = ai_review
            state["next_action"] = "execute"
        else:
            review = {
                "approved": False,
                "reason": self._extract_review_reason(
                    ai_review,
                    "没有可执行动作，保留为人工排查建议",
                ),
                "blocked_actions": [],
                "allowed_actions": [],
            }
            if ai_review:
                review["ai_review"] = ai_review
            state["status"] = "planned"
            state["next_action"] = "finish"

        if ai_review:
            ai_note = self._compact_ai_note(
                ai_review,
                ["reason", "human_confirmation_note", "risk_summary"],
            )
            if ai_note:
                state["messages"].append(
                    {
                        "agent": "Reviewer",
                        "content": ai_note,
                        "level": "info",
                        "timestamp": datetime.now().isoformat(),
                    }
                )

        state["review"] = review
        return state

    async def _analyze_problem_context(self, state: OpsWorkflowState) -> Dict[str, Any]:
        if isinstance(self.llm_client, NullLLMClient):
            return {}
        prompt = (
            "分析以下 Kubernetes 故障上下文，返回 JSON。"
            "只允许使用提供的字段，不要虚构 cluster、pod、node、timestamp。"
            "如果缺失就返回 null 或空数组。"
            "输出字段: problem_type, components, severity, suggested_approach, required_agents。\n"
            f"问题描述：{state.get('problem_description', '')}\n"
            f"Deployment：{state.get('deployment', '')}\n"
            f"Namespace：{state.get('namespace', '')}\n"
        )
        return await self._parse_llm_json(
            [{"role": "user", "content": prompt}],
            use_task_model=False,
        )

    async def _generate_ai_analysis(self, diagnosis: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(self.llm_client, NullLLMClient):
            return {}
        payload = {
            "deployment": diagnosis.get("deployment"),
            "namespace": diagnosis.get("namespace"),
            "problem_description": diagnosis.get("problem_description"),
            "events": diagnosis.get("events", []),
        }
        prompt = (
            "请基于以下上下文输出结构化Kubernetes故障分析(JSON)。"
            "只能基于给定输入，不要虚构示例集群、节点、Pod、时间戳。"
            "必须尽量包含 fault_type, analysis, root_cause, observations, suspected_causes, suggested_actions, missing_evidence 字段。\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        response = await self._parse_llm_json(
            [{"role": "user", "content": prompt}],
            use_task_model=False,
        )
        return self._normalize_ai_analysis(response, diagnosis)

    async def _summarize_plan_with_ai(
        self, state: OpsWorkflowState, plan: Dict[str, Any]
    ) -> Dict[str, Any]:
        if isinstance(self.llm_client, NullLLMClient):
            return {}
        prompt = (
            "请根据下面的候选动作生成修复计划摘要(JSON)。"
            "只能总结输入里的候选动作，不要原样回传整个计划对象。"
            "必须尽量包含 summary, priority_actions, recommended_order, risk_focus 字段。\n"
            "修复计划摘要\n"
            f"{json.dumps(plan, ensure_ascii=False)}"
        )
        response = await self._parse_llm_json(
            [{"role": "user", "content": prompt}],
            use_task_model=True,
        )
        return self._normalize_ai_plan_summary(response, plan)

    async def _review_with_ai(
        self,
        state: OpsWorkflowState,
        blocked_actions: List[Dict[str, Any]],
        allowed_actions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if isinstance(self.llm_client, NullLLMClient):
            return {}
        payload = {
            "problem_description": state.get("problem_description", ""),
            "blocked_actions": blocked_actions,
            "allowed_actions": allowed_actions,
        }
        prompt = (
            "请给出Reviewer结论(JSON)，说明是否建议人工确认。"
            "只输出结论，不要原样复述整个输入对象。"
            "必须尽量包含 reason, human_confirmation_note, risk_summary 字段。\n"
            "Reviewer结论\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        response = await self._parse_llm_json(
            [{"role": "user", "content": prompt}],
            use_task_model=True,
        )
        return self._normalize_ai_review(response, blocked_actions, allowed_actions)

    async def _parse_llm_json(
        self, messages: List[Dict[str, str]], use_task_model: bool
    ) -> Dict[str, Any]:
        response = await self.llm_client.generate_response(
            messages,
            response_format={"type": "json_object"},
            use_task_model=use_task_model,
        )
        if not response:
            return {}
        if isinstance(response, dict):
            return response
        text = str(response).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if "```json" in text:
                json_part = text.split("```json", 1)[1].split("```", 1)[0].strip()
                try:
                    return json.loads(json_part)
                except json.JSONDecodeError:
                    return {"raw_text": text}
        return {"raw_text": text}

    def _compact_ai_note(self, payload: Dict[str, Any], keys: List[str]) -> str:
        parts: List[str] = []
        for key in keys:
            value = payload.get(key)
            if not value:
                continue
            if isinstance(value, list):
                text = " / ".join(str(item) for item in value[:3])
            else:
                text = str(value)
            if text:
                parts.append(text)
        if not parts and payload.get("raw_text"):
            parts.append(str(payload["raw_text"]))
        return " | ".join(parts)

    def _extract_review_reason(
        self, ai_review: Dict[str, Any], fallback: str
    ) -> str:
        if not ai_review:
            return fallback
        reviewer_conclusion = ai_review.get("reviewer_conclusion") or {}
        return (
            ai_review.get("reason")
            or reviewer_conclusion.get("reason")
            or reviewer_conclusion.get("risk_summary")
            or reviewer_conclusion.get("analysis")
            or ai_review.get("raw_text")
            or fallback
        )

    def _normalize_ai_analysis(
        self, ai_analysis: Dict[str, Any], diagnosis: Dict[str, Any]
    ) -> Dict[str, Any]:
        if not ai_analysis:
            return {}

        source = ai_analysis.get("faultAnalysis") or ai_analysis
        event_messages = [
            str(item.get("message") or "")
            for item in (diagnosis.get("events") or [])
            if isinstance(item, dict)
        ]
        diagnosis_text = " ".join(
            [str(diagnosis.get("problem_description") or ""), *event_messages]
        ).lower()
        inferred_fault_type = "unknown"
        if "imagepull" in diagnosis_text or "pull image" in diagnosis_text:
            inferred_fault_type = "image_pull_failure"
        elif "crashloop" in diagnosis_text:
            inferred_fault_type = "crash_loop_backoff"
        elif "insufficient" in diagnosis_text or "oom" in diagnosis_text:
            inferred_fault_type = "resource_insufficient"

        normalized = {
            "fault_type": source.get("fault_type")
            or source.get("faultType")
            or inferred_fault_type,
            "analysis": source.get("analysis") or source.get("summary") or source.get("rootCause"),
            "root_cause": source.get("root_cause") or source.get("rootCause"),
            "observations": source.get("observations") or source.get("symptoms") or [],
            "suspected_causes": source.get("suspected_causes") or source.get("suspectedCauses") or [],
            "suggested_actions": source.get("suggested_actions") or source.get("suggestedActions") or [],
            "missing_evidence": source.get("missing_evidence") or source.get("missingEvidence") or [],
            "raw_text": ai_analysis.get("raw_text"),
        }
        return {key: value for key, value in normalized.items() if value}

    def _normalize_ai_plan_summary(
        self, ai_plan_summary: Dict[str, Any], plan: Dict[str, Any]
    ) -> Dict[str, Any]:
        if not ai_plan_summary:
            return {}

        summary = ai_plan_summary.get("summary")
        if isinstance(summary, dict):
            summary = None
        priority_actions = ai_plan_summary.get("priority_actions") or ai_plan_summary.get(
            "recommended_order"
        )
        if not priority_actions:
            priority_actions = [
                action.get("action_type")
                for action in plan.get("allowed_actions", []) + plan.get("blocked_actions", [])
                if action.get("action_type")
            ]
        normalized = {
            "summary": summary
            or f"候选 {plan.get('summary', {}).get('total_actions', 0)} 个动作，"
            f"阻断 {plan.get('summary', {}).get('blocked_actions', 0)} 个动作",
            "priority_actions": priority_actions[:3] if isinstance(priority_actions, list) else priority_actions,
            "recommended_order": ai_plan_summary.get("recommended_order") or [],
            "risk_focus": ai_plan_summary.get("risk_focus"),
            "raw_text": ai_plan_summary.get("raw_text"),
        }
        return {key: value for key, value in normalized.items() if value}

    def _normalize_ai_review(
        self,
        ai_review: Dict[str, Any],
        blocked_actions: List[Dict[str, Any]],
        allowed_actions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not ai_review:
            return {}

        source = ai_review.get("reviewer_conclusion") or ai_review
        blocked_action_types = [
            action.get("action_type") for action in blocked_actions if action.get("action_type")
        ]
        normalized = {
            "reason": ai_review.get("reason")
            or source.get("reason")
            or source.get("risk_summary"),
            "human_confirmation_note": ai_review.get("human_confirmation_note")
            or source.get("human_confirmation_note"),
            "risk_summary": ai_review.get("risk_summary")
            or source.get("risk_summary")
            or ("需要人工确认动作: " + ", ".join(blocked_action_types) if blocked_action_types else None),
            "approved_action_types": [
                action.get("action_type")
                for action in allowed_actions
                if action.get("action_type")
            ],
            "blocked_action_types": blocked_action_types,
            "raw_text": ai_review.get("raw_text"),
        }
        return {key: value for key, value in normalized.items() if value}

    async def _executor(self, state: OpsWorkflowState) -> OpsWorkflowState:
        state = self._copy_state(state)
        self._mark_agent(state, "Executor", "执行允许范围内的工具操作")
        execution = await self.remediation_engine.execute_plan(state.get("plan") or {})
        state["execution"] = execution
        state["status"] = execution.get("status", "completed")
        state["next_action"] = "finish"
        return state

    def _route_from_coordinator(self, state: OpsWorkflowState) -> str:
        next_action = state.get("next_action")
        if next_action == "analyze":
            return "analyzer"
        if next_action == "plan":
            return "planner"
        return "finish"

    def _route_from_reviewer(self, state: OpsWorkflowState) -> str:
        next_action = state.get("next_action")
        if next_action == "execute":
            return "executor"
        if next_action == "analyze":
            return "analyzer"
        if next_action == "human_confirm":
            return "human_confirm"
        return "finish"

    def _copy_state(self, state: OpsWorkflowState) -> OpsWorkflowState:
        copied = deepcopy(dict(state))
        copied.setdefault("agents_used", [])
        copied.setdefault("messages", [])
        return copied

    def _mark_agent(self, state: OpsWorkflowState, agent: str, action: str) -> None:
        if agent not in state["agents_used"]:
            state["agents_used"].append(agent)
        state["messages"].append(
            {
                "agent": agent,
                "action": action,
                "timestamp": datetime.now().isoformat(),
            }
        )

    def _finalize(self, state: Dict[str, Any]) -> Dict[str, Any]:
        plan = state.get("plan") or {}
        review = state.get("review") or {}
        execution = state.get("execution") or {}
        status = state.get("status") or execution.get("status") or "completed"
        return {
            "status": status,
            "next_action": state.get("next_action", "finish"),
            "agents_used": state.get("agents_used", []),
            "diagnosis": state.get("diagnosis", {}),
            "plan": plan,
            "review": review,
            "execution": execution,
            "candidate_actions": plan.get("candidate_actions", []),
            "executed_actions": execution.get("executed_actions", []),
            "blocked_actions": review.get("blocked_actions") or execution.get("blocked_actions", []),
            "messages": state.get("messages", []),
            "timestamp": datetime.now().isoformat(),
        }
