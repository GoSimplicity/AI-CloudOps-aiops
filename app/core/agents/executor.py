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
from typing import Any, Dict, List

from app.config.settings import config
from app.services.kubernetes import KubernetesService
from app.services.notification import NotificationService
from app.utils.time_utils import iso_utc_now

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
        "timestamp": iso_utc_now(),
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
            action = step.get("action") or ""
            
            if target.get("resource_type") == "deployment":
                deployment = await self.k8s_service.get_deployment(name, namespace)
                if deployment:
                    # 处理额外检查动作（信息性，不阻断流程）
                    if action == "collect_pod_events":
                        events = await self.k8s_service.get_events(namespace=namespace, field_selector=f"involvedObject.name={name}", limit=20)
                        return {"success": True, "message": f"已收集事件 {len(events)} 条"}
                    if action == "collect_pod_logs":
                        tail = int(step.get("tail_lines", 200))
                        logs = await self.k8s_service.get_recent_pod_logs(namespace=namespace, label_selector=f"app={name}", max_pods=3, tail_lines=tail, include_previous=False)
                        return {"success": True, "message": f"已收集日志 Pod={len(logs)}"}
                    if action == "validate_image_pull":
                        pods = await self.k8s_service.get_pods(namespace=namespace, label_selector=f"app={name}")
                        affected = []
                        for pod in pods or []:
                            pod_name = ((pod.get("metadata", {}) or {}).get("name") or "")
                            spec_containers = ((pod.get("spec", {}) or {}).get("containers") or [])
                            image_of = {c.get("name"): (c.get("image") or "") for c in spec_containers}
                            for cs in ((pod.get("status", {}) or {}).get("container_statuses", []) or []):
                                waiting = ((cs.get("state", {}) or {}).get("waiting", {}) or {})
                                reason = (waiting.get("reason") or "").lower()
                                if reason in ("imagepullbackoff", "errimagepull"):
                                    c_name = cs.get("name")
                                    img = image_of.get(c_name, "")
                                    # 解析镜像：registry/repo:tag
                                    registry_host = ""
                                    repository = img
                                    tag = "latest"
                                    if img:
                                        # 分离 tag
                                        repo_part = img
                                        if ":" in img and not img.endswith(":"):  # 有 tag 或端口
                                            # 取最后一个冒号后若包含/，可能是端口；简单处理：按最后一个冒号分割，再检查后段是否含"/"
                                            last_colon = img.rfind(":")
                                            after = img[last_colon + 1 :]
                                            before = img[:last_colon]
                                            if "/" not in after:
                                                tag = after or tag
                                                repo_part = before
                                        # 分离 registry
                                        if "/" in repo_part:
                                            first_slash = repo_part.find("/")
                                            cand = repo_part[:first_slash]
                                            if any(x in cand for x in (".", ":", "localhost")):
                                                registry_host = cand
                                                repository = repo_part[first_slash + 1 :]
                                            else:
                                                repository = repo_part
                                        else:
                                            repository = repo_part
                                    affected.append({
                                        "pod": pod_name,
                                        "container": c_name,
                                        "image": img,
                                        "registry": registry_host,
                                        "repository": repository,
                                        "tag": tag,
                                        "reason": reason,
                                        "message": (waiting.get("message") or ""),
                                    })
                        if not affected:
                            return {"success": True, "message": "未发现镜像拉取错误"}
                        # 生成建议
                        suggestions = []
                        registries = sorted({a.get("registry") for a in affected if a.get("registry")})
                        if registries:
                            suggestions.append(f"检查镜像仓库连通性与DNS解析：{', '.join(registries)}（网络出口/防火墙/代理）")
                        suggestions.append("为私有仓库配置 imagePullSecrets，并在 ServiceAccount/Pod 上引用")
                        # 汇总repo:tag
                        repos = []
                        for a in affected:
                            repo = a.get("repository") or ""
                            tagv = a.get("tag") or "latest"
                            if repo:
                                repos.append(f"{repo}:{tagv}")
                        if repos:
                            suggestions.append("确认以下镜像与标签已存在于仓库：" + ", ".join(sorted(set(repos))[:5]))
                        suggestions.append("考虑使用镜像 digest（@sha256:...）固定版本，提升可重复性")
                        suggestions.append("根据发布策略设置 imagePullPolicy：latest/频繁更新用 Always；稳定版本用 IfNotPresent")
                        suggestions.append("确认节点/命名空间到镜像仓库的网络出口（NetworkPolicy/安全组/NAT）")
                        suggestions.append("如为自建不安全仓库，配置运行时 insecure registry 或启用 TLS")
                        suggestions.append("可重启 Deployment 以重试镜像拉取（kubectl rollout restart）")
                        return {
                            "success": True,
                            "message": f"发现 {len(affected)} 处镜像拉取错误",
                            "suggestions": suggestions,
                            "details": {"affected": affected[:20]},
                        }
                    if action == "validate_mount":
                        pods = await self.k8s_service.get_pods(namespace=namespace, label_selector=f"app={name}")
                        found = False
                        for pod in pods:
                            status = (pod.get("status", {}) or {})
                            for cs in status.get("container_statuses", []) or []:
                                waiting = (cs.get("state", {}) or {}).get("waiting", {}) or {}
                                msg = (waiting.get("message") or "").lower()
                                if any(k in msg for k in ("mount", "mountvolume", "volume")):
                                    found = True
                                    break
                            if found:
                                break
                        return {"success": True, "message": "可能存在卷挂载问题" if found else "未发现卷挂载问题"}
                    # 默认检查通过
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
                # 根据配置决定是否启用 Dry-Run
                if config.remediation.dry_run:
                    dry_ok = await self.k8s_service.patch_deployment(
                        name, patch, namespace, dry_run=True, field_manager="aiops-executor"
                    )
                    if not dry_ok:
                        return {"success": False, "error": f"Dry-Run 校验未通过: {name}"}

                success = await self.k8s_service.patch_deployment(
                    name, patch, namespace, dry_run=False, field_manager="aiops-executor"
                )
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
                "kubectl.kubernetes.io/restartedAt": iso_utc_now()
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