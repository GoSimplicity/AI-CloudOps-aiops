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
from copy import deepcopy
from datetime import datetime
import logging
import time
from typing import Any, Dict, List, Optional

from ..common.constants import ServiceConstants
from ..common.exceptions import AutoFixError, ResourceNotFoundError, ValidationError
from ..core.agents.k8s_fixer import K8sFixerAgent
from ..core.agents.ops_workflow import OpsMultiAgentWorkflow
from ..core.agents.supervisor import SupervisorAgent
from ..core.autofix import AutoFixRemediationEngine
from ..core.interfaces.k8s_client import K8sClient
from ..core.interfaces.llm_client import NullLLMClient
from .base import BaseService

logger = logging.getLogger("aiops.services.autofix")


class AutoFixService(BaseService):
    """自动修复服务"""

    def __init__(self) -> None:
        super().__init__("autofix")
        self._k8s_fixer: Optional[K8sFixerAgent] = None
        self._supervisor: Optional[SupervisorAgent] = None
        self._remediation_engine: Optional[AutoFixRemediationEngine] = None
        self._ops_workflow: Optional[OpsMultiAgentWorkflow] = None
        self._pending_workflows: Dict[str, Dict[str, Any]] = {}

    async def _do_initialize(self) -> None:
        try:
            # 初始化K8s修复代理（注入K8s和LLM依赖）
            from .kubernetes import KubernetesService
            from .llm import LLMService

            k8s_client: K8sClient = KubernetesService()
            llm_client = LLMService()
            self._k8s_fixer = K8sFixerAgent(
                llm_client=llm_client, k8s_client=k8s_client
            )
            self.logger.info("K8s修复代理初始化完成")

            # 初始化监督代理（注入LLM）
            self._supervisor = SupervisorAgent(llm_client=llm_client)
            self.logger.info("监督代理初始化完成")

            self._remediation_engine = AutoFixRemediationEngine(k8s_client=k8s_client)
            self._ops_workflow = OpsMultiAgentWorkflow(
                remediation_engine=self._remediation_engine,
                llm_client=llm_client,
            )
            self.logger.info("自动修复执行引擎和多智能体工作流初始化完成")

        except Exception as e:
            self.logger.error(f"自动修复服务组件初始化失败: {str(e)}")
            raise AutoFixError(f"初始化失败: {str(e)}")

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
            self.logger.warning(f"自动修复服务健康检查失败: {str(e)}")
            return False

    async def execute_multi_agent_workflow(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """执行基于LangGraph的多智能体自动修复工作流。"""
        self._ensure_initialized()

        problem_description = (request or {}).get("problem_description")
        if not problem_description or not isinstance(problem_description, str):
            raise ValidationError("problem_description", "问题描述不能为空")

        if self._ops_workflow is None:
            if self._remediation_engine is None:
                if not self._k8s_fixer:
                    raise AutoFixError("自动修复执行引擎未初始化")
                self._remediation_engine = AutoFixRemediationEngine(
                    k8s_client=self._k8s_fixer.k8s_service
                )
            llm_client = (
                self._k8s_fixer.llm_service
                if self._k8s_fixer and getattr(self._k8s_fixer, "llm_service", None)
                else NullLLMClient()
            )
            self._ops_workflow = OpsMultiAgentWorkflow(
                remediation_engine=self._remediation_engine,
                llm_client=llm_client,
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
        plan_id = result.get("plan", {}).get("plan_id")
        if plan_id:
            if result.get("status") == "needs_human_confirmation":
                self._pending_workflows[plan_id] = deepcopy(result)
            else:
                self._pending_workflows.pop(plan_id, None)
        return result

    async def confirm_multi_agent_workflow(
        self, plan_id: str, approved_action_ids: List[str]
    ) -> Dict[str, Any]:
        """人工确认后继续执行多智能体工作流中被阻断的动作。"""
        self._ensure_initialized()

        normalized_plan_id = str(plan_id or "").strip()
        if not normalized_plan_id:
            raise ValidationError("plan_id", "计划ID不能为空")

        workflow_result = deepcopy(self._pending_workflows.get(normalized_plan_id) or {})
        if not workflow_result:
            raise ValidationError("plan_id", "待确认工作流不存在或已过期")

        if workflow_result.get("status") != "needs_human_confirmation":
            raise ValidationError("plan_id", "当前工作流不处于待人工确认状态")

        normalized_action_ids = [str(action_id).strip() for action_id in approved_action_ids if str(action_id).strip()]
        if not normalized_action_ids:
            raise ValidationError("approved_action_ids", "至少选择一个待确认动作")

        if self._remediation_engine is None:
            if not self._k8s_fixer:
                raise AutoFixError("自动修复执行引擎未初始化")
            self._remediation_engine = AutoFixRemediationEngine(
                k8s_client=self._k8s_fixer.k8s_service
            )

        diagnosis = deepcopy(workflow_result.get("diagnosis") or {})
        if not diagnosis:
            raise ValidationError("plan_id", "工作流结果缺少诊断上下文")

        plan = await self._remediation_engine.build_plan(diagnosis)
        plan["plan_id"] = normalized_plan_id
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
        allowed_actions = deepcopy(plan.get("allowed_actions") or [])
        allowed_actions = [
            action
            for action in allowed_actions
            if str(action.get("action_id") or "") not in previous_executed_action_ids
        ]
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
                f"存在无效动作ID: {', '.join(invalid_action_ids)}",
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

        execution = await self._remediation_engine.execute_plan(plan)
        current_executed_actions = execution.get("executed_actions", [])
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
                "action": f"人工批准 {len(approved_actions)} 个高风险动作继续执行",
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
        if remaining_blocked_count > 0:
            self._pending_workflows[normalized_plan_id] = deepcopy(response)
        else:
            self._pending_workflows.pop(normalized_plan_id, None)
        return response

    async def fix_kubernetes_deployment(
        self,
        deployment: str,
        namespace: str = "default",
        force_fix: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """修复K8s部署问题"""
        self._ensure_initialized()

        # 验证输入参数
        self._validate_k8s_params(deployment, namespace)

        try:
            # 获取部署信息
            deployment_info = await self.execute_with_timeout(
                lambda: self._k8s_fixer.k8s_service.get_deployment(
                    deployment, namespace
                ),
                timeout=ServiceConstants.AUTOFIX_K8S_TIMEOUT,
                operation_name="get_deployment",
            )

            if not deployment_info:
                raise ResourceNotFoundError(
                    "Deployment", f"{deployment} (namespace: {namespace})"
                )

            # 执行修复操作（统一走K8sFixer，结合event_hint决定是否强制修复策略）
            event_hint = (getattr(self, "_last_event_hint", "") or "").lower()

            # 优先快速路径：显式的镜像拉取失败提示，直接应用回退镜像
            try:
                if any(
                    k in event_hint
                    for k in [
                        "imagepullbackoff",
                        "errimagepull",
                        "image pull",
                        "failed to pull image",
                    ]
                ):
                    containers = (
                        deployment_info.get("spec", {})
                        .get("template", {})
                        .get("spec", {})
                        .get("containers", [])
                        or []
                    )
                    # 选择目标容器：优先使用请求传入，其次第一个容器
                    requested_container = getattr(self, "_target_container", None)
                    if requested_container and any(
                        c.get("name") == requested_container for c in containers
                    ):
                        target_container_name = requested_container
                    else:
                        target_container_name = (
                            containers[0].get("name", deployment)
                            if containers
                            else deployment
                        )

                    fallback_image = "nginx:1.21.6"

                    # 若当前镜像已为回退镜像，则无需patch
                    current_image = None
                    for c in containers:
                        if c.get("name") == target_container_name:
                            current_image = c.get("image")
                            break

                    need_patch = current_image != fallback_image
                    patched = False

                    start_ts = time.time()

                    if need_patch:
                        patch = {
                            "spec": {
                                "template": {
                                    "spec": {
                                        "containers": [
                                            {
                                                "name": target_container_name,
                                                "image": fallback_image,
                                                "imagePullPolicy": "IfNotPresent",
                                            }
                                        ]
                                    }
                                }
                            }
                        }

                        patched = await self._k8s_fixer.k8s_service.patch_deployment(
                            deployment, patch, namespace
                        )

                    # 可选等待rollout完成，确保可用性
                    should_wait = bool(getattr(self, "_wait_rollout", True))
                    if should_wait:
                        rollout_timeout = min(
                            ServiceConstants.AUTOFIX_DEFAULT_TIMEOUT, 120
                        )
                        rollout = await self._k8s_fixer.k8s_service.wait_for_deployment_rollout(
                            deployment, namespace, timeout_seconds=rollout_timeout
                        )
                    else:
                        rollout = {"ready": True, "skipped": True}

                    success = bool(rollout.get("ready"))
                    elapsed = round(time.time() - start_ts, 3)

                    actions = []
                    if need_patch and patched:
                        actions.extend(
                            [
                                f"更新镜像为 {fallback_image}",
                                "设置 imagePullPolicy=IfNotPresent",
                            ]
                        )
                    elif not need_patch:
                        actions.append("镜像已为回退版本，无需更新")

                    if should_wait and not success:
                        if isinstance(rollout, dict) and "error" in rollout:
                            actions.append(f"rollout检查失败: {rollout.get('error')}")
                        else:
                            actions.append("等待rollout超时")

                    msg = (
                        f"镜像为 {fallback_image}，部署已就绪"
                        if success
                        else f"镜像为 {fallback_image}，部署未在超时内就绪"
                    )

                    return self._wrap_fix_result(
                        {
                            "fixed": success,
                            "message": msg,
                            "actions_taken": actions,
                            "success": success,
                            "status": "completed" if success else "degraded",
                            "rollout": rollout,
                            "execution_time": elapsed,
                        },
                        deployment,
                        namespace,
                        "k8s_fixer",
                        dry_run,
                    )
            except Exception as quick_e:
                # 快速路径失败则继续走常规分析修复
                self.logger.warning(f"快速镜像回退路径失败，将尝试通用修复: {quick_e}")

            # 常规路径：将提示拼入错误描述，触发更激进的规则
            if force_fix or any(
                k in event_hint
                for k in ["crashloop", "imagepull", "oom", "probe", "unhealthy"]
            ):
                error_desc = event_hint or ""
            else:
                error_desc = ""

            fix_result = await self.execute_with_timeout(
                lambda: self._k8s_fixer.analyze_and_fix_deployment(
                    deployment, namespace, error_desc
                ),
                timeout=ServiceConstants.AUTOFIX_ANALYSIS_TIMEOUT,
                operation_name="k8s_fix_analysis",
            )

            return self._wrap_fix_result(
                fix_result, deployment, namespace, "k8s_fixer", dry_run
            )

        except Exception as e:
            self.logger.error(f"修复Kubernetes部署失败: {str(e)}")
            if isinstance(e, (ValidationError, ResourceNotFoundError, AutoFixError)):
                raise e
            raise AutoFixError(f"修复失败: {str(e)}")

    async def bootstrap_test_resources(self) -> Dict[str, Any]:
        """在test命名空间创建用于自愈演示的故障资源"""
        self._ensure_initialized()
        try:
            k8s = self._k8s_fixer.k8s_service
            # 确保命名空间存在
            await k8s.ensure_namespace("test")

            results = []
            manifests = [
                "deploy/test-namespace-setup.yaml",
                "deploy/test-additional-problems.yaml",
            ]
            for m in manifests:
                results.append(await k8s.apply_yaml_file(m, namespace="test"))

            return {
                "success": all(r.get("success") for r in results),
                "applied": results,
                "namespace": "test",
            }
        except Exception as e:
            raise AutoFixError(f"引导测试资源失败: {str(e)}")

    async def diagnose_kubernetes_issues(
        self,
        deployment: Optional[str] = None,
        namespace: str = "default",
        include_pods: bool = True,
        include_logs: bool = False,
        include_events: bool = True,
    ) -> Dict[str, Any]:
        """诊断Kubernetes问题"""
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
                            logs[pod_info["name"]] = f"获取日志失败: {str(e)}"

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
            self.logger.error(f"诊断Kubernetes问题失败: {str(e)}")
            if isinstance(e, (ValidationError, AutoFixError)):
                raise e
            raise AutoFixError(f"诊断失败: {str(e)}")

    async def get_autofix_config(self) -> Dict[str, Any]:
        """获取自动修复配置信息"""
        from ..config.settings import config

        config_info = {
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
                "update_image",
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
            self.logger.error(f"获取自动修复服务健康信息失败: {str(e)}")
            return {
                "service": "autofix",
                "status": ServiceConstants.STATUS_UNHEALTHY,
                "error": str(e),
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

    def _wrap_fix_result(
        self,
        fix_result: Any,
        deployment: str,
        namespace: str,
        agent_type: str,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """包装修复结果"""
        if isinstance(fix_result, dict):
            wrapped_result = fix_result.copy()
        else:
            wrapped_result = {"result": str(fix_result)}

        # 添加元数据
        wrapped_result.update(
            {
                "deployment": deployment,
                "namespace": namespace,
                "agent_type": agent_type,
                "dry_run": dry_run,
                "timestamp": datetime.now().isoformat(),
                "success": wrapped_result.get(
                    "success", bool(wrapped_result.get("fixed", True))
                ),
            }
        )

        # 确保包含必要字段
        if "actions_taken" not in wrapped_result:
            # 尽量从 message 中提取操作；若无则为空
            wrapped_result["actions_taken"] = wrapped_result.get("actions", []) or []

        if "status" not in wrapped_result:
            wrapped_result["status"] = (
                "completed" if wrapped_result["success"] else "failed"
            )

        return wrapped_result

    async def cleanup(self) -> None:
        """清理自动修复服务资源"""
        try:
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
                    self.logger.warning(f"清理K8s修复代理失败: {e}")
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
                    self.logger.warning(f"清理监督代理失败: {e}")
                self._supervisor = None

            self._ops_workflow = None
            self._remediation_engine = None

            # 调用父类清理方法
            await super().cleanup()

            self.logger.info("自动修复服务资源清理完成")

        except Exception as e:
            self.logger.error(f"自动修复服务资源清理失败: {str(e)}")
            raise
