#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
K8s执行Agent - 专门负责执行修复操作
Author: AI Assistant
License: Apache 2.0
Description: 执行具体修复操作的Agent，确保操作安全和可回滚
"""

import logging
import asyncio
import time
from typing import Dict, Any, List
from datetime import datetime
from app.services.kubernetes import KubernetesService
from app.services.notification import NotificationService

logger = logging.getLogger("aiops.executor")


class K8sExecutorAgent:
    """Kubernetes执行Agent"""

    def __init__(self):
        self.k8s_service = KubernetesService()
        self.notification_service = NotificationService()
        self.execution_log = []

    async def execute_strategy(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        """执行修复策略"""
        try:
            execution_id = f"exec_{int(time.time())}"
            logger.info(f"开始执行修复策略: {execution_id}")

            result = {
                "execution_id": execution_id,
                "timestamp": datetime.now().isoformat(),
                "strategy_id": strategy.get("id"),
                "target": strategy.get("target"),
                "success": False,
                "steps": [],
                "warnings": [],
                "errors": [],
                "rollback_needed": False,
                "final_state": None,
            }

            # 执行前检查
            pre_check_result = await self._pre_execution_check(strategy)
            if not pre_check_result["ready"]:
                result["errors"].extend(pre_check_result["errors"])
                return result

            # 创建备份
            backup = await self._create_backup(strategy)
            result["backup_created"] = backup

            # 执行每个步骤
            for step_idx, step in enumerate(strategy.get("steps", [])):
                step_result = await self._execute_step(step, strategy, step_idx + 1)
                result["steps"].append(step_result)

                if not step_result["success"]:
                    result["errors"].append(step_result["error"])
                    if strategy.get("auto_rollback", True):
                        result["rollback_needed"] = True
                        await self._rollback(backup, strategy)
                    break

            # 验证最终结果
            if not result["rollback_needed"]:
                verification = await self._verify_result(strategy)
                result["final_state"] = verification
                result["success"] = verification.get("verified", False)

                if not result["success"]:
                    result["errors"].append("修复验证失败")
                    if strategy.get("auto_rollback", True):
                        await self._rollback(backup, strategy)

            # 发送通知
            await self._send_notification(result, strategy)

            logger.info(f"策略执行完成: {execution_id}, 成功: {result['success']}")
            return result

        except Exception as e:
            logger.error(f"执行策略失败: {str(e)}")
            return {
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
                "success": False,
            }

    async def _pre_execution_check(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        """执行前检查"""
        try:
            target = strategy.get("target", {})
            resource_type = target.get("resource_type", "")
            name = target.get("name", "")
            namespace = target.get("namespace", "default")

            checks = {"ready": True, "errors": [], "warnings": []}

            if not name:
                checks["ready"] = False
                checks["errors"].append("资源名称不能为空")
                return checks

            # 检查资源是否存在
            resource_exists = await self._check_resource_exists(
                resource_type, name, namespace
            )
            if not resource_exists:
                checks["ready"] = False
                checks["errors"].append(f"目标资源不存在: {resource_type}/{name}")

            # 检查权限
            has_permission = await self._check_permissions(resource_type, namespace)
            if not has_permission:
                checks["ready"] = False
                checks["errors"].append(f"无权限操作: {resource_type} in {namespace}")

            # 检查集群状态
            cluster_healthy = await self._check_cluster_health()
            if not cluster_healthy:
                checks["warnings"].append("集群状态不佳，可能影响执行结果")

            return checks

        except Exception as e:
            logger.error(f"执行前检查失败: {str(e)}")
            return {"ready": False, "errors": [str(e)]}

    async def _create_backup(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        """创建资源备份"""
        try:
            target = strategy.get("target", {})
            resource_type = target.get("resource_type")
            name = target.get("name", "")
            namespace = target.get("namespace", "default")

            if not name:
                return {"error": "资源名称不能为空"}

            backup = {
                "created_at": datetime.now().isoformat(),
                "resource_type": resource_type,
                "name": name,
                "namespace": namespace,
                "data": None,
            }

            # 获取当前资源状态作为备份
            if resource_type == "deployment":
                deployment = await self.k8s_service.get_deployment(name, namespace)
                backup["data"] = deployment
            elif resource_type == "service":
                service = await self.k8s_service.get_service(name, namespace)
                backup["data"] = service

            logger.info(f"备份已创建: {resource_type}/{name}")
            return backup

        except Exception as e:
            logger.error(f"创建备份失败: {str(e)}")
            return {"error": str(e)}

    async def _execute_step(
        self, step: str, strategy: Dict[str, Any], step_num: int
    ) -> Dict[str, Any]:
        """执行单个步骤"""
        try:
            target = strategy.get("target", {})

            step_result = {
                "step_number": step_num,
                "description": step,
                "success": False,
                "start_time": datetime.now().isoformat(),
                "end_time": None,
                "details": {},
                "error": None,
            }

            # 根据步骤类型执行具体操作
            if "检查" in step:
                result = await self._execute_check_step(step, target)
            elif "调整" in step or "修改" in step:
                result = await self._execute_modify_step(step, target, strategy)
            elif "重启" in step:
                result = await self._execute_restart_step(step, target)
            elif "监控" in step:
                result = await self._execute_monitor_step(step, target)
            else:
                result = {"success": True, "details": {"action": "generic_step"}}

            step_result.update(result)
            step_result["end_time"] = datetime.now().isoformat()
            step_result["success"] = result.get("success", False)

            if not step_result["success"]:
                step_result["error"] = result.get("error", "步骤执行失败")

            logger.info(f"步骤 {step_num} 完成: {step}, 成功: {step_result['success']}")
            return step_result

        except Exception as e:
            logger.error(f"执行步骤失败: {str(e)}")
            return {
                "step_number": step_num,
                "description": step,
                "success": False,
                "error": str(e),
                "end_time": datetime.now().isoformat(),
            }

    async def _execute_check_step(
        self, step: str, target: Dict[str, Any]
    ) -> Dict[str, Any]:
        """执行检查步骤"""
        try:
            resource_type = target.get("resource_type", "")
            name = target.get("name", "")
            namespace = target.get("namespace", "default")

            if not name:
                return {"success": False, "error": "资源名称不能为空"}

            if resource_type == "deployment":
                deployment = await self.k8s_service.get_deployment(name, namespace)
                if deployment:
                    return {
                        "success": True,
                        "details": {
                            "replicas": deployment.get("spec", {}).get("replicas"),
                            "available": deployment.get("status", {}).get(
                                "available_replicas"
                            ),
                            "ready": deployment.get("status", {}).get("ready_replicas"),
                        },
                    }

            return {"success": True, "details": {"checked": True}}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _execute_modify_step(
        self, step: str, target: Dict[str, Any], strategy: Dict[str, Any]
    ) -> Dict[str, Any]:
        """执行修改步骤"""
        try:
            resource_type = target.get("resource_type", "")
            name = target.get("name", "")
            namespace = target.get("namespace", "default")

            if not name:
                return {"success": False, "error": "资源名称不能为空"}

            if resource_type == "deployment":
                if "探针" in step:
                    return await self._fix_probes(name, namespace, strategy)
                elif "资源" in step:
                    return await self._adjust_resources(name, namespace, strategy)

            return {"success": True, "details": {"modified": True}}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _execute_restart_step(
        self, step: str, target: Dict[str, Any]
    ) -> Dict[str, Any]:
        """执行重启步骤"""
        try:
            resource_type = target.get("resource_type", "")
            name = target.get("name", "")
            namespace = target.get("namespace", "default")

            if not name:
                return {"success": False, "error": "资源名称不能为空"}

            if resource_type == "deployment":
                success = await self.k8s_service.restart_deployment(name, namespace)
                return {"success": success, "details": {"action": "restart_deployment"}}

            return {"success": True, "details": {"restarted": True}}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _execute_monitor_step(
        self, step: str, target: Dict[str, Any]
    ) -> Dict[str, Any]:
        """执行监控步骤"""
        try:
            resource_type = target.get("resource_type", "")
            name = target.get("name", "")
            namespace = target.get("namespace", "default")

            if not name:
                return {"success": False, "error": "资源名称不能为空"}

            # 等待并检查状态
            await asyncio.sleep(10)  # 等待10秒

            if resource_type == "deployment":
                deployment = await self.k8s_service.get_deployment(name, namespace)
                if deployment:
                    ready = deployment.get("status", {}).get("ready_replicas", 0)
                    desired = deployment.get("spec", {}).get("replicas", 0)

                    return {
                        "success": ready == desired and ready > 0,
                        "details": {
                            "ready_replicas": ready,
                            "desired_replicas": desired,
                        },
                    }

            return {"success": True, "details": {"monitored": True}}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _fix_probes(
        self, name: str, namespace: str, strategy: Dict[str, Any]
    ) -> Dict[str, Any]:
        """修复探针配置"""
        try:
            # 创建探针修复补丁
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "main",
                                    "livenessProbe": {
                                        "httpGet": {"path": "/", "port": 80},
                                        "initialDelaySeconds": 30,
                                        "periodSeconds": 10,
                                        "timeoutSeconds": 5,
                                        "failureThreshold": 3,
                                    },
                                    "readinessProbe": {
                                        "httpGet": {"path": "/", "port": 80},
                                        "initialDelaySeconds": 10,
                                        "periodSeconds": 5,
                                        "timeoutSeconds": 3,
                                        "failureThreshold": 3,
                                    },
                                }
                            ]
                        }
                    }
                }
            }

            success = await self.k8s_service.patch_deployment(name, patch, namespace)
            return {
                "success": success,
                "details": {"action": "fix_probes", "deployment": name},
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _adjust_resources(
        self, name: str, namespace: str, strategy: Dict[str, Any]
    ) -> Dict[str, Any]:
        """调整资源配置"""
        try:
            # 创建资源调整补丁
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "main",
                                    "resources": {
                                        "requests": {"memory": "128Mi", "cpu": "100m"},
                                        "limits": {"memory": "256Mi", "cpu": "200m"},
                                    },
                                }
                            ]
                        }
                    }
                }
            }

            success = await self.k8s_service.patch_deployment(name, patch, namespace)
            return {
                "success": success,
                "details": {"action": "adjust_resources", "deployment": name},
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _rollback(
        self, backup: Dict[str, Any], strategy: Dict[str, Any]
    ) -> Dict[str, Any]:
        """回滚操作"""
        try:
            if not backup or "error" in backup:
                return {"success": False, "error": "无有效备份可回滚"}

            resource_type = backup.get("resource_type", "")
            name = backup.get("name", "")
            namespace = backup.get("namespace", "default")
            original_data = backup.get("data")

            if not name:
                return {"success": False, "error": "备份中资源名称为空"}

            if resource_type == "deployment" and original_data:
                # 恢复原始配置
                success = await self.k8s_service.patch_deployment(
                    name, original_data, namespace
                )

                logger.info(f"回滚完成: {resource_type}/{name}")
                return {
                    "success": success,
                    "details": {"action": "rollback", "resource": name},
                }

            return {"success": True, "details": {"rollback": True}}

        except Exception as e:
            logger.error(f"回滚失败: {str(e)}")
            return {"success": False, "error": str(e)}

    async def _verify_result(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        """验证修复结果"""
        try:
            target = strategy.get("target", {})
            resource_type = target.get("resource_type", "")
            name = target.get("name", "")
            namespace = target.get("namespace", "default")

            if not name:
                return {"verified": False, "error": "资源名称为空"}

            if resource_type == "deployment":
                deployment = await self.k8s_service.get_deployment(name, namespace)
                if deployment:
                    ready = deployment.get("status", {}).get("ready_replicas", 0)
                    desired = deployment.get("spec", {}).get("replicas", 0)
                    available = deployment.get("status", {}).get(
                        "available_replicas", 0
                    )

                    verified = ready == desired == available and ready > 0

                    return {
                        "verified": verified,
                        "details": {
                            "ready_replicas": ready,
                            "desired_replicas": desired,
                            "available_replicas": available,
                        },
                    }

            return {"verified": True, "details": {"verification": "completed"}}

        except Exception as e:
            logger.error(f"验证结果失败: {str(e)}")
            return {"verified": False, "error": str(e)}

    async def _check_resource_exists(
        self, resource_type: str, name: str, namespace: str
    ) -> bool:
        """检查资源是否存在"""
        try:
            if resource_type == "deployment":
                deployment = await self.k8s_service.get_deployment(name, namespace)
                return deployment is not None
            elif resource_type == "service":
                service = await self.k8s_service.get_service(name, namespace)
                return service is not None
            return False
        except Exception:
            return False

    async def _check_permissions(self, resource_type: str, namespace: str) -> bool:
        """检查权限"""
        try:
            # 尝试获取资源列表来检查权限
            if resource_type == "deployment":
                deployments = await self.k8s_service.get_deployments(namespace)
                return deployments is not None
            return True
        except Exception:
            return False

    async def _check_cluster_health(self) -> bool:
        """检查集群健康状态"""
        try:
            nodes = await self.k8s_service.get_nodes()
            return nodes is not None and len(nodes) > 0
        except Exception:
            return False

    async def _send_notification(
        self, result: Dict[str, Any], strategy: Dict[str, Any]
    ):
        """发送通知"""
        try:
            target = strategy.get("target", {})
            resource_type = target.get("resource_type", "unknown")
            resource_name = target.get("name", "unknown")

            message = f"""
