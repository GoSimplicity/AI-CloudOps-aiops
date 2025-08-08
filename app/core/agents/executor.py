#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
K8s执行Agent - 执行修复操作
Author: AI Assistant
License: Apache 2.0
Description: 执行具体修复操作的Agent
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List

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
            logger.info(f"执行修复策略: {execution_id}")

            result = {
                "execution_id": execution_id,
                "timestamp": datetime.now().isoformat(),
                "strategy_id": strategy.get("id"),
                "target": strategy.get("target"),
                "success": False,
                "steps": [],
                "errors": []
            }

            # 预检查
            if not await self._pre_check(strategy):
                result["errors"].append("预检查失败")
                return result

            # 执行策略步骤
            steps = strategy.get("steps", [])
            for i, step in enumerate(steps):
                step_result = await self._execute_step(step, strategy.get("target", {}))
                result["steps"].append(step_result)
                
                if not step_result["success"]:
                    result["errors"].append(f"步骤 {i+1} 失败: {step_result.get('error', '')}")
                    break

            result["success"] = len(result["errors"]) == 0
            
            # 发送通知
            await self._send_notification(result)
            
            return result

        except Exception as e:
            logger.error(f"执行策略失败: {str(e)}")
            return {
                "execution_id": execution_id,
                "success": False,
                "errors": [f"执行失败: {str(e)}"]
            }

    async def _pre_check(self, strategy: Dict[str, Any]) -> bool:
        """执行前检查"""
        try:
            target = strategy.get("target", {})
            namespace = target.get("namespace", "default")
            
            # 检查集群连接
            if not self.k8s_service.is_healthy():
                logger.error("集群连接不健康")
                return False

            # 检查权限
            if not await self._check_permissions(target.get("resource_type", "deployment"), namespace):
                logger.error("权限检查失败")
                return False

            return True

        except Exception as e:
            logger.error(f"预检查失败: {str(e)}")
            return False

    async def _execute_step(self, step: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
        """执行单个步骤"""
        step_type = step.get("type", "")
        step_result = {
            "type": step_type,
            "success": False,
            "message": "",
            "error": ""
        }

        try:
            if step_type == "check":
                step_result.update(await self._execute_check_step(step, target))
            elif step_type == "modify":
                step_result.update(await self._execute_modify_step(step, target))
            elif step_type == "restart":
                step_result.update(await self._execute_restart_step(step, target))
            elif step_type == "monitor":
                step_result.update(await self._execute_monitor_step(step, target))
            else:
                step_result["error"] = f"未知步骤类型: {step_type}"

        except Exception as e:
            step_result["error"] = str(e)
            logger.error(f"执行步骤失败: {str(e)}")

        return step_result

    async def _execute_check_step(self, step: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
        """执行检查步骤"""
        try:
            name = target.get("name")
            namespace = target.get("namespace", "default")
            
            if target.get("resource_type") == "deployment":
                deployment = await self.k8s_service.get_deployment(name, namespace)
                if deployment:
                    return {"success": True, "message": f"部署 {name} 存在"}
                else:
                    return {"success": False, "error": f"部署 {name} 不存在"}
            
            return {"success": True, "message": "检查完成"}

        except Exception as e:
            return {"success": False, "error": f"检查失败: {str(e)}"}

    async def _execute_modify_step(self, step: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
        """执行修改步骤"""
        try:
            name = target.get("name")
            namespace = target.get("namespace", "default")
            patch = step.get("patch", {})
            
            if not patch:
                return {"success": False, "error": "缺少补丁数据"}

            if target.get("resource_type") == "deployment":
                success = await self.k8s_service.patch_deployment(name, patch, namespace)
                if success:
                    return {"success": True, "message": f"成功修改部署 {name}"}
                else:
                    return {"success": False, "error": f"修改部署 {name} 失败"}
            
            return {"success": False, "error": "不支持的资源类型"}

        except Exception as e:
            return {"success": False, "error": f"修改失败: {str(e)}"}

    async def _execute_restart_step(self, step: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
        """执行重启步骤"""
        try:
            name = target.get("name")
            namespace = target.get("namespace", "default")
            
            if target.get("resource_type") == "deployment":
                # 通过添加重启时间戳来触发重启
                patch = {
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "kubectl.kubernetes.io/restartedAt": datetime.now().isoformat()
                                }
                            }
                        }
                    }
                }
                success = await self.k8s_service.patch_deployment(name, patch, namespace)
                if success:
                    return {"success": True, "message": f"成功重启部署 {name}"}
                else:
                    return {"success": False, "error": f"重启部署 {name} 失败"}
            
            return {"success": False, "error": "不支持的资源类型"}

        except Exception as e:
            return {"success": False, "error": f"重启失败: {str(e)}"}

    async def _execute_monitor_step(self, step: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
        """执行监控步骤"""
        try:
            name = target.get("name")
            namespace = target.get("namespace", "default")
            wait_time = step.get("wait_time", 30)
            
            # 等待指定时间
            await asyncio.sleep(min(wait_time, 60))  # 最多等待60秒
            
            # 检查状态
            if target.get("resource_type") == "deployment":
                pods = await self.k8s_service.get_pods(namespace=namespace, label_selector=f"app={name}")
                healthy_pods = sum(1 for pod in pods if pod.get("status", {}).get("phase") == "Running")
                
                return {
                    "success": True,
                    "message": f"监控完成，{healthy_pods}/{len(pods)} Pod正常运行"
                }
            
            return {"success": True, "message": "监控完成"}

        except Exception as e:
            return {"success": False, "error": f"监控失败: {str(e)}"}

    async def _check_permissions(self, resource_type: str, namespace: str) -> bool:
        """检查权限"""
        try:
            # 简化的权限检查：尝试列出资源
            if resource_type == "deployment":
                self.k8s_service.apps_v1.list_namespaced_deployment(namespace, limit=1)
                return True
            return True
        except Exception as e:
            logger.error(f"权限检查失败: {str(e)}")
            return False

    async def _send_notification(self, result: Dict[str, Any]):
        """发送通知"""
        try:
            status = "成功" if result["success"] else "失败"
            message = f"执行策略{status}: {result.get('strategy_id', 'Unknown')}"
            
            await self.notification_service.send_notification(
                title="K8s修复执行结果",
                message=message,
                level="info" if result["success"] else "error"
            )
        except Exception as e:
            logger.error(f"发送通知失败: {str(e)}")

    async def dry_run(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        """试运行"""
        try:
            logger.info(f"试运行策略: {strategy.get('id')}")
            
            # 模拟执行
            steps = strategy.get("steps", [])
            simulated_steps = []
            
            for i, step in enumerate(steps):
                simulated_steps.append({
                    "step": i + 1,
                    "type": step.get("type"),
                    "action": step.get("action", ""),
                    "will_execute": True,
                    "estimated_time": "5s"
                })
            
            return {
                "dry_run": True,
                "strategy_id": strategy.get("id"),
                "steps": simulated_steps,
                "estimated_duration": f"{len(steps) * 5}s",
                "safe_to_execute": True
            }
            
        except Exception as e:
            logger.error(f"试运行失败: {str(e)}")
            return {
                "dry_run": True,
                "error": f"试运行失败: {str(e)}",
                "safe_to_execute": False
            }

    def get_execution_history(self) -> List[Dict[str, Any]]:
        """获取执行历史"""
        return self.execution_log.copy()

    def clear_execution_history(self):
        """清空执行历史"""
        self.execution_log.clear()
        logger.info("执行历史已清空")