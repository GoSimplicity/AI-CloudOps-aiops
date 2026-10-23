#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps智能自动修复服务
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
import logging
from typing import Any, Dict, List, Optional

from ..common.constants import ServiceConstants
from ..common.exceptions import AutoFixError, FeatureDisabledError, ValidationError, WorkflowApprovalError
from ..common.identity import require_principal
from ..config.settings import config
from ..core.agents.k8s_fixer import K8sFixerAgent
from ..core.agents.ops_workflow import OpsMultiAgentWorkflow
from ..core.agents.supervisor import SupervisorAgent
from ..core.autofix import AutoFixRemediationEngine
from ..core.autofix.policy import (  # noqa: F401  ensure_repair_enabled / REPAIR_DISABLED_MESSAGE 保持从本模块导出
    REPAIR_DISABLED_MESSAGE,
    build_namespace_policy,
    ensure_namespace_allowed,
    ensure_repair_enabled,
)
from ..core.interfaces.k8s_client import K8sClient
from ..core.interfaces.llm_client import NullLLMClient
from .base import BaseService
from .workflow_approvals import CONFIRM_ENDPOINT, WorkflowApproval, WorkflowApprovals

logger = logging.getLogger("aiops.services.autofix")

BOOTSTRAP_DISABLED_MESSAGE = "测试资源引导入口 /autofix/bootstrap 已关闭（autofix.bootstrap_enabled / AUTOFIX_BOOTSTRAP_ENABLED=false）"
# /autofix/bootstrap 创建故障演示资源的目标命名空间
BOOTSTRAP_NAMESPACE = "test"


def ensure_bootstrap_enabled() -> None:
    if not config.autofix.bootstrap_enabled:
        raise FeatureDisabledError(BOOTSTRAP_DISABLED_MESSAGE, switch="autofix.bootstrap_enabled")


def ensure_bootstrap_namespace_allowed() -> None:
    """bootstrap 开启后仍要求 test 命名空间在 allowed_namespaces 内且非受限命名空间。

    复用 run/confirm 的同一判定（AutoFixRemediationEngine._namespace_allowed + 相同配置注入），
    只构造策略对象、不传 k8s 客户端，因此拒绝时不会初始化 Kubernetes 或创建任何资源。
    """
    ensure_namespace_allowed(
        BOOTSTRAP_NAMESPACE,
        subject="测试资源引导入口 /autofix/bootstrap 已开启",
        outcome="已拒绝创建测试资源",
    )


class AutoFixService(BaseService):
    """自动修复服务"""

    def __init__(self) -> None:
        super().__init__("autofix")
        self._k8s_fixer: Optional[K8sFixerAgent] = None
        self._supervisor: Optional[SupervisorAgent] = None
        self._remediation_engine: Optional[AutoFixRemediationEngine] = None
        self._ops_workflow: Optional[OpsMultiAgentWorkflow] = None
        self._approvals = WorkflowApprovals()
        self._closing = False
        self._active_operations: dict[asyncio.Task[Any], str] = {}

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        owner = require_principal()
        if self._closing:
            raise WorkflowApprovalError(503, "自动修复服务已关闭")
        task = asyncio.current_task()
        nested = task in self._active_operations
        if not nested:
            if len(self._active_operations) >= 64 or sum(value == owner for value in self._active_operations.values()) >= 4:
                raise WorkflowApprovalError(429, "自动修复正在处理较多任务，请稍后重试")
            if task is not None:
                self._active_operations[task] = owner
        try:
            yield
        finally:
            if not nested and task is not None:
                self._active_operations.pop(task, None)

    @staticmethod
    def _build_remediation_engine(k8s_client: Any) -> AutoFixRemediationEngine:
        """按 autofix 配置构造执行引擎：白名单为空即 fail-closed，默认不自动执行"""
        return build_namespace_policy(k8s_client)

    async def initialize(self) -> None:
        if self._closing:
            raise WorkflowApprovalError(503, "自动修复服务已关闭")
        await super().initialize()

    async def _do_initialize(self) -> None:
        if self._closing:
            raise WorkflowApprovalError(503, "自动修复服务已关闭")
        try:
            # 初始化K8s修复代理（注入K8s和LLM依赖）
            from .kubernetes import KubernetesService
            from .llm import LLMService

            k8s_client: K8sClient = await asyncio.to_thread(KubernetesService)
            llm_client = LLMService()
            self._k8s_fixer = K8sFixerAgent(
                llm_client=llm_client, k8s_client=k8s_client
            )
            self.logger.info("K8s修复代理初始化完成")

            # 初始化监督代理（注入LLM）
            self._supervisor = SupervisorAgent(llm_client=llm_client)
            self.logger.info("监督代理初始化完成")

            self._remediation_engine = self._build_remediation_engine(k8s_client)
            self._ops_workflow = OpsMultiAgentWorkflow(
                remediation_engine=self._remediation_engine,
                llm_client=llm_client,
                auto_execute=bool(config.autofix.auto_execute_enabled),
            )
            self.logger.info("自动修复执行引擎和多智能体工作流初始化完成")

        except Exception as e:
            self.logger.exception(f"自动修复服务组件初始化失败, 错误类型: {type(e).__name__}")
            raise AutoFixError("初始化失败") from e

    async def _do_health_check(self) -> bool:
        try:
            if not self._k8s_fixer or not self._supervisor:
                return False

            # 检查K8s连接状态
            k8s_healthy = await self.execute_with_timeout(
                self._k8s_fixer.k8s_service.health_check,
                timeout=ServiceConstants.AUTOFIX_K8S_TIMEOUT,
                operation_name="k8s_health_check",
            )

            return bool(k8s_healthy)

        except Exception as e:
            # 承接 SEC-26 P3-3：warning 只记异常类型，完整内容放 debug
            self.logger.warning("自动修复服务健康检查失败，异常类型: %s", type(e).__name__)
            self.logger.debug("自动修复服务健康检查失败详情: %s", e, exc_info=True)
            return False

    async def execute_multi_agent_workflow(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """在可信身份、容量与生命周期边界内执行工作流"""
        async with self._operation():
            return await self._execute_owned_workflow(request)

    async def _execute_owned_workflow(self, request: Dict[str, Any]) -> Dict[str, Any]:
        require_principal()
        if not isinstance(request, dict) or not isinstance(request.get("problem_description"), str) or not request["problem_description"].strip():
            raise ValidationError("problem_description", "问题描述不能为空")
        reservation = self._approvals.reserve()
        try:
            await self.initialize()
        except BaseException:
            self._approvals.discard(reservation)
            raise
        try:
            # 工作流内部可能执行低风险动作，异常后不能假定没有副作用
            reservation.effects_started = True
            result = await self._run_multi_agent_workflow(request)
            if result.get("status") == "failed" or (result.get("execution") or {}).get("status") == "failed":
                raise WorkflowApprovalError(409, "工作流执行失败，可能已产生副作用，禁止盲目重试，请人工核对")
            self._approvals.publish(reservation, result)
            return result
        except asyncio.CancelledError:
            self._approvals.fail(reservation, "工作流已中断，可能已产生副作用，不能盲目重试，请人工核对")
            raise
        except Exception as error:
            reason = "工作流结果不确定，可能已产生副作用，禁止盲目重试，请人工核对"
            self._approvals.fail(reservation, reason)
            raise WorkflowApprovalError(409, reason) from error

    async def _run_multi_agent_workflow(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """执行基于LangGraph的多智能体自动修复工作流。"""
        self._ensure_initialized()

        problem_description = (request or {}).get("problem_description")
        if not problem_description or not isinstance(problem_description, str):
            raise ValidationError("problem_description", "问题描述不能为空")

        if self._ops_workflow is None:
            if self._remediation_engine is None:
                if not self._k8s_fixer:
                    raise AutoFixError("自动修复执行引擎未初始化")
                self._remediation_engine = self._build_remediation_engine(self._k8s_fixer.k8s_service)
            llm_client = (
                self._k8s_fixer.llm_service
                if self._k8s_fixer and getattr(self._k8s_fixer, "llm_service", None)
                else NullLLMClient()
            )
            self._ops_workflow = OpsMultiAgentWorkflow(
                remediation_engine=self._remediation_engine,
                llm_client=llm_client,
                auto_execute=bool(config.autofix.auto_execute_enabled),
            )

        workflow_input = {
            "problem_description": problem_description,
            "deployment": request.get("deployment", ""),
            "namespace": request.get("namespace", "default"),
            "event": request.get("event", problem_description),
            "diagnosis": request.get("diagnosis", {}),
        }
        result = await self._ops_workflow.run(workflow_input)
        result["workflow_engine"] = "langgraph"
        result["workflow_nodes"] = [
            "Coordinator",
            "Analyzer",
            "Planner",
            "Reviewer",
            "Executor",
        ]
        return result

    async def confirm_multi_agent_workflow(self, plan_id: str, approved_action_ids: List[str]) -> Dict[str, Any]:
        """原子领取当前主体的原计划动作后再执行"""
        async with self._operation():
            return await self._confirm_owned_workflow(plan_id, approved_action_ids)

    async def _confirm_owned_workflow(self, plan_id: str, approved_action_ids: List[str]) -> Dict[str, Any]:
        require_principal()
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ValidationError("plan_id", "计划ID不能为空")
        record = self._approvals.claim(plan_id.strip(), approved_action_ids)
        normalized_ids = [action_id.strip() for action_id in approved_action_ids]
        try:
            await self.initialize()
            result = await self._confirm_claimed_workflow(record, normalized_ids)
            if (result.get("execution") or {}).get("status") != "completed":
                raise WorkflowApprovalError(409, "动作执行未全部确认成功，可能已产生副作用，禁止重试同一计划，请人工核对")
            self._approvals.publish(record, result)
            return result
        except asyncio.CancelledError:
            self._approvals.fail(record, "审批执行已中断，可能已产生副作用，不能重试同一计划，请人工核对")
            raise
        except Exception as error:
            if record.effects_started:
                reason = "审批执行结果不确定，可能已产生副作用，禁止重试同一计划，请人工核对"
            else:
                reason = "审批已失效，尚未执行确认动作，请重新生成计划，不能重试原审批"
            self._approvals.fail(record, reason)
            if isinstance(error, WorkflowApprovalError):
                raise
            raise WorkflowApprovalError(409, reason) from error

    @staticmethod
    def _namespace_precheck_refusal(namespace: Any, actions: List[Dict[str, Any]], *engines: Any) -> bool:
        """对每个（去重后的）引擎调用公开的 namespace_refusal；缺少该方法或预检异常一律按拒绝（fail-closed）"""
        seen = set()
        for engine in engines:
            if id(engine) in seen:
                continue
            seen.add(id(engine))
            refusal = getattr(engine, "namespace_refusal", None)
            if not callable(refusal):
                logger.warning("执行引擎 %s 未实现 namespace_refusal，按拒绝处理", type(engine).__name__)
                return True
            try:
                if refusal(namespace, actions):
                    return True
            except Exception as e:
                logger.warning("命名空间预检异常，按拒绝处理，异常类型: %s", type(e).__name__)
                return True
        return False

    async def _confirm_claimed_workflow(self, record: WorkflowApproval, normalized_action_ids: List[str]) -> Dict[str, Any]:
        normalized_plan_id = record.plan_id
        workflow_result = deepcopy(record.result or {})
        if self._remediation_engine is None:
            if not self._k8s_fixer:
                raise AutoFixError("自动修复执行引擎未初始化")
            self._remediation_engine = self._build_remediation_engine(self._k8s_fixer.k8s_service)

        diagnosis = deepcopy(workflow_result.get("diagnosis") or {})
        if not diagnosis:
            raise ValidationError("plan_id", "工作流结果缺少诊断上下文")

        shared_engine = self._remediation_engine
        # 承接 SEC-26 P3-7：确认时按当前配置在局部重新构建策略（allowed_namespaces + auto_execute + 受限命名空间），
        # 不修改共享单例；计划生成后被收紧的命名空间在重新校验中即被拒绝（403）。
        # 注意：配置本身只在启动时加载，改白名单需重启（重启会清掉待审批项），见 docs/autofix_guide.md。
        engine = shared_engine
        if isinstance(shared_engine, AutoFixRemediationEngine):
            engine = shared_engine.with_policy(build_namespace_policy())
        current_plan = await engine.build_plan(diagnosis)
        plan = deepcopy(workflow_result.get("plan") or {})
        if any(current_plan.get(key) != plan.get(key) for key in ("namespace", "deployment")):
            raise WorkflowApprovalError(409, "重新校验的目标与原计划不一致，请重新生成计划")
        plan["plan_id"] = normalized_plan_id
        current_by_id = {action.get("action_id"): action for action in current_plan.get("candidate_actions", [])}
        original_by_id = {action.get("action_id"): action for action in plan.get("candidate_actions", [])}
        for action_id in normalized_action_ids:
            current = current_by_id.get(action_id)
            original = original_by_id.get(action_id)
            if current is None or original is None or any(current.get(key) != original.get(key) for key in ("action_id", "action_type", "target", "parameters", "executable", "rollback_available")):
                raise WorkflowApprovalError(409, "原计划动作或目标参数已变化，请重新生成计划")
            risk = current.get("risk_assessment") or {}
            if any(risk.get(key) is not True for key in ("namespace_allowed", "permission_allowed")):
                # SEC-26 P3-2：确认时重新校验未通过属于授权/策略拒绝，返回 403（审批不能覆盖）
                raise WorkflowApprovalError(403, "当前权限或命名空间限制未通过，审批不能覆盖，请重新诊断")
            if risk.get("resource_exists") is not True:
                # 承接 SEC-26 P3-6：目标资源已不存在属于状态冲突，返回 409（尚未执行，需重新诊断）
                raise WorkflowApprovalError(409, "目标资源当前不存在，审批不能覆盖，尚未执行，请重新诊断")

        review = deepcopy(workflow_result.get("review") or {})
        previous_executed_actions = deepcopy(workflow_result.get("executed_actions") or [])
        previous_approved_action_ids = [
            str(action_id)
            for action_id in (review.get("approved_action_ids") or [])
            if action_id
        ]
        previous_executed_action_ids = {
            str(action.get("action_id"))
            for action in previous_executed_actions
            if action.get("action_id")
        }
        previous_executed_action_ids.update(previous_approved_action_ids)
        blocked_actions = [
            deepcopy(action)
            for action in (plan.get("blocked_actions") or [])
            if str(action.get("action_id") or "") not in previous_executed_action_ids
        ]
        allowed_actions = []
        candidate_actions = deepcopy(plan.get("candidate_actions") or [])

        blocked_by_id = {
            action.get("action_id"): action
            for action in blocked_actions
            if action.get("action_id")
        }
        invalid_action_ids = [
            action_id for action_id in normalized_action_ids if action_id not in blocked_by_id
        ]
        if invalid_action_ids:
            raise ValidationError(
                "approved_action_ids",
                "选择了不属于原计划或已经确认的动作",
            )

        approved_actions = []
        remaining_blocked_actions = []
        for action in blocked_actions:
            action_id = action.get("action_id")
            if action_id in normalized_action_ids:
                approved_action = deepcopy(action)
                risk = deepcopy(approved_action.get("risk_assessment") or {})
                reasons = list(risk.get("reasons") or [])
                if "human confirmed" not in reasons:
                    reasons.append("human confirmed")
                risk["allowed"] = True
                risk["requires_human_confirmation"] = False
                risk["reasons"] = reasons
                approved_action["risk_assessment"] = risk
                approved_actions.append(approved_action)
            else:
                remaining_blocked_actions.append(deepcopy(action))

        for action in candidate_actions:
            action_id = action.get("action_id")
            if action_id in normalized_action_ids or action_id in previous_executed_action_ids:
                risk = deepcopy(action.get("risk_assessment") or {})
                reasons = list(risk.get("reasons") or [])
                marker = (
                    "human confirmed"
                    if action_id in normalized_action_ids
                    else "previously confirmed"
                )
                if marker not in reasons:
                    reasons.append(marker)
                risk["allowed"] = True
                risk["requires_human_confirmation"] = False
                risk["reasons"] = reasons
                action["risk_assessment"] = risk

        plan["candidate_actions"] = candidate_actions
        plan["allowed_actions"] = allowed_actions + approved_actions
        plan["blocked_actions"] = remaining_blocked_actions

        # SEC-26 第一层审查附加条件 P3-1：置 effects_started 前先做执行引擎同一道命名空间硬校验；
        # 被拒绝时尚未调用任何执行接口，审批记录应进入 invalidated（而非 uncertain）。
        # 预检与下面 execute_plan 内的校验之间没有 await，读到的是同一份白名单。
        # 所有引擎（不只是 AutoFixRemediationEngine）都必须实现 RemediationEngine.namespace_refusal 并经过预检；
        # 局部策略副本与共享单例都要放行才执行（任一拒绝即拒绝）。
        if self._namespace_precheck_refusal(plan.get("namespace", "default"), plan["allowed_actions"], engine, shared_engine):
            raise WorkflowApprovalError(403, "执行前重新校验命名空间限制未通过，未执行任何动作，请重新诊断")

        record.effects_started = True
        execution = await engine.execute_plan(plan)
        current_executed_actions = execution.get("executed_actions", [])
        executed_ids = [action.get("action_id") for action in current_executed_actions]
        if execution.get("status") == "blocked" and not current_executed_actions:
            # SEC-26 P3-2：执行前重新校验（命名空间白名单/受限命名空间）未通过，未执行任何动作 → 403
            not_attempted_ids = {action.get("action_id") for action in execution.get("not_attempted_actions") or []}
            if execution.get("post_check") is None and set(normalized_action_ids) <= not_attempted_ids:
                # 执行器在任何写操作之前拒绝（无已执行动作、无事后检查、选中动作全部未尝试）：
                # 可确认尚未产生副作用，回退 effects_started，使审批记录进入 invalidated
                record.effects_started = False
            raise WorkflowApprovalError(403, "执行前重新校验命名空间限制未通过，未执行任何动作，请重新诊断")
        if set(executed_ids) != set(normalized_action_ids) or len(executed_ids) != len(normalized_action_ids) or any(action.get("status") != "succeeded" for action in current_executed_actions):
            # 已进入执行且未全部成功：可能已有副作用，保持 409（冲突/需人工核对），不归为授权拒绝
            raise WorkflowApprovalError(409, "执行记录未确认所有选中动作成功，可能已产生副作用，不能重试原计划，请人工核对")
        cumulative_executed_actions = previous_executed_actions + current_executed_actions
        execution_payload = deepcopy(execution)
        execution_payload["executed_actions"] = cumulative_executed_actions
        execution_payload["blocked_actions"] = remaining_blocked_actions

        agents_used = list(workflow_result.get("agents_used") or [])
        for agent in ["HumanConfirm", "Executor"]:
            if agent not in agents_used:
                agents_used.append(agent)

        messages = list(workflow_result.get("messages") or [])
        messages.append(
            {
                "agent": "HumanConfirm",
                "action": f"人工批准 {len(approved_actions)} 个原计划动作继续执行",
                "timestamp": datetime.now().isoformat(),
            }
        )
        messages.append(
            {
                "agent": "Executor",
                "action": "执行人工确认后放行的动作",
                "timestamp": datetime.now().isoformat(),
            }
        )

        remaining_blocked_count = len(remaining_blocked_actions)
        executed_count = len(current_executed_actions)
        if remaining_blocked_count > 0:
            review_reason = (
                f"已执行 {executed_count} 个经人工确认动作，仍有 "
                f"{remaining_blocked_count} 个动作待确认"
            )
            status = "needs_human_confirmation"
            next_action = "human_confirm"
            review_approved = False
        else:
            review_reason = f"已执行 {executed_count} 个经人工确认动作"
            status = execution.get("status", "completed")
            next_action = "finish"
            review_approved = True

        review.update(
            {
                "approved": review_approved,
                "reason": review_reason,
                "approved_action_ids": list(
                    dict.fromkeys(previous_approved_action_ids + normalized_action_ids)
                ),
                "blocked_actions": remaining_blocked_actions,
                "allowed_actions": plan["allowed_actions"],
            }
        )

        response = {
            "status": status,
            "next_action": next_action,
            "agents_used": agents_used,
            "diagnosis": diagnosis,
            "plan": plan,
            "review": review,
            "execution": execution_payload,
            "candidate_actions": plan.get("candidate_actions", []),
            "executed_actions": cumulative_executed_actions,
            "blocked_actions": remaining_blocked_actions,
            "messages": messages,
            "timestamp": datetime.now().isoformat(),
            "workflow_engine": "langgraph",
            "workflow_nodes": [
                "Coordinator",
                "Analyzer",
                "Planner",
                "Reviewer",
                "HumanConfirm",
                "Executor",
            ],
        }
        return response

    async def fix_kubernetes_deployment(
        self,
        deployment: str,
        namespace: str = "default",
        force_fix: bool = False,
        dry_run: bool = False,
        event: str = "",
    ) -> Dict[str, Any]:
        """/autofix/repair 兼容入口：默认关闭；开启后只生成计划并走与 run 相同的确认与白名单链路

        SEC-26：旧实现绕过风险评估直接 patch（含硬编码回退镜像、force 激进规则），已删除。
        ``dry_run`` 仅为签名兼容保留——本入口本身不执行任何写操作。
        """
        async with self._operation():
            ensure_repair_enabled()
            if force_fix:
                raise ValidationError("force", "force 修复已停用：所有动作必须经 /workflow/confirm 显式确认，不能绕过风险评估")
            self._validate_k8s_params(deployment, namespace)
            problem = (event or "").strip() or f"修复 Deployment {deployment}"
            result = await self._execute_owned_workflow(
                {
                    "problem_description": problem,
                    "deployment": deployment,
                    "namespace": namespace,
                    "event": problem,
                }
            )
            executed = result.get("executed_actions") or []
            result.update(
                {
                    "deployment": deployment,
                    "namespace": namespace,
                    "dry_run": dry_run,
                    "success": result.get("status") == "completed" and bool(executed),
                    "actions_taken": [str(action.get("action_type") or action.get("action_id")) for action in executed],
                }
            )
            return result

    async def bootstrap_test_resources(self) -> Dict[str, Any]:
        async with self._operation():
            return await self._bootstrap_test_resources()

    async def _bootstrap_test_resources(self) -> Dict[str, Any]:
        """在test命名空间创建用于自愈演示的故障资源（默认关闭；开启后 test 仍须在 allowed_namespaces 内）"""
        require_principal()
        ensure_bootstrap_enabled()
        # 白名单校验先于初始化检查与任何 Kubernetes 调用
        ensure_bootstrap_namespace_allowed()
        self._ensure_initialized()
        namespace = BOOTSTRAP_NAMESPACE
        try:
            k8s = self._k8s_fixer.k8s_service
            # 确保命名空间存在
            await k8s.ensure_namespace(namespace)

            results = []
            manifests = [
                "deploy/test-namespace-setup.yaml",
                "deploy/test-additional-problems.yaml",
            ]
            for m in manifests:
                results.append(await k8s.apply_yaml_file(m, namespace=namespace))

            return {
                "success": all(r.get("success") for r in results),
                "applied": results,
                "namespace": namespace,
            }
        except Exception as e:
            self.logger.exception(f"引导测试资源失败, 错误类型: {type(e).__name__}")
            raise AutoFixError("引导测试资源失败") from e

    async def diagnose_kubernetes_issues(self, deployment: Optional[str] = None, namespace: str = "default", include_pods: bool = True, include_logs: bool = False, include_events: bool = True) -> Dict[str, Any]:
        async with self._operation():
            return await self._diagnose_kubernetes_issues(deployment, namespace, include_pods, include_logs, include_events)

    async def _diagnose_kubernetes_issues(
        self,
        deployment: Optional[str] = None,
        namespace: str = "default",
        include_pods: bool = True,
        include_logs: bool = False,
        include_events: bool = True,
    ) -> Dict[str, Any]:
        """诊断Kubernetes问题"""
        require_principal()
        self._ensure_initialized()

        # 验证命名空间
        if not namespace or not isinstance(namespace, str):
            raise ValidationError("namespace", "命名空间不能为空")

        try:
            diagnosis_result = {
                "namespace": namespace,
                "deployment": deployment,
                "timestamp": datetime.now().isoformat(),
                "diagnosis": {},
            }

            # 获取部署信息
            if deployment:
                deployment_info = await self.execute_with_timeout(
                    lambda: self._k8s_fixer.k8s_service.get_deployment(
                        deployment, namespace
                    ),
                    timeout=ServiceConstants.AUTOFIX_K8S_TIMEOUT,
                    operation_name="get_deployment_for_diagnosis",
                )
                diagnosis_result["diagnosis"]["deployment"] = deployment_info

            # 获取Pod信息
            if include_pods:
                pods = await self.execute_with_timeout(
                    lambda: self._k8s_fixer.k8s_service.get_pods(namespace, deployment),
                    timeout=ServiceConstants.AUTOFIX_K8S_TIMEOUT,
                    operation_name="get_pods",
                )
                diagnosis_result["diagnosis"]["pods"] = pods

                # 获取Pod日志
                if include_logs and pods:
                    logs = {}
                    for pod_info in pods[: ServiceConstants.AUTOFIX_MAX_PODS_FOR_LOGS]:
                        try:
                            pod_logs = await self.execute_with_timeout(
                                lambda: self._k8s_fixer.k8s_service.get_pod_logs(
                                    pod_info["name"], namespace
                                ),
                                timeout=ServiceConstants.AUTOFIX_LOGS_TIMEOUT,
                                operation_name="get_pod_logs",
                            )
                            logs[pod_info["name"]] = pod_logs
                        except Exception as e:
                            self.logger.exception(f"获取Pod日志失败 - Pod: {pod_info['name']}, 错误类型: {type(e).__name__}")
                            logs[pod_info["name"]] = "获取日志失败"

                    diagnosis_result["diagnosis"]["logs"] = logs

            # 获取事件信息
            if include_events:
                events = await self.execute_with_timeout(
                    lambda: self._k8s_fixer.k8s_service.get_events(
                        namespace, deployment
                    ),
                    timeout=ServiceConstants.AUTOFIX_K8S_TIMEOUT,
                    operation_name="get_events",
                )
                diagnosis_result["diagnosis"]["events"] = events

            return diagnosis_result

        except Exception as e:
            if isinstance(e, (ValidationError, AutoFixError)):
                self.logger.error(f"诊断Kubernetes问题失败: {e.message}")
                raise e
            self.logger.exception(f"诊断Kubernetes问题失败, 错误类型: {type(e).__name__}")
            raise AutoFixError("诊断失败") from e

    async def get_autofix_config(self) -> Dict[str, Any]:
        """获取自动修复配置信息"""
        config_info = {
            "execution_policy": {
                "auto_execute_enabled": bool(config.autofix.auto_execute_enabled),
                "allowed_namespaces": list(config.autofix.allowed_namespaces or []),
                "repair_enabled": bool(config.autofix.repair_enabled),
                "bootstrap_enabled": bool(config.autofix.bootstrap_enabled),
                "confirm_endpoint": CONFIRM_ENDPOINT,
            },
            "k8s_config": {
                "namespace_default": "default",
                "timeout": ServiceConstants.AUTOFIX_K8S_TIMEOUT,
                "max_pods_for_logs": ServiceConstants.AUTOFIX_MAX_PODS_FOR_LOGS,
                "logs_timeout": ServiceConstants.AUTOFIX_LOGS_TIMEOUT,
            },
            "workflow_config": {
                "supervisor_timeout": ServiceConstants.AUTOFIX_WORKFLOW_TIMEOUT,
                "analysis_timeout": ServiceConstants.AUTOFIX_ANALYSIS_TIMEOUT,
                "retry_attempts": getattr(config, "autofix_retry_attempts", 3),
                "retry_delay": getattr(config, "autofix_retry_delay", 5),
                "workflow_engine": "langgraph",
                "workflow_nodes": [
                    "Coordinator",
                    "Analyzer",
                    "Planner",
                    "Reviewer",
                    "Executor",
                ],
            },
            "supported_resources": [
                "deployments",
                "pods",
                "services",
                "configmaps",
                "secrets",
            ],
            "fix_strategies": [
                "restart_deployment",
                "scale_deployment",
                "patch_image_pull_policy",
                "fix_configuration",
                "resource_adjustment",
                "risk_assessment",
                "multi_agent_workflow",
            ],
            "constraints": {
                "max_deployment_name_length": ServiceConstants.AUTOFIX_MAX_NAME_LENGTH,
                "max_namespace_name_length": ServiceConstants.AUTOFIX_MAX_NAME_LENGTH,
                "k8s_timeout": ServiceConstants.AUTOFIX_K8S_TIMEOUT,
                "workflow_timeout": ServiceConstants.AUTOFIX_WORKFLOW_TIMEOUT,
            },
        }

        return config_info

    async def get_service_health_info(self) -> Dict[str, Any]:
        """获取服务健康信息"""
        try:
            health_status = {
                "service": "autofix",
                "status": (
                    ServiceConstants.STATUS_HEALTHY
                    if await self.health_check()
                    else ServiceConstants.STATUS_UNHEALTHY
                ),
                "timestamp": datetime.now().isoformat(),
                "components": {
                    "k8s_fixer": "unknown",
                    "supervisor": "unknown",
                    "k8s_service": "unknown",
                    "remediation_engine": "unknown",
                    "ops_workflow": "unknown",
                },
            }

            # 检查各组件状态
            if self._k8s_fixer:
                health_status["components"]["k8s_fixer"] = (
                    ServiceConstants.STATUS_HEALTHY
                )

                # 检查K8s服务连接
                try:
                    k8s_health = await self.execute_with_timeout(
                        self._k8s_fixer.k8s_service.health_check,
                        timeout=ServiceConstants.AUTOFIX_K8S_TIMEOUT,
                        operation_name="k8s_service_health",
                    )
                    health_status["components"]["k8s_service"] = (
                        ServiceConstants.STATUS_HEALTHY
                        if k8s_health
                        else ServiceConstants.STATUS_UNHEALTHY
                    )
                except Exception:
                    health_status["components"]["k8s_service"] = (
                        ServiceConstants.STATUS_UNHEALTHY
                    )
            else:
                health_status["components"]["k8s_fixer"] = (
                    ServiceConstants.STATUS_UNHEALTHY
                )

            if self._supervisor:
                health_status["components"]["supervisor"] = (
                    ServiceConstants.STATUS_HEALTHY
                )
            else:
                health_status["components"]["supervisor"] = (
                    ServiceConstants.STATUS_UNHEALTHY
                )

            health_status["components"]["remediation_engine"] = (
                ServiceConstants.STATUS_HEALTHY
                if self._remediation_engine
                else ServiceConstants.STATUS_UNHEALTHY
            )
            health_status["components"]["ops_workflow"] = (
                ServiceConstants.STATUS_HEALTHY
                if self._ops_workflow
                else ServiceConstants.STATUS_UNHEALTHY
            )

            return health_status

        except Exception as e:
            self.logger.exception(f"获取自动修复服务健康信息失败, 错误类型: {type(e).__name__}")
            return {
                "service": "autofix",
                "status": ServiceConstants.STATUS_UNHEALTHY,
                "error": "获取健康信息失败",
                "timestamp": datetime.now().isoformat(),
            }

    def _validate_k8s_params(self, deployment: str, namespace: str) -> None:
        """验证Kubernetes参数"""
        if not deployment or not isinstance(deployment, str):
            raise ValidationError("deployment", "部署名称不能为空")

        if len(deployment) > ServiceConstants.AUTOFIX_MAX_NAME_LENGTH:
            raise ValidationError(
                "deployment",
                f"部署名称长度不能超过 {ServiceConstants.AUTOFIX_MAX_NAME_LENGTH} 字符",
            )

        if not namespace or not isinstance(namespace, str):
            raise ValidationError("namespace", "命名空间不能为空")

        if len(namespace) > ServiceConstants.AUTOFIX_MAX_NAME_LENGTH:
            raise ValidationError(
                "namespace",
                f"命名空间长度不能超过 {ServiceConstants.AUTOFIX_MAX_NAME_LENGTH} 字符",
            )

    async def cleanup(self) -> None:
        """清理自动修复服务资源"""
        try:
            self._closing = True
            current = asyncio.current_task()
            tasks = [task for task in self._active_operations if task is not current and not task.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._active_operations.clear()
            self._approvals.clear()
            self.logger.info("开始清理自动修复服务资源...")

            # 清理K8s修复代理
            if self._k8s_fixer:
                try:
                    if hasattr(self._k8s_fixer, "cleanup"):
                        if asyncio.iscoroutinefunction(self._k8s_fixer.cleanup):
                            await self._k8s_fixer.cleanup()
                        else:
                            self._k8s_fixer.cleanup()
                except Exception as e:
                    self.logger.warning("清理K8s修复代理失败，异常类型: %s", type(e).__name__)
                    self.logger.debug("清理K8s修复代理失败详情: %s", e, exc_info=True)
                self._k8s_fixer = None

            # 清理监督代理
            if self._supervisor:
                try:
                    if hasattr(self._supervisor, "cleanup"):
                        if asyncio.iscoroutinefunction(self._supervisor.cleanup):
                            await self._supervisor.cleanup()
                        else:
                            self._supervisor.cleanup()
                except Exception as e:
                    self.logger.warning("清理监督代理失败，异常类型: %s", type(e).__name__)
                    self.logger.debug("清理监督代理失败详情: %s", e, exc_info=True)
                self._supervisor = None

            self._ops_workflow = None
            self._remediation_engine = None

            # 调用父类清理方法
            await super().cleanup()

            self.logger.info("自动修复服务资源清理完成")

        except Exception as e:
            self.logger.error("自动修复服务资源清理失败，异常类型: %s", type(e).__name__)
            self.logger.debug("自动修复服务资源清理失败详情: %s", e, exc_info=True)
            raise