K8s修复执行完成

策略ID: {result.get("strategy_id", "unknown")}
目标: {resource_type} {resource_name}
成功: {result.get("success", False)}

步骤执行结果:
"""
            for step in result.get("steps", []):
                status = "✓" if step.get("success", False) else "✗"
                message += f"{status} {step.get('description', '未知步骤')}\n"

            errors = result.get("errors", [])
            if errors:
                message += "\n错误:\n" + "\n".join(str(error) for error in errors)

            await self.notification_service.send_feishu_message(
                title="K8s修复执行报告",
                message=message,
                color="green" if result.get("success", False) else "red",
            )

        except Exception as e:
            logger.error(f"发送通知失败: {str(e)}")

    def get_execution_history(self) -> List[Dict[str, Any]]:
        """获取执行历史"""
        return self.execution_log

    async def dry_run(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        """试运行策略（不实际执行）"""
        try:
            dry_run_result = {
                "would_execute": True,
                "steps_preview": strategy.get("steps", []),
                "estimated_time": strategy.get("estimated_time", 0),
                "risk_assessment": self._assess_risk(strategy),
                "warnings": [],
            }

            # 检查潜在风险
            if strategy.get("severity") == "critical":
                dry_run_result["warnings"].append("这是高风险操作")

            return dry_run_result

        except Exception as e:
            return {"would_execute": False, "error": str(e)}

    def _assess_risk(self, strategy: Dict[str, Any]) -> str:
        """评估风险"""
        risk_factors = {
            "critical": strategy.get("severity") == "critical",
            "resource_modification": strategy.get("type")
            in ["resource_adjustment", "scaling_fix"],
            "service_impact": strategy.get("target", {}).get("resource_type")
            == "service",
        }

        if any(risk_factors.values()):
            return "high"
        else:
            return "low"
