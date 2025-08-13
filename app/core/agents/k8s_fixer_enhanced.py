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
import time
from typing import Any, Dict, List

from app.services.kubernetes import KubernetesService
from app.services.llm import LLMService

logger = logging.getLogger("aiops.k8s_fixer_enhanced")


class EnhancedK8sFixerAgent:
    """增强版Kubernetes智能修复代理"""

    def __init__(self):
        self.k8s_service = KubernetesService()
        self.llm_service = LLMService()
        self.max_retries = 3
        self.retry_delay = 2

        # 问题识别规则
        self.problem_rules = {
            "crash_loop": {"patterns": ["CrashLoopBackOff"], "severity": "high"},
            "probe_failure": {"patterns": ["probe failed", "Unhealthy"], "severity": "medium"},
            "resource_pressure": {"patterns": ["OutOfMemory", "cpu throttling"], "severity": "high"},
            "image_pull": {"patterns": ["ImagePullBackOff", "ErrImagePull"], "severity": "medium"},
            "mount_failure": {"patterns": ["MountVolume.SetUp failed"], "severity": "medium"},
        }

        logger.info("增强版K8s修复代理初始化完成")

    async def analyze_and_fix_deployment(
        self, deployment_name: str, namespace: str, error_description: str
    ) -> str:
        """分析并修复部署问题"""
        try:
            logger.info(f"开始修复部署 {deployment_name}/{namespace}")

            # 收集上下文信息
            context = await self._gather_context(deployment_name, namespace)
            if not context:
                return f"无法获取部署 {deployment_name} 的信息"

            # 检测问题
            problems = await self._detect_problems(context, error_description)
            if not problems:
                return f"部署 {deployment_name} 未发现明显问题"

            # 生成并执行修复方案
            fix_result = await self._execute_fixes(deployment_name, namespace, problems, context)
            
            # 验证修复结果
            verification = await self._verify_fix(deployment_name, namespace)

            return self._format_report(fix_result, verification, problems)

        except Exception as e:
            logger.error(f"修复过程失败: {str(e)}")
            return f"修复失败: {str(e)}"

    async def _gather_context(self, name: str, namespace: str) -> Dict[str, Any]:
        """收集部署上下文信息"""
        try:
            deployment = await self.k8s_service.get_deployment(name, namespace)
            if not deployment:
                return {}

            pods = await self.k8s_service.get_pods_async(
                namespace=namespace, label_selector=f"app={name}"
            )

            events = await self.k8s_service.get_events(
                namespace=namespace, field_selector=f"involvedObject.name={name}", limit=20
            )

            return {
                "deployment": deployment,
                "pods": pods,
                "events": events,
                "timestamp": time.time(),
            }

        except Exception as e:
            logger.error(f"收集上下文信息失败: {str(e)}")
            return {}

    async def _detect_problems(self, context: Dict[str, Any], error_description: str) -> List[Dict[str, Any]]:
        """检测部署问题"""
        problems = []
        try:
            # 检查Pod状态
            pods = context.get("pods", [])
            for pod in pods:
                status = pod.get("status", {})
                
                # 检查CrashLoopBackOff
                for container_status in status.get("container_statuses", []):
                    if container_status.get("state", {}).get("waiting", {}).get("reason") == "CrashLoopBackOff":
                        problems.append({
                            "type": "crash_loop",
                            "severity": "high",
                            "pod": pod.get("metadata", {}).get("name"),
                            "description": "容器持续崩溃重启"
                        })

                # 检查探针失败
                if status.get("phase") != "Running":
                    for condition in status.get("conditions", []):
                        if condition.get("type") == "Ready" and condition.get("status") != "True":
                            problems.append({
                                "type": "probe_failure",
                                "severity": "medium", 
                                "pod": pod.get("metadata", {}).get("name"),
                                "description": "Pod未就绪"
                            })

            # 检查事件信息
            events = context.get("events", [])
            for event in events[-10:]:  # 只看最近10个事件
                reason = event.get("reason", "")
                message = event.get("message", "")
                
                if "Failed" in reason or "Error" in reason:
                    problem_type = "unknown"
                    if "probe" in message.lower() or "health" in message.lower():
                        problem_type = "probe_failure"
                    elif "memory" in message.lower() or "oom" in message.lower():
                        problem_type = "resource_pressure"
                    elif "image" in message.lower():
                        problem_type = "image_pull"
                    
                    problems.append({
                        "type": problem_type,
                        "severity": "medium",
                        "event": reason,
                        "description": message[:100]
                    })

            return problems[:5]  # 限制问题数量

        except Exception as e:
            logger.error(f"问题检测失败: {str(e)}")
            return []

    async def _execute_fixes(
        self, deployment_name: str, namespace: str, problems: List[Dict], context: Dict
    ) -> List[Dict[str, Any]]:
        """执行修复方案"""
        results = []
        deployment = context.get("deployment", {})
        
        for problem in problems:
            try:
                result = {"problem": problem["type"], "success": False, "action": ""}
                
                if problem["type"] == "crash_loop":
                    # 修复CrashLoopBackOff：添加或修复探针
                    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                    if containers:
                        container = containers[0]
                        patch = {
                            "spec": {
                                "template": {
                                    "spec": {
                                        "containers": [{
                                            "name": container.get("name", "app"),
                                            "readinessProbe": {
                                                "httpGet": {"path": "/", "port": 80},
                                                "initialDelaySeconds": 5,
                                                "periodSeconds": 10
                                            }
                                        }]
                                    }
                                }
                            }
                        }
                        success = await self.k8s_service.patch_deployment(deployment_name, patch, namespace)
                        result.update({"success": success, "action": "添加readinessProbe"})
                
                elif problem["type"] == "probe_failure":
                    # 修复探针路径
                    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                    if containers:
                        container = containers[0]
                        patch = {
                            "spec": {
                                "template": {
                                    "spec": {
                                        "containers": [{
                                            "name": container.get("name", "app"),
                                            "livenessProbe": {
                                                "httpGet": {"path": "/", "port": 80},
                                                "periodSeconds": 10,
                                                "failureThreshold": 3
                                            }
                                        }]
                                    }
                                }
                            }
                        }
                        success = await self.k8s_service.patch_deployment(deployment_name, patch, namespace)
                        result.update({"success": success, "action": "修复livenessProbe"})
                
                elif problem["type"] == "resource_pressure":
                    # 调整资源限制
                    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                    if containers:
                        container = containers[0]
                        patch = {
                            "spec": {
                                "template": {
                                    "spec": {
                                        "containers": [{
                                            "name": container.get("name", "app"),
                                            "resources": {
                                                "requests": {"memory": "128Mi", "cpu": "100m"},
                                                "limits": {"memory": "256Mi", "cpu": "200m"}
                                            }
                                        }]
                                    }
                                }
                            }
                        }
                        success = await self.k8s_service.patch_deployment(deployment_name, patch, namespace)
                        result.update({"success": success, "action": "调整资源配置"})

                results.append(result)
                
            except Exception as e:
                logger.error(f"执行修复失败: {str(e)}")
                results.append({
                    "problem": problem["type"], 
                    "success": False, 
                    "action": f"修复失败: {str(e)}"
                })

        return results

    async def _verify_fix(self, deployment_name: str, namespace: str) -> Dict[str, Any]:
        """验证修复结果"""
        try:
            await asyncio.sleep(10)  # 等待修复生效
            
            pods = await self.k8s_service.get_pods_async(
                namespace=namespace, label_selector=f"app={deployment_name}"
            )
            
            if not pods:
                return {"status": "failed", "message": "未找到Pod"}

            healthy_count = 0
            for pod in pods:
                status = pod.get("status", {})
                if status.get("phase") == "Running":
                    for condition in status.get("conditions", []):
                        if condition.get("type") == "Ready" and condition.get("status") == "True":
                            healthy_count += 1
                            break

            total_pods = len(pods)
            success_rate = healthy_count / total_pods if total_pods > 0 else 0

            return {
                "status": "success" if success_rate > 0.5 else "partial",
                "healthy_pods": healthy_count,
                "total_pods": total_pods,
                "success_rate": f"{success_rate:.1%}"
            }

        except Exception as e:
            logger.error(f"验证修复结果失败: {str(e)}")
            return {"status": "failed", "message": f"验证失败: {str(e)}"}

    def _format_report(self, fix_results: List[Dict], verification: Dict, problems: List[Dict]) -> str:
        """格式化修复报告"""
        try:
            report = []
            report.append("🔧 修复报告")
            report.append(f"发现问题: {len(problems)}")
            
            for result in fix_results:
                status = "✅" if result["success"] else "❌"
                report.append(f"{status} {result['problem']}: {result['action']}")

            if verification.get("status") == "success":
                report.append(f"✅ 验证结果: {verification.get('success_rate', 'N/A')} Pod正常运行")
            else:
                report.append(f"⚠️ 验证结果: {verification.get('message', '部分成功')}")

            return "\n".join(report)

        except Exception as e:
            logger.error(f"格式化报告失败: {str(e)}")
            return f"修复完成，但报告生成失败: {str(e)}"

    async def diagnose_deployment_health(self, deployment_name: str, namespace: str) -> str:
        """诊断部署健康状态"""
        try:
            context = await self._gather_context(deployment_name, namespace)
            if not context:
                return f"无法获取部署 {deployment_name} 的信息"

            pods = context.get("pods", [])
            if not pods:
                return f"部署 {deployment_name} 没有运行的Pod"

            healthy = 0
            total = len(pods)
            issues = []

            for pod in pods:
                status = pod.get("status", {})
                pod_name = pod.get("metadata", {}).get("name", "")
                
                if status.get("phase") == "Running":
                    is_ready = any(
                        c.get("type") == "Ready" and c.get("status") == "True"
                        for c in status.get("conditions", [])
                    )
                    if is_ready:
                        healthy += 1
                    else:
                        issues.append(f"Pod {pod_name} 运行但未就绪")
                else:
                    issues.append(f"Pod {pod_name} 状态: {status.get('phase', 'Unknown')}")

            report = [f"部署健康状态: {healthy}/{total} Pod健康"]
            if issues:
                report.append("问题:")
                report.extend(issues[:5])  # 只显示前5个问题

            return "\n".join(report)

        except Exception as e:
            logger.error(f"健康诊断失败: {str(e)}")
            return f"诊断失败: {str(e)}"