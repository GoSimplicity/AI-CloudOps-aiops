#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.config.settings import config
from app.core.agents.detector import K8sDetectorAgent
from app.core.agents.executor import K8sExecutorAgent
from app.core.agents.rollback import K8sRollbackAgent
from app.core.agents.strategist import K8sStrategistAgent
from app.core.agents.verifier import K8sVerifierAgent
from app.services.notification import NotificationService
from app.utils.time_utils import iso_utc_now

logger = logging.getLogger("aiops.coordinator")


"""
说明：本模块内的工作流方法直接返回 Dict 结构用于 API 响应，
原先定义的 AgentState 数据类未被引用，已移除以减少冗余。
"""


class K8sCoordinatorAgent:
    """K8s多Agent协调器"""

    def __init__(self):
        self.detector = K8sDetectorAgent()
        self.strategist = K8sStrategistAgent()
        self.executor = K8sExecutorAgent()
        self.verifier = K8sVerifierAgent()
        self.rollback = K8sRollbackAgent()
        self.notification_service = NotificationService()
        self.workflow_history = []
        # 轻量指标（内存级计数，便于API导出）
        self.metrics = {
            "total_workflows": 0,
            "successful_workflows": 0,
            "rolled_back": 0,
            "avg_success_rate": 0.0,
        }

    async def run_full_workflow(
        self, deployment: str, namespace: str = "default"
    ) -> Dict[str, Any]:
        """运行完整的修复工作流"""
        try:
            logger.info(f"🚀 开始K8s多Agent修复工作流: {deployment}/{namespace}")

            workflow_id = f"workflow_{int(asyncio.get_event_loop().time())}"
            start_time = datetime.now(timezone.utc)

            # 步骤1: 检测问题
            logger.info("🔍 步骤1: 检测问题...")
            issues = await self.detector.detect_deployment_issues(deployment, namespace)

            if "error" in issues:
                return {
                    "workflow_id": workflow_id,
                    "success": False,
                    "error": issues["error"],
                    "stage": "detection",
                }

            if not issues["issues"]:
                return {
                    "workflow_id": workflow_id,
                    "success": True,
                    "message": "未发现问题，无需修复",
                    "stage": "detection",
                }

            # 步骤2: 制定策略
            logger.info("📋 步骤2: 制定修复策略...")
            all_issues = await self.detector.detect_all_issues(namespace)
            strategy = await self.strategist.analyze_issues(all_issues)

            if "error" in strategy:
                return {
                    "workflow_id": workflow_id,
                    "success": False,
                    "error": strategy["error"],
                    "stage": "strategy",
                }

            # 步骤3: 执行策略（受安全策略与命名空间白/黑名单约束）
            logger.info("⚙️ 步骤3: 执行修复策略...")
            execution_results = []

            for strategy_item in strategy.get("strategies", []):
                if strategy_item["target"]["name"] != deployment:
                    continue
                if not strategy_item.get("auto_fix", False):
                    continue
                # 命名空间白/黑名单检查
                if config.remediation.namespace_whitelist and (namespace not in set(config.remediation.namespace_whitelist)):
                    logger.info(f"跳过执行（不在白名单）: {deployment}/{namespace}")
                    continue
                if config.remediation.namespace_blacklist and (namespace in set(config.remediation.namespace_blacklist)):
                    logger.info(f"跳过执行（在黑名单）: {deployment}/{namespace}")
                    continue
                # 安全模式：仅告警不执行
                if config.remediation.safe_mode:
                    logger.info(f"安全模式启用：仅告警不执行策略 {strategy_item.get('id')}")
                    execution_results.append({
                        "execution_id": f"noop_{int(asyncio.get_event_loop().time())}",
                        "strategy_id": strategy_item.get("id"),
                        "target": strategy_item.get("target"),
                        "success": True,
                        "steps": [],
                        "errors": [],
                        "message": "safe_mode: no-op"
                    })
                    continue

                # 正常执行策略（仅在非 safe_mode 情况下）
                execution_result = await self.executor.execute_strategy(strategy_item)
                execution_results.append(execution_result)

            # 步骤4: 验证结果（双通道：规则再检测 + 就绪率验证）
            logger.info("✅ 步骤4: 验证修复结果...")
            final_issues = await self.detector.detect_deployment_issues(deployment, namespace)
            verify_wait = max(0, int(config.remediation.verify_wait_seconds or 20))
            verification = await self.verifier.verify_deployment_health(deployment, namespace, wait_seconds=verify_wait)

            # 若验证失败，尝试回滚
            if verification.get("status") == "failed" and config.remediation.allow_rollback:
                logger.warning("验证失败，执行回滚...")
                await self.rollback.rollback_deployment(deployment, namespace, reason="verification_failed")

            # 步骤5: 生成最终报告
            final_report = await self._generate_final_report(
                workflow_id, issues, strategy, execution_results, final_issues
            )
            final_report["verification"] = verification

            # 保存工作流历史
            self.workflow_history.append(
                {
                    "workflow_id": workflow_id,
                    "deployment": deployment,
                    "namespace": namespace,
                    "start_time": start_time.isoformat(),
            "end_time": iso_utc_now(),
                    "issues_detected": len(issues["issues"]),
                    "strategies_created": len(strategy.get("strategies", [])),
                    "executions": len(execution_results),
                    "final_report": final_report,
                }
            )

            # 发送通知
            await self._send_workflow_notification(final_report)

            # 更新内存指标
            self.metrics["total_workflows"] += 1
            self.metrics["successful_workflows"] += 1 if final_report.get("success") else 0
            if verification.get("status") == "failed":
                self.metrics["rolled_back"] += 1 if config.remediation.allow_rollback else 0
            # 平滑更新平均成功率（工作流成功率而非Pod成功率）
            t = self.metrics["total_workflows"]
            s = self.metrics["successful_workflows"]
            self.metrics["avg_success_rate"] = round((s / t) * 100, 2) if t > 0 else 0.0

            logger.info(f"✅ 工作流完成: {workflow_id}")
            return final_report

        except Exception as e:
            logger.error(f"工作流执行失败: {str(e)}")
            return {
                "workflow_id": f"workflow_{int(asyncio.get_event_loop().time())}",
                "success": False,
                "error": str(e),
                "stage": "coordinator",
            }

    async def run_batch_workflow(self, namespace: str = "default") -> Dict[str, Any]:
        """批量处理命名空间内所有问题"""
        try:
            logger.info(f"🚀 开始批量修复工作流: {namespace}")

            workflow_id = f"batch_{int(asyncio.get_event_loop().time())}"
            start_time = datetime.now(timezone.utc)

            # 检测所有问题
            all_issues = await self.detector.detect_all_issues(namespace)

            if "error" in all_issues:
                return {"success": False, "error": all_issues["error"]}

            if all_issues["summary"]["total_issues"] == 0:
                return {
                    "workflow_id": workflow_id,
                    "success": True,
                    "message": "未发现问题",
                    "issues_processed": 0,
                }

            # 制定批量策略
            strategy = await self.strategist.analyze_issues(all_issues)

            # 执行所有策略
            execution_results = []
            for strategy_item in strategy.get("strategies", []):
                if strategy_item["auto_fix"]:
                    result = await self.executor.execute_strategy(strategy_item)
                    execution_results.append(result)

            # 验证结果
            final_issues = await self.detector.detect_all_issues(namespace)

            batch_report = {
                "workflow_id": workflow_id,
                "namespace": namespace,
                "start_time": start_time.isoformat(),
            "end_time": iso_utc_now(),
                "initial_issues": all_issues["summary"]["total_issues"],
                "fixable_issues": strategy["summary"]["fixable_issues"],
                "executed_strategies": len(execution_results),
                "successful_executions": len(
                    [r for r in execution_results if r.get("success")]
                ),
                "remaining_issues": final_issues["summary"]["total_issues"],
                "success": final_issues["summary"]["total_issues"]
                < all_issues["summary"]["total_issues"],
            }

            self.workflow_history.append(batch_report)

            return batch_report

        except Exception as e:
            logger.error(f"批量工作流失败: {str(e)}")
            return {"success": False, "error": str(e)}

    async def _generate_final_report(
        self,
        workflow_id: str,
        initial_issues: Dict,
        strategy: Dict,
        executions: List[Dict],
        final_issues: Dict,
    ) -> Dict[str, Any]:
        """生成最终报告"""
        try:
            # 计算修复成功率
            total_issues = initial_issues["summary"]["total_issues"]
            remaining_issues = final_issues["summary"]["total_issues"]
            fixed_issues = max(0, total_issues - remaining_issues)
            success_rate = (
                (fixed_issues / total_issues * 100) if total_issues > 0 else 0
            )

            # 分析执行结果
            successful_executions = len([e for e in executions if e.get("success")])
            total_executions = len(executions)

            report = {
                "workflow_id": workflow_id,
        "timestamp": iso_utc_now(),
                "success": remaining_issues < total_issues,
                "summary": {
                    "total_issues": total_issues,
                    "fixed_issues": fixed_issues,
                    "remaining_issues": remaining_issues,
                    "success_rate": round(success_rate, 2),
                    "executions": total_executions,
                    "successful_executions": successful_executions,
                },
                "details": {
                    "initial_issues": initial_issues,
                    "strategy": strategy,
                    "execution_results": executions,
                    "final_issues": final_issues,
                },
                "recommendations": await self._generate_recommendations(final_issues),
            }

            return report

        except Exception as e:
            logger.error(f"生成报告失败: {str(e)}")
            return {"success": False, "error": str(e)}

    async def _generate_recommendations(self, final_issues: Dict) -> List[str]:
        """生成后续建议"""
        recommendations = []

        if "error" in final_issues:
            recommendations.append("检查集群连接和权限")
            return recommendations

        if final_issues["summary"]["total_issues"] > 0:
            remaining_types = set()
            for issue in final_issues.get("details", []):
                remaining_types.add(issue.get("sub_type", "unknown"))

            for issue_type in remaining_types:
                if issue_type == "image_pull_error":
                    recommendations.append("检查镜像仓库访问权限和镜像标签")
                elif issue_type == "crash_loop":
                    recommendations.append("查看Pod日志，分析应用崩溃原因")
                elif issue_type == "resource_pressure":
                    recommendations.append("增加节点资源或优化应用资源使用")
                elif issue_type == "replica_mismatch":
                    recommendations.append("手动检查Deployment配置和节点状态")

        if not recommendations:
            recommendations.append("系统运行正常，建议定期监控")

        return recommendations

    async def _send_workflow_notification(self, report: Dict[str, Any]):
        """发送工作流完成通知"""
        try:
            summary = report["summary"]
            message = f"""
🎯 K8s修复工作流完成

工作流ID: {report["workflow_id"]}
修复成功率: {summary["success_rate"]}%
修复问题数: {summary["fixed_issues"]}/{summary["total_issues"]}
执行策略数: {summary["successful_executions"]}/{summary["executions"]}

状态: {"✅ 成功" if report["success"] else "❌ 部分成功"}
"""

            await self.notification_service.send_feishu_message(
                message=message,
                title="K8s修复工作流报告",
                color="green" if report["success"] else "orange",
            )

        except Exception as e:
            logger.error(f"发送通知失败: {str(e)}")

    def get_workflow_history(self) -> List[Dict[str, Any]]:
        """获取工作流历史"""
        return self.workflow_history

    async def health_check(self) -> Dict[str, Any]:
        """协调器健康检查"""
        try:
            # 检查各组件状态
            detector_healthy = hasattr(self.detector, "k8s_service")
            strategist_healthy = hasattr(self.strategist, "llm_service")
            executor_healthy = hasattr(self.executor, "k8s_service")

            return {
                "healthy": all(
                    [detector_healthy, strategist_healthy, executor_healthy]
                ),
                "components": {
                    "detector": detector_healthy,
                    "strategist": strategist_healthy,
                    "executor": executor_healthy,
                },
        "timestamp": iso_utc_now(),
            }

        except Exception as e:
            return {
                "healthy": False,
                "error": str(e),
        "timestamp": iso_utc_now(),
            }

    # 备注：历史版本中的 reset_workflow 未在代码中被调用，已移除以保持代码整洁


class Coordinator:
    def __init__(self):
        self.agent = K8sCoordinatorAgent()

    async def fix_deployment(self, namespace: str, deployment: str, issues: List[str]):
        return await self.agent.run_full_workflow(deployment=deployment, namespace=namespace)

    async def execute_workflow(self, workflow_type: str, namespace: str, target: str):
        if workflow_type == "full_autofix":
            return await self.agent.run_batch_workflow(namespace=namespace)
        return {"workflow_id": "wf_0", "status": "unsupported"}
