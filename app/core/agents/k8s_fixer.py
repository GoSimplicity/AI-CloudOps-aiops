#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: Kubernetes自动修复代理
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import requests
import yaml

from app.config.settings import config
from app.core.interfaces.k8s_client import K8sClient, NullK8sClient
from app.core.interfaces.llm_client import LLMClient, NullLLMClient
from app.utils.workload_context import model_workload_context

logger = logging.getLogger("aiops.k8s_fixer")


class K8sFixerAgent:
    """K8s自动修复代理"""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        k8s_client: Optional[K8sClient] = None,
    ):
        self.k8s_service: K8sClient = k8s_client or NullK8sClient()
        self.llm_service: LLMClient = llm_client or NullLLMClient()
        self.llm = self.llm_service  # 保留一个llm属性作为兼容

        # 从配置获取重试参数
        self.max_retries = config.rca.max_retries if hasattr(config, "rca") else 3
        self.retry_delay = (
            getattr(config.common, "retry_delay", 2) if hasattr(config, "common") else 2
        )
        logger.info("K8s Fixer Agent initialized")

        # 延迟检查依赖，避免阻塞初始化
        self._llm_checked = False

    def _test_model_failover(self):
        try:
            # 检查主要服务连通性
            primary_healthy = False
            backup_healthy = False

            # 检查主要提供方
            if config.llm.provider.lower() == "openai":
                try:
                    # 检查连接
                    headers = {"Authorization": f"Bearer {config.llm.api_key}"}
                    response = requests.get(
                        f"{config.llm.base_url.rstrip('/')}/models",
                        headers=headers,
                        timeout=config.llm.request_timeout,
                    )
                    primary_healthy = response.status_code < 400
                    logger.info(
                        f"主要OpenAI模型服务状态: {'可用' if primary_healthy else '不可用'}"
                    )
                except Exception as e:
                    logger.warning(f"无法连接到OpenAI API: {str(e)}")

                # 检查备用提供方
                try:
                    ollama_host = config.llm.ollama_base_url.replace("/v1", "")
                    response = requests.get(
                        f"{ollama_host}/api/tags", timeout=config.llm.request_timeout
                    )
                    backup_healthy = response.status_code == 200
                    logger.info(
                        f"备用Ollama模型服务状态: {'可用' if backup_healthy else '不可用'}"
                    )
                except Exception as e:
                    logger.warning(f"无法连接到Ollama API: {str(e)}")
            else:
                # Ollama作为主要提供方
                try:
                    ollama_host = config.llm.ollama_base_url.replace("/v1", "")
                    response = requests.get(
                        f"{ollama_host}/api/tags", timeout=config.llm.request_timeout
                    )
                    primary_healthy = response.status_code == 200
                    logger.info(
                        f"主要Ollama模型服务状态: {'可用' if primary_healthy else '不可用'}"
                    )
                except Exception as e:
                    logger.warning(f"无法连接到Ollama API: {str(e)}")

                # 检查备用提供方
                try:
                    headers = {"Authorization": f"Bearer {config.llm.api_key}"}
                    response = requests.get(
                        f"{config.llm.base_url.rstrip('/')}/models",
                        headers=headers,
                        timeout=config.llm.request_timeout,
                    )
                    backup_healthy = response.status_code < 400
                    logger.info(
                        f"备用OpenAI模型服务状态: {'可用' if backup_healthy else '不可用'}"
                    )
                except Exception as e:
                    logger.warning(f"无法连接到OpenAI API: {str(e)}")

            # 输出最终状态
            if primary_healthy:
                logger.info("将使用主要LLM模型服务")
            elif backup_healthy:
                logger.info("主要LLM服务不可用，将使用备用LLM模型服务")
            else:
                logger.error("主要和备用LLM模型服务均不可用，自动修复功能可能受限")

        except Exception as e:
            logger.error(f"模型故障切换测试失败: {str(e)}")

    async def analyze_and_fix_deployment(
        self, deployment_name: str, namespace: str, error_description: str
    ) -> str:
        """分析并修复Kubernetes Deployment问题"""
        try:
            logger.info(f"开始分析和修复Deployment: {deployment_name}")

            # 首先检查K8s连接是否正常
            if not await self._check_and_fix_k8s_connection():
                return "无法连接到Kubernetes集群，请检查配置"

            # 获取当前Deployment配置
            deployment = await self.k8s_service.get_deployment(
                deployment_name, namespace
            )
            if not deployment:
                return f"无法获取Deployment {deployment_name} 的配置信息"

            # 获取相关事件
            events = await self.k8s_service.get_events(
                namespace=namespace,
                field_selector=f"involvedObject.name={deployment_name}",
            )

            # 获取Pod状态
            pods = await self.k8s_service.get_pods(
                namespace=namespace, label_selector=f"app={deployment_name}"
            )

            # 通用的确保可序列化的函数
            def ensure_serializable(obj):
                if isinstance(obj, dict):
                    return {k: ensure_serializable(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [ensure_serializable(item) for item in obj]
                elif hasattr(obj, "isoformat"):  # datetime对象
                    return obj.isoformat()
                else:
                    return obj

            # 准备上下文信息，确保可序列化
            context = {
                "deployment": ensure_serializable(deployment),
                "events": ensure_serializable(events[:10]),  # 最近10个事件
                "pods": [
                    ensure_serializable(self._extract_pod_info(pod)) for pod in pods[:5]
                ],  # 最近5个Pod
                "error_description": error_description,
            }

            # 仅模型副本隐藏环境变量和历史完整配置，内部原对象不变
            model_deployment = model_workload_context(deployment)
            model_context = {**context, "deployment": ensure_serializable(model_deployment)}

            # 处理可能的强制修复情况
            force_fix = False
            if (
                "健康检查" in error_description
                or "readinessProbe" in error_description
                or "livenessProbe" in error_description
                or "探针" in error_description
            ):
                force_fix = True
                logger.info("基于错误描述设置强制修复标志")

            # 检查是否有Pod处于CrashLoopBackOff/OOMKilled状态或Unhealthy状态
            has_crashloop = False
            has_oom = False
            for pod in pods or []:
                status = pod.get("status", {})
                container_statuses = status.get("container_statuses", [])
                for c_status in container_statuses:
                    if (
                        c_status.get("state", {}).get("waiting", {}).get("reason")
                        == "CrashLoopBackOff"
                    ):
                        has_crashloop = True
                        logger.info("检测到Pod处于CrashLoopBackOff状态，将强制修复")
                    if (
                        c_status.get("last_state", {})
                        .get("terminated", {})
                        .get("reason")
                        == "OOMKilled"
                    ):
                        has_oom = True
                        logger.info("检测到OOMKilled，将调整内存限制")
                if has_crashloop or has_oom:
                    break

            # 检查事件中是否有Unhealthy/镜像拉取失败
            has_probe_issue = False
            has_image_pull_issue = False
            for event in events or []:
                if (
                    "Unhealthy" in event.get("reason", "")
                    and "probe failed" in event.get("message", "").lower()
                ):
                    has_probe_issue = True
                    logger.info("检测到探针健康检查失败，将强制修复")
                msg = (event.get("message") or "").lower()
                reason = (event.get("reason") or "").lower()
                if any(
                    k in reason for k in ["errimagepull", "imagepullbackoff"]
                ) or any(
                    k in msg
                    for k in [
                        "imagepullbackoff",
                        "back-off pulling image",
                        "failed to pull image",
                    ]
                ):
                    has_image_pull_issue = True
                    logger.info("检测到镜像拉取失败，将尝试回退到已知可用镜像")
                if has_probe_issue or has_image_pull_issue:
                    break

            # 如果有CrashLoopBackOff/Probe/ImagePull问题，强制修复模式
            if has_crashloop or has_probe_issue or has_image_pull_issue or has_oom:
                force_fix = True

            # 尝试识别常见问题并自动修复
            auto_fix_result = await self._identify_and_fix_common_issues(
                deployment, context, force_fix
            )
            if auto_fix_result.get("fixed"):
                # 验证修复结果
                verification_result = await self._verify_fix(deployment_name, namespace)
                if "成功" in verification_result or "就绪Pod" in verification_result:
                    # 返回完整结果以保留 actions_taken 等信息
                    return auto_fix_result
                else:
                    # 如果验证失败，记录但继续尝试其他修复方案
                    logger.warning(f"自动修复验证失败: {verification_result}")
                    # 等待一段时间后再次检查，给K8s一些时间应用更改
                    await asyncio.sleep(self.retry_delay + 1)

            try:
                # 使用分析服务
                analysis = None
                retry_count = 0
                max_retries = 2  # 重试次数

                while analysis is None and retry_count < max_retries:
                    try:
                        logger.info(
                            f"尝试使用LLM分析K8s问题 (尝试 {retry_count + 1}/{max_retries + 1})"
                        )
                        logger.info(
                            f"当前使用模型: {config.llm.provider}:{self.llm_service.model}"
                        )

                        analysis = await self.llm_service.analyze_k8s_problem(
                            yaml.dump(model_deployment, default_flow_style=False),
                            error_description,
                            json.dumps(model_context, indent=2),
                        )

                        if analysis:
                            logger.info(
                                f"成功使用LLM分析问题: {analysis.get('analysis', 'N/A')}"
                            )
                            logger.info(
                                f"使用的模型: {config.llm.provider}:{self.llm_service.model}"
                            )
                        else:
                            retry_count += 1
                            logger.warning(
                                f"分析返回为空，尝试重试 ({retry_count}/{max_retries})"
                            )
                            await asyncio.sleep(self.retry_delay)
                    except Exception as e:
                        retry_count += 1
                        logger.error(f"分析出错: {str(e)}")
                        logger.error(f"尝试重试 ({retry_count}/{max_retries})")

                        # 记录当前使用的模型配置
                        if hasattr(self.llm_service, "provider") and hasattr(
                            self.llm_service, "model"
                        ):
                            logger.info(
                                f"上次尝试使用的模型: {self.llm_service.provider}:{self.llm_service.model}"
                            )

                        # 显式尝试使用备用模型
                        try:
                            if retry_count == 1:  # 第一次失败后，尝试强制使用备用模型
                                backup_provider = (
                                    "ollama"
                                    if config.llm.provider.lower() == "openai"
                                    else "openai"
                                )
                                backup_model = (
                                    config.llm.ollama_model
                                    if backup_provider == "ollama"
                                    else config.llm.model
                                )
                                logger.info(
                                    f"尝试显式切换到备用模型: {backup_provider}:{backup_model}"
                                )

                                # 回退由服务层处理
                        except Exception as backup_e:
                            logger.error(f"切换到备用模型失败: {str(backup_e)}")

                        await asyncio.sleep(self.retry_delay)

                if not analysis:
                    # 分析失败，尝试直接修复
                    if (
                        "健康检查" in error_description
                        or "readinessProbe" in error_description
                    ):
                        logger.info("基于错误描述进行修复，跳过LLM分析")
                        fix_result = await self._identify_and_fix_common_issues(
                            deployment, context, force_fix=True
                        )
                        return fix_result["message"]
                    else:
                        # 尝试最后一次使用备用方案，如果可能的话
                        try:
                            logger.warning("分析失败，最后尝试备用方案进行问题分析")
                            # 构建最简单的分析请求
                            simple_analysis = (
                                await self.llm_service.analyze_k8s_problem(
                                    yaml.dump(model_deployment, default_flow_style=False),
                                    error_description,
                                )
                            )

                            if simple_analysis:
                                logger.info("使用备用模型成功进行简化分析")
                                analysis = simple_analysis
                            else:
                                logger.error("所有分析尝试均失败")
                                return "无法分析问题，建议手动检查Kubernetes配置"
                        except Exception as final_e:
                            logger.error(f"最终分析尝试失败: {str(final_e)}")
                            return "无法分析问题，建议手动检查"

                logger.info(f"问题分析完成: {analysis.get('analysis', 'N/A')}")

                # 执行修复操作
                fix_result = await self._execute_fix(
                    deployment_name, namespace, analysis
                )

                # 验证修复结果
                verification_result = await self._verify_fix(deployment_name, namespace)
                if "失败" in verification_result.lower():
                    logger.warning(f"修复验证失败: {verification_result}")

                return fix_result

            except Exception as e:
                logger.error(f"分析或执行修复时出错: {str(e)}")
                # 如果是nginx相关部署，尝试执行默认修复
                if (
                    deployment_name.lower().startswith("nginx")
                    or "nginx"
                    in deployment.get("spec", {})
                    .get("template", {})
                    .get("spec", {})
                    .get("containers", [{}])[0]
                    .get("image", "")
                    .lower()
                ):
                    logger.info("对nginx相关部署执行默认修复")
                    return await self._identify_and_fix_common_issues(
                        deployment, context, force_fix=True
                    )["message"]
                return f"分析或执行修复失败: {str(e)}"

        except Exception as e:
            logger.error(f"分析和修复Deployment失败: {str(e)}")
            return f"修复失败: {str(e)}"

    async def _check_and_fix_k8s_connection(self) -> bool:
        """只检查服务器配置的目标，不扫描或切换其他集群凭据"""
        return bool(await self.k8s_service.health_check())

    async def _identify_and_fix_common_issues(
        self,
        deployment: Dict[str, Any],
        context: Dict[str, Any],
        force_fix: bool = False,
    ) -> Dict[str, Any]:
        """识别并修复常见的Kubernetes问题"""
        try:
            deployment_name = deployment.get("metadata", {}).get("name", "unknown")
            namespace = deployment.get("metadata", {}).get("namespace", "default")
            pod_template = deployment.get("spec", {}).get("template", {})
            containers = pod_template.get("spec", {}).get("containers", [])

            if not containers:
                return {"fixed": False, "message": "无法找到容器配置"}

            main_container = containers[0]
            issues_found = []
            fixes_applied = []
            patch = {"spec": {"template": {"spec": {"containers": [{}]}}}}
            container_patch = patch["spec"]["template"]["spec"]["containers"][0]
            need_to_patch = False
            is_nginx = (
                deployment_name.lower().startswith("nginx")
                or "nginx" in main_container.get("image", "").lower()
            )

            # 如果是Nginx，添加容器名称
            if is_nginx:
                container_patch["name"] = main_container.get("name", "nginx")

            # 检查是否存在CrashLoopBackOff/OOMKilled问题
            pod_issues = []
            for pod in context.get("pods", []):
                status = pod.get("status", {})
                container_statuses = status.get("container_statuses", [])
                for c_status in container_statuses:
                    if (
                        c_status.get("state", {}).get("waiting", {}).get("reason")
                        == "CrashLoopBackOff"
                    ):
                        pod_issues.append("CrashLoopBackOff")
                    if (
                        c_status.get("last_state", {})
                        .get("terminated", {})
                        .get("reason")
                        == "OOMKilled"
                    ):
                        pod_issues.append("OOMKilled")
                if status.get("phase") != "Running" or not self._is_pod_ready(status):
                    pod_issues.append("NotReady")

            # 特殊处理 nginx-problematic 部署
            if deployment_name == "nginx-problematic":
                logger.info("检测到nginx-problematic部署，应用专门修复")

                # 分析问题并准备修复
                issues_found = []
                fixes_applied = []

                # 检查资源请求和限制
                if "resources" in main_container:
                    resources = main_container["resources"]
                    if "requests" in resources and "memory" in resources["requests"]:
                        memory_request = resources["requests"]["memory"]
                        if memory_request.endswith("Mi"):
                            memory_value = int(memory_request[:-2])
                            if memory_value > 256:
                                issues_found.append(f"内存请求({memory_request})过高")
                                if "resources" not in container_patch:
                                    container_patch["resources"] = {}
                                if "requests" not in container_patch["resources"]:
                                    container_patch["resources"]["requests"] = {}
                                container_patch["resources"]["requests"]["memory"] = (
                                    "128Mi"
                                )
                                fixes_applied.append(
                                    f"将内存请求从{memory_request}降低到128Mi"
                                )
                                need_to_patch = True

                    if "requests" in resources and "cpu" in resources["requests"]:
                        cpu_request = resources["requests"]["cpu"]
                        if cpu_request.endswith("m"):
                            cpu_value = int(cpu_request[:-1])
                            if cpu_value > 300:
                                issues_found.append(f"CPU请求({cpu_request})过高")
                                if "resources" not in container_patch:
                                    container_patch["resources"] = {}
                                if "requests" not in container_patch["resources"]:
                                    container_patch["resources"]["requests"] = {}
                                container_patch["resources"]["requests"]["cpu"] = "200m"
                                fixes_applied.append(
                                    f"将CPU请求从{cpu_request}降低到200m"
                                )
                                need_to_patch = True

                # 检查ReadinessProbe
                if "readinessProbe" in main_container:
                    readiness_probe = main_container["readinessProbe"]

                    # 检查HTTP探针路径
                    if "httpGet" in readiness_probe:
                        path = readiness_probe.get("httpGet", {}).get("path")
                        if path == "/health":
                            issues_found.append("Nginx ReadinessProbe的HTTP路径不正确")
                            if "readinessProbe" not in container_patch:
                                container_patch["readinessProbe"] = {}
                            if "httpGet" not in container_patch["readinessProbe"]:
                                container_patch["readinessProbe"]["httpGet"] = {}
                            container_patch["readinessProbe"]["httpGet"]["path"] = "/"
                            fixes_applied.append(
                                "将Nginx ReadinessProbe的HTTP路径修改为'/'"
                            )
                            need_to_patch = True

                    # 检查探针频率
                    if readiness_probe.get("periodSeconds", 10) < 5:
                        issues_found.append("ReadinessProbe探针频率过高")
                        if "readinessProbe" not in container_patch:
                            container_patch["readinessProbe"] = {}
                        container_patch["readinessProbe"]["periodSeconds"] = 10
                        fixes_applied.append("将ReadinessProbe周期调整为10秒")
                        need_to_patch = True

                    # 检查失败阈值
                    if readiness_probe.get("failureThreshold", 3) < 2:
                        issues_found.append("ReadinessProbe失败阈值过低")
                        if "readinessProbe" not in container_patch:
                            container_patch["readinessProbe"] = {}
                        container_patch["readinessProbe"]["failureThreshold"] = 3
                        fixes_applied.append("将ReadinessProbe失败阈值调整为3")
                        need_to_patch = True

                # 应用修复
                if need_to_patch:
                    # 确保container_patch中包含name字段
                    container_patch["name"] = main_container.get("name", "nginx")

                    # 应用修补
                    patch_result = await self.k8s_service.patch_deployment(
                        deployment_name, patch, namespace
                    )

                    if patch_result:
                        logger.info(f"成功应用修复补丁: {deployment_name}")
                        message = f"""
自动修复 {deployment_name} 完成:
- 发现的问题：{", ".join(issues_found)}
- 执行的操作：{", ".join(fixes_applied)}
                        """
                        return {"fixed": True, "message": message}
                    else:
                        logger.error(f"应用修复补丁失败: {deployment_name}")
                        return {
                            "fixed": False,
                            "message": f"尝试修复{deployment_name}失败，无法应用修补",
                        }
                else:
                    logger.info(f"无需修补: {deployment_name}")
                    return {
                        "fixed": False,
                        "message": f"检查了{deployment_name}，但没有发现需要修复的问题",
                    }

            # 检查CrashLoopBackOff问题
            if "CrashLoopBackOff" in pod_issues:
                # 情况1：有livenessProbe但没有readinessProbe
                if (
                    "livenessProbe" in main_container
                    and "readinessProbe" not in main_container
                ):
                    # 可能是缺少readinessProbe导致的问题，添加默认的readinessProbe
                    issues_found.append("缺少ReadinessProbe")
                    container_patch["readinessProbe"] = {
                        "httpGet": {"path": "/", "port": 80},
                        "initialDelaySeconds": 5,
                        "periodSeconds": 10,
                        "failureThreshold": 3,
                    }
                    fixes_applied.append("添加默认的Nginx ReadinessProbe")
                    need_to_patch = True

                # 情况2：有livenessProbe配置问题，无论是否有readinessProbe
                if "livenessProbe" in main_container:
                    liveness_probe = main_container["livenessProbe"]
                    liveness_issues = []

                    # 检查HTTP探针路径
                    if "httpGet" in liveness_probe:
                        path = liveness_probe.get("httpGet", {}).get("path")
                        if path != "/" and is_nginx:
                            liveness_issues.append("路径错误")
                            if "livenessProbe" not in container_patch:
                                container_patch["livenessProbe"] = {}
                            if "httpGet" not in container_patch["livenessProbe"]:
                                container_patch["livenessProbe"]["httpGet"] = {}
                            container_patch["livenessProbe"]["httpGet"]["path"] = "/"
                            fixes_applied.append("修复livenessProbe路径为/")
                            need_to_patch = True

                    # 检查探针频率
                    if liveness_probe.get("periodSeconds", 10) < 5:
                        liveness_issues.append("探针频率过高")
                        if "livenessProbe" not in container_patch:
                            container_patch["livenessProbe"] = {}
                        container_patch["livenessProbe"]["periodSeconds"] = 10
                        fixes_applied.append("调整livenessProbe周期为10秒")
                        need_to_patch = True

                    # 检查失败阈值
                    if liveness_probe.get("failureThreshold", 3) < 2:
                        liveness_issues.append("失败阈值过低")
                        if "livenessProbe" not in container_patch:
                            container_patch["livenessProbe"] = {}
                        container_patch["livenessProbe"]["failureThreshold"] = 3
                        fixes_applied.append("调整livenessProbe失败阈值为3")
                        need_to_patch = True

                    # 检查初始延迟
                    if liveness_probe.get("initialDelaySeconds", 10) < 5:
                        liveness_issues.append("初始延迟过短")
                        if "livenessProbe" not in container_patch:
                            container_patch["livenessProbe"] = {}
                        container_patch["livenessProbe"]["initialDelaySeconds"] = 10
                        fixes_applied.append("调整livenessProbe初始延迟为10秒")
                        need_to_patch = True

                    if liveness_issues:
                        issues_found.append(
                            f"LivenessProbe配置问题: {', '.join(liveness_issues)}"
                        )
                        logger.info(f"检测到LivenessProbe问题: {liveness_issues}")

                # 情况3：强制修复模式下的通用处理
                if force_fix and is_nginx:
                    # 确保livenessProbe配置正确
                    if "livenessProbe" not in container_patch:
                        container_patch["livenessProbe"] = {}
                    container_patch["livenessProbe"] = {
                        "httpGet": {"path": "/", "port": 80},
                        "initialDelaySeconds": 10,
                        "periodSeconds": 10,
                        "failureThreshold": 3,
                    }
                    if "LivenessProbe配置问题" not in issues_found:
                        issues_found.append("LivenessProbe配置问题")
                    fixes_applied.append("重置livenessProbe为默认安全配置")
                    need_to_patch = True

            # 事件中检测镜像拉取问题
            events = context.get("events", [])
            has_image_pull_issue = False
            for ev in events:
                reason = (ev.get("reason") or "").lower()
                msg = (ev.get("message") or "").lower()
                if any(
                    k in reason for k in ["errimagepull", "imagepullbackoff"]
                ) or any(
                    k in msg
                    for k in [
                        "imagepullbackoff",
                        "back-off pulling image",
                        "failed to pull image",
                    ]
                ):
                    has_image_pull_issue = True
                    break

            # 如果Deployment级事件未包含镜像拉取失败，但上层传入的错误描述包含提示，则也认为是镜像拉取问题
            if not has_image_pull_issue:
                error_hint = (context.get("error_description") or "").lower()
                if any(
                    k in error_hint
                    for k in [
                        "imagepullbackoff",
                        "errimagepull",
                        "image pull",
                        "failed to pull image",
                        "back-off pulling image",
                    ]
                ):
                    has_image_pull_issue = True

            # 镜像拉取失败时，回退到通用可用镜像
            if has_image_pull_issue:
                # 使用用户提供的稳定镜像作为回退镜像
                fallback_image = "nginx:1.21.6"
                current_image = main_container.get("image")
                if current_image != fallback_image:
                    issues_found.append("镜像拉取失败")
                    container_patch["image"] = fallback_image
                    fixes_applied.append(
                        f"将镜像从 {current_image} 回退为 {fallback_image}"
                    )
                    # 降低拉取策略，避免频繁拉取
                    container_patch["imagePullPolicy"] = "IfNotPresent"
                    need_to_patch = True

            # OOMKilled 时提高内存限制
            if "OOMKilled" in pod_issues:
                resources = main_container.get("resources", {})
                limits = resources.get("limits", {})
                mem_limit = limits.get("memory", "")
                try:
                    mem_value = (
                        int(mem_limit[:-2]) if mem_limit.endswith("Mi") else None
                    )
                except Exception:
                    mem_value = None
                if mem_value is None or mem_value < 256:
                    if "resources" not in container_patch:
                        container_patch["resources"] = {}
                    if "limits" not in container_patch["resources"]:
                        container_patch["resources"]["limits"] = {}
                    container_patch["resources"]["limits"]["memory"] = "256Mi"
                    issues_found.append("检测到OOMKilled")
                    fixes_applied.append("提升内存限制至256Mi 以缓解OOM")
                    need_to_patch = True

            # 检查ReadinessProbe
            if "readinessProbe" in main_container:
                probe = main_container["readinessProbe"]

                # 检查失败阈值过低
                if probe.get("failureThreshold", 3) < 2:
                    issues_found.append("ReadinessProbe失败阈值过低")
                    if "readinessProbe" not in container_patch:
                        container_patch["readinessProbe"] = {}
                    container_patch["readinessProbe"]["failureThreshold"] = 3
                    fixes_applied.append("将ReadinessProbe失败阈值调整为3")
                    need_to_patch = True

                # 检查探针频率过高
                if probe.get("periodSeconds", 10) < 5:
                    issues_found.append("ReadinessProbe探针频率过高")
                    if "readinessProbe" not in container_patch:
                        container_patch["readinessProbe"] = {}
                    container_patch["readinessProbe"]["periodSeconds"] = 10
                    fixes_applied.append("将ReadinessProbe周期调整为10秒")
                    need_to_patch = True

                # 检查初始延迟
                if probe.get("initialDelaySeconds", 5) < 5:
                    issues_found.append("ReadinessProbe初始延迟过短")
                    if "readinessProbe" not in container_patch:
                        container_patch["readinessProbe"] = {}
                    container_patch["readinessProbe"]["initialDelaySeconds"] = 5
                    fixes_applied.append("将ReadinessProbe初始延迟调整为5秒")
                    need_to_patch = True

                # 检查HTTP探针路径
                if "httpGet" in probe:
                    path = probe.get("httpGet", {}).get("path")
                    if path == "/health" and is_nginx:
                        # Nginx默认页面是/，/health需要额外配置
                        issues_found.append("Nginx ReadinessProbe的HTTP路径不正确")
                        if "readinessProbe" not in container_patch:
                            container_patch["readinessProbe"] = {}
                        if "httpGet" not in container_patch["readinessProbe"]:
                            container_patch["readinessProbe"]["httpGet"] = {}
                        container_patch["readinessProbe"]["httpGet"]["path"] = "/"
                        fixes_applied.append(
                            "将Nginx ReadinessProbe的HTTP路径修改为'/'"
                        )
                        need_to_patch = True

            # 检查LivenessProbe
            if "livenessProbe" in main_container:
                probe = main_container["livenessProbe"]

                # 检查失败阈值过低
                if probe.get("failureThreshold", 3) < 2:
                    issues_found.append("LivenessProbe失败阈值过低")
                    if "livenessProbe" not in container_patch:
                        container_patch["livenessProbe"] = {}
                    container_patch["livenessProbe"]["failureThreshold"] = 3
                    fixes_applied.append("将LivenessProbe失败阈值调整为3")
                    need_to_patch = True

                # 检查探针频率过高
                if probe.get("periodSeconds", 10) < 5:
                    issues_found.append("LivenessProbe探针频率过高")
                    if "livenessProbe" not in container_patch:
                        container_patch["livenessProbe"] = {}
                    container_patch["livenessProbe"]["periodSeconds"] = 10
                    fixes_applied.append("将LivenessProbe周期调整为10秒")
                    need_to_patch = True

                # 检查初始延迟
                if probe.get("initialDelaySeconds", 10) < 5:
                    issues_found.append("LivenessProbe初始延迟过短")
                    if "livenessProbe" not in container_patch:
                        container_patch["livenessProbe"] = {}
                    container_patch["livenessProbe"]["initialDelaySeconds"] = 10
                    fixes_applied.append("将LivenessProbe初始延迟调整为10秒")
                    need_to_patch = True

                # 检查HTTP探针路径
                if "httpGet" in probe:
                    path = probe.get("httpGet", {}).get("path")
                    if (
                        path == "/nonexistent"
                        or path == "/health"
                        or path == "/healthz"
                    ) and is_nginx:
                        # Nginx默认页面是/，其他路径需要额外配置
                        issues_found.append("Nginx LivenessProbe的HTTP路径不正确")
                        if "livenessProbe" not in container_patch:
                            container_patch["livenessProbe"] = {}
                        if "httpGet" not in container_patch["livenessProbe"]:
                            container_patch["livenessProbe"]["httpGet"] = {}
                        container_patch["livenessProbe"]["httpGet"]["path"] = "/"
                        fixes_applied.append("将LivenessProbe路径调整为/")
                        need_to_patch = True
            elif is_nginx and (force_fix or "NotReady" in pod_issues):
                # 如果是Nginx但没有LivenessProbe，添加一个
                container_patch["livenessProbe"] = {
                    "httpGet": {"path": "/", "port": 80},
                    "initialDelaySeconds": 10,
                    "periodSeconds": 10,
                    "failureThreshold": 3,
                }
                fixes_applied.append("添加默认的Nginx LivenessProbe")
                need_to_patch = True
                issues_found.append("缺少LivenessProbe")

            # 如果是Nginx但没有ReadinessProbe，添加一个
            if (
                is_nginx
                and "readinessProbe" not in main_container
                and (force_fix or "NotReady" in pod_issues)
            ):
                container_patch["readinessProbe"] = {
                    "httpGet": {"path": "/", "port": 80},
                    "initialDelaySeconds": 5,
                    "periodSeconds": 10,
                    "failureThreshold": 3,
                }
                fixes_applied.append("添加默认的Nginx ReadinessProbe")
                need_to_patch = True
                issues_found.append("缺少ReadinessProbe")

            # 检查资源请求和限制（包含OOM线索时进一步降低限制并提高内存）
            if "resources" in main_container:
                resources = main_container["resources"]

                # 检查内存请求
                if "requests" in resources and "memory" in resources["requests"]:
                    memory_request = resources["requests"]["memory"]
                    if memory_request.endswith("Mi"):
                        memory_value = int(memory_request[:-2])
                        if memory_value > 256:  # 假设超过256Mi就认为可能过高
                            issues_found.append(f"内存请求({memory_request})过高")
                            if "resources" not in container_patch:
                                container_patch["resources"] = {}
                            if "requests" not in container_patch["resources"]:
                                container_patch["resources"]["requests"] = {}
                            container_patch["resources"]["requests"]["memory"] = "128Mi"
                            fixes_applied.append(
                                f"将内存请求从{memory_request}降低到128Mi"
                            )
                            need_to_patch = True
                        # 如果发生OOMKilled，尝试提升limits.memory
                        if "limits" in resources and "memory" in resources["limits"]:
                            mem_limit = resources["limits"]["memory"]
                            if mem_limit.endswith("Mi"):
                                mem_limit_value = int(mem_limit[:-2])
                                if mem_limit_value <= 128 and any(
                                    e.get("reason", "").lower() in ["oomkilled", "oom"]
                                    for e in []
                                ):
                                    if "resources" not in container_patch:
                                        container_patch["resources"] = {}
                                    if "limits" not in container_patch["resources"]:
                                        container_patch["resources"]["limits"] = {}
                                    container_patch["resources"]["limits"]["memory"] = (
                                        "256Mi"
                                    )
                                    fixes_applied.append(
                                        "检测到OOMKilled，提高内存限制至256Mi"
                                    )
                                    need_to_patch = True

                # 检查CPU请求
                if "requests" in resources and "cpu" in resources["requests"]:
                    cpu_request = resources["requests"]["cpu"]
                    if cpu_request.endswith("m"):
                        cpu_value = int(cpu_request[:-1])
                        if cpu_value > 300:  # 假设超过300m就认为可能过高
                            issues_found.append(f"CPU请求({cpu_request})过高")
                            if "resources" not in container_patch:
                                container_patch["resources"] = {}
                            if "requests" not in container_patch["resources"]:
                                container_patch["resources"]["requests"] = {}
                            container_patch["resources"]["requests"]["cpu"] = "200m"
                            fixes_applied.append(f"将CPU请求从{cpu_request}降低到200m")
                            need_to_patch = True

            # 应用修复
            if need_to_patch:
                # 确保container_patch中包含name字段
                container_patch["name"] = main_container.get("name", "nginx")

                # 应用修补
                patch_result = await self.k8s_service.patch_deployment(
                    deployment_name, patch, namespace
                )

                if patch_result:
                    logger.info(f"成功应用修复补丁: {deployment_name}")
                    message = f"""
自动修复 {deployment_name} 完成:
- 发现的问题：{", ".join(issues_found)}
- 执行的操作：{", ".join(fixes_applied)}
                    """
                    return {
                        "fixed": True,
                        "message": message,
                        "actions_taken": fixes_applied,
                    }
                else:
                    logger.error(f"应用修复补丁失败: {deployment_name}")
                    return {
                        "fixed": False,
                        "message": f"尝试修复{deployment_name}失败，无法应用修补",
                        "actions_taken": fixes_applied,
                    }
            else:
                # 即使没有需要修补的内容，也返回有意义的结果
                if issues_found:
                    logger.info(f"发现问题但无法自动修复: {issues_found}")
                    return {
                        "fixed": False,
                        "message": f"检测到以下问题但未应用自动修复：{', '.join(issues_found)}\n请参考Kubernetes文档或联系管理员手动修复。",
                        "actions_taken": fixes_applied,
                    }
                else:
                    return {
                        "fixed": False,
                        "message": f"未发现 {deployment_name} 的常见问题",
                        "actions_taken": [],
                    }

        except Exception as e:
            logger.error(f"识别和修复常见问题失败: {str(e)}")
            return {"fixed": False, "message": f"修复失败: {str(e)}"}

    async def _execute_fix(
        self, deployment_name: str, namespace: str, analysis: Dict[str, Any]
    ) -> str:
        """执行修复操作"""
        try:
            actions_taken = []

            # 检查分析结果
            action = analysis.get("action")
            if not action:
                return "分析未提供修复操作"

            # 解析修复建议
            if "修改资源限制" in action or "修改资源请求" in action:
                logger.info("执行资源配置修复")

                # 准备资源补丁
                resources_patch = {
                    "spec": {"template": {"spec": {"containers": [{"resources": {}}]}}}
                }
                resources = resources_patch["spec"]["template"]["spec"]["containers"][
                    0
                ]["resources"]

                # 解析资源建议
                if "requests" in analysis:
                    resources["requests"] = {}
                    if "cpu" in analysis["requests"]:
                        resources["requests"]["cpu"] = analysis["requests"]["cpu"]
                        actions_taken.append(
                            f"设置CPU请求: {analysis['requests']['cpu']}"
                        )

                    if "memory" in analysis["requests"]:
                        resources["requests"]["memory"] = analysis["requests"]["memory"]
                        actions_taken.append(
                            f"设置内存请求: {analysis['requests']['memory']}"
                        )

                if "limits" in analysis:
                    resources["limits"] = {}
                    if "cpu" in analysis["limits"]:
                        resources["limits"]["cpu"] = analysis["limits"]["cpu"]
                        actions_taken.append(
                            f"设置CPU限制: {analysis['limits']['cpu']}"
                        )

                    if "memory" in analysis["limits"]:
                        resources["limits"]["memory"] = analysis["limits"]["memory"]
                        actions_taken.append(
                            f"设置内存限制: {analysis['limits']['memory']}"
                        )

                # 应用补丁
                success = await self.k8s_service.patch_deployment(
                    deployment_name, resources_patch, namespace
                )

                if success:
                    logger.info(f"成功修改资源配置: {deployment_name}")
                else:
                    logger.error(f"修改资源配置失败: {deployment_name}")
                    return "修改资源配置失败"

            elif (
                "修改健康检查" in action
                or "修改readinessProbe" in action
                or "修改livenessProbe" in action
            ):
                logger.info("执行健康检查配置修复")

                # 检查是修改readinessProbe还是livenessProbe
                probe_type = "readinessProbe"
                if "修改livenessProbe" in action:
                    probe_type = "livenessProbe"

                # 准备健康检查补丁
                probe_patch = {
                    "spec": {"template": {"spec": {"containers": [{probe_type: {}}]}}}
                }
                probe = probe_patch["spec"]["template"]["spec"]["containers"][0][
                    probe_type
                ]

                # 解析健康检查建议
                if "httpGet" in analysis:
                    probe["httpGet"] = {}
                    if "path" in analysis["httpGet"]:
                        probe["httpGet"]["path"] = analysis["httpGet"]["path"]
                        actions_taken.append(
                            f"修改HTTP检查路径: {analysis['httpGet']['path']}"
                        )

                    if "port" in analysis["httpGet"]:
                        probe["httpGet"]["port"] = int(analysis["httpGet"]["port"])
                        actions_taken.append(
                            f"修改HTTP检查端口: {analysis['httpGet']['port']}"
                        )

                if "periodSeconds" in analysis:
                    probe["periodSeconds"] = int(analysis["periodSeconds"])
                    actions_taken.append(f"修改检查周期: {analysis['periodSeconds']}秒")

                if "failureThreshold" in analysis:
                    probe["failureThreshold"] = int(analysis["failureThreshold"])
                    actions_taken.append(
                        f"修改失败阈值: {analysis['failureThreshold']}"
                    )

                # 应用补丁
                success = await self.k8s_service.patch_deployment(
                    deployment_name, probe_patch, namespace
                )

                if success:
                    logger.info(f"成功修改健康检查配置: {deployment_name}")
                else:
                    logger.error(f"修改健康检查配置失败: {deployment_name}")
                    return "修改健康检查配置失败"

            elif "重启部署" in action:
                logger.info("执行重启操作")

                # 执行重启
                success = await self.k8s_service.restart_deployment(
                    deployment_name, namespace
                )

                if success:
                    logger.info(f"成功重启部署: {deployment_name}")
                    actions_taken.append("重启部署")
                else:
                    logger.error(f"重启部署失败: {deployment_name}")
                    return "重启部署失败"

            elif "扩展建议" in analysis:
                # 执行其他建议的操作
                additional_action = await self._execute_additional_action(
                    deployment_name, namespace, analysis.get("扩展建议")
                )

                if additional_action:
                    actions_taken.append(additional_action)

            # 验证修复效果
            verification = await self._verify_fix(deployment_name, namespace)

            # 构建结果消息
            if actions_taken:
                result = f"""
自动修复完成：
- 部署: {deployment_name}
- 命名空间: {namespace}
- 执行的操作: {"; ".join(actions_taken)}
- 验证结果: {verification}
                """
            else:
                result = f"未执行任何修复操作，建议手动检查: {deployment_name}"

            return result

        except Exception as e:
            logger.error(f"执行修复操作失败: {str(e)}")
            return f"执行修复操作失败: {str(e)}"

    async def _execute_additional_action(
        self, deployment_name: str, namespace: str, action: str
    ) -> str:
        """执行额外的修复操作"""
        try:
            if not action:
                return ""

            if "扩展副本" in action and "replicas" in action:
                # 解析副本数
                import re

                replicas_match = re.search(r"(\d+)", action)
                if replicas_match:
                    replicas = int(replicas_match.group(1))
                    success = await self.k8s_service.scale_deployment(
                        deployment_name, replicas, namespace
                    )

                    if success:
                        return f"扩展部署到{replicas}个副本"

            return f"执行额外操作: {action}"

        except Exception as e:
            logger.error(f"执行额外操作失败: {str(e)}")
            return f"执行额外操作失败: {str(e)}"

    async def _verify_fix(self, deployment_name: str, namespace: str) -> str:
        """验证修复效果"""
        try:
            # 等待短暂时间以便修改生效
            import asyncio

            await asyncio.sleep(self.retry_delay + 1)

            # 重试多次检查
            retries = 3
            for attempt in range(retries):
                # 获取最新部署状态
                deployment = await self.k8s_service.get_deployment(
                    deployment_name, namespace
                )
                if not deployment:
                    logger.warning(
                        f"验证修复时无法获取部署: {deployment_name} (尝试 {attempt + 1}/{retries})"
                    )
                    if attempt < retries - 1:
                        await asyncio.sleep(self.retry_delay)
                        continue
                    return "无法获取部署状态"

                # 获取最新Pod状态
                pods = await self.k8s_service.get_pods(
                    namespace=namespace, label_selector=f"app={deployment_name}"
                )

                # 检查Pod状态
                ready_pods = 0
                total_pods = len(pods)

                # 检查是否有CrashLoopBackOff状态
                has_crash_loop = False
                crash_loop_pods = []

                for pod in pods:
                    status = pod.get("status", {})
                    pod_name = pod.get("metadata", {}).get("name", "unknown")

                    # 检查Pod状态
                    if self._is_pod_ready(status):
                        ready_pods += 1

                    # 检查CrashLoopBackOff
                    container_statuses = status.get("container_statuses", [])
                    for c_status in container_statuses:
                        if (
                            c_status.get("state", {}).get("waiting", {}).get("reason")
                            == "CrashLoopBackOff"
                        ):
                            has_crash_loop = True
                            crash_loop_pods.append(pod_name)

                # 检查是否有足够的Pod已就绪
                if total_pods == 0:
                    if attempt < retries - 1:
                        logger.info(
                            f"未找到相关Pod，等待后重试 (尝试 {attempt + 1}/{retries})"
                        )
                        await asyncio.sleep(self.retry_delay)
                        continue
                    return "未找到相关Pod"

                # 计算就绪比例
                readiness_percentage = (ready_pods / total_pods) * 100

                # 如果所有Pod都已就绪，验证成功
                if ready_pods == total_pods:
                    logger.info(
                        f"验证成功: 所有Pod ({ready_pods}/{total_pods}) 均已就绪"
                    )
                    return f"修复成功: 所有Pod ({ready_pods}/{total_pods}) 均已就绪"

                # 如果大部分Pod已就绪，算部分成功
                if readiness_percentage >= 50:
                    if attempt < retries - 1:
                        logger.info(
                            f"部分Pod已就绪 ({ready_pods}/{total_pods})，等待后重试 (尝试 {attempt + 1}/{retries})"
                        )
                        await asyncio.sleep(self.retry_delay + 1)
                        continue
                    return f"部分修复: 就绪Pod: {ready_pods}/{total_pods} ({readiness_percentage:.1f}%)"

                # 如果有CrashLoopBackOff问题，验证失败
                if has_crash_loop:
                    if attempt < retries - 1:
                        logger.warning(
                            f"检测到CrashLoopBackOff状态，等待后重试 (尝试 {attempt + 1}/{retries})"
                        )
                        await asyncio.sleep(self.retry_delay + 1)
                        continue
                    return f"验证失败: {len(crash_loop_pods)}/{total_pods} 的Pod处于CrashLoopBackOff状态"

                # 继续等待更多Pod就绪
                if attempt < retries - 1:
                    logger.info(
                        f"当前就绪Pod: {ready_pods}/{total_pods}，等待后重试 (尝试 {attempt + 1}/{retries})"
                    )
                    await asyncio.sleep(self.retry_delay + 1)
                    continue

            # 返回最终状态
            return f"就绪Pod: {ready_pods}/{total_pods} ({readiness_percentage:.1f}%)"

        except Exception as e:
            logger.error(f"验证修复效果失败: {str(e)}")
            return f"验证失败: {str(e)}"

    def _extract_pod_info(self, pod: Dict[str, Any]) -> Dict[str, Any]:
        """提取Pod信息"""
        status = pod.get("status", {})

        return {
            "name": pod.get("metadata", {}).get("name", ""),
            "ready": self._is_pod_ready(status),
            "phase": status.get("phase", ""),
            "restart_count": self._get_restart_count(status),
            "creation_timestamp": pod.get("metadata", {}).get("creation_timestamp"),
        }

    def _is_pod_ready(self, status: Dict[str, Any]) -> bool:
        """检查Pod是否就绪"""
        conditions = status.get("conditions", [])
        for condition in conditions:
            if condition.get("type") == "Ready":
                return condition.get("status") == "True"
        return False

    def _get_restart_count(self, status: Dict[str, Any]) -> int:
        """获取Pod重启次数"""
        container_statuses = status.get("container_statuses", [])
        if container_statuses:
            return container_statuses[0].get("restart_count", 0)
        return 0

    async def diagnose_cluster_health(self, namespace: str = None) -> str:
        """诊断集群健康状态"""
        try:
            # 首先检查K8s连接
            if not await self._check_and_fix_k8s_connection():
                return "无法连接到Kubernetes集群，请检查配置"

            # 获取当前命名空间下的所有部署
            namespace = namespace or config.k8s.namespace

            # 1. 检查节点状态
            nodes_status = "无法获取节点状态"
            try:
                nodes = await asyncio.to_thread(self.k8s_service.core_v1.list_node, _request_timeout=(3, 10))
                ready_nodes = 0
                total_nodes = len(nodes.items)

                for node in nodes.items:
                    conditions = node.status.conditions
                    for condition in conditions:
                        if condition.type == "Ready" and condition.status == "True":
                            ready_nodes += 1
                            break

                nodes_status = f"节点: {ready_nodes}/{total_nodes} 就绪"
            except Exception as e:
                logger.error(f"获取节点状态失败: {str(e)}")

            # 2. 获取Pod状态
            pods_status = "无法获取Pod状态"
            try:
                pods = await self.k8s_service.get_pods(namespace=namespace)
                total_pods = len(pods)
                running_pods = 0
                problematic_pods = []

                for pod in pods:
                    status = pod.get("status", {})
                    phase = status.get("phase", "")
                    pod_name = pod.get("metadata", {}).get("name", "")

                    if phase == "Running" and self._is_pod_ready(status):
                        running_pods += 1
                    elif phase != "Succeeded":  # 不计算已经成功完成的Job
                        problematic_pods.append(
                            {
                                "name": pod_name,
                                "phase": phase,
                                "restart_count": self._get_restart_count(status),
                            }
                        )

                pods_status = f"Pod: {running_pods}/{total_pods} 运行中"
                if problematic_pods:
                    pods_status += "\n问题Pod列表:\n"
                    for pod in problematic_pods[:5]:  # 只显示前5个
                        pods_status += f"- {pod['name']}: 状态={pod['phase']}, 重启次数={pod['restart_count']}\n"

                    if len(problematic_pods) > 5:
                        pods_status += (
                            f"...以及其他 {len(problematic_pods) - 5} 个问题Pod"
                        )
            except Exception as e:
                logger.error(f"获取Pod状态失败: {str(e)}")

            # 3. 获取Deployment状态
            deployments_status = "无法获取部署状态"
            try:
                deployments = await asyncio.to_thread(
                    self.k8s_service.apps_v1.list_namespaced_deployment,
                    _request_timeout=(3, 10),
                    namespace=namespace
                )
                total_deployments = len(deployments.items)
                healthy_deployments = 0
                problematic_deployments = []

                for deployment in deployments.items:
                    available_replicas = deployment.status.available_replicas or 0
                    replicas = deployment.spec.replicas

                    if available_replicas == replicas:
                        healthy_deployments += 1
                    else:
                        problematic_deployments.append(
                            {
                                "name": deployment.metadata.name,
                                "available": available_replicas,
                                "desired": replicas,
                            }
                        )

                deployments_status = (
                    f"部署: {healthy_deployments}/{total_deployments} 健康"
                )
                if problematic_deployments:
                    deployments_status += "\n问题部署列表:\n"
                    for d in problematic_deployments:
                        deployments_status += (
                            f"- {d['name']}: {d['available']}/{d['desired']} 副本就绪\n"
                        )
            except Exception as e:
                logger.error(f"获取部署状态失败: {str(e)}")

            # 4. 获取最近事件
            events_info = "无法获取事件信息"
            try:
                events = await self.k8s_service.get_events(
                    namespace=namespace, limit=10
                )

                if events:
                    warning_events = []
                    for event in events:
                        if event.get("type") == "Warning":
                            warning_events.append(
                                {
                                    "reason": event.get("reason", ""),
                                    "message": event.get("message", "")[
                                        :100
                                    ],  # 截取前100个字符
                                    "object": event.get("involved_object", {}).get(
                                        "name", ""
                                    ),
                                }
                            )

                    if warning_events:
                        events_info = "最近警告事件:\n"
                        for event in warning_events[:5]:  # 只显示前5个
                            events_info += f"- {event['object']}: {event['reason']} - {event['message']}\n"
                    else:
                        events_info = "没有发现警告事件"
                else:
                    events_info = "没有最近事件"
            except Exception as e:
                logger.error(f"获取事件信息失败: {str(e)}")

            # 汇总诊断结果
            diagnosis = f"""
集群健康诊断 (命名空间: {namespace}):
=================================
{nodes_status}
{pods_status}
{deployments_status}
{events_info}
=================================
诊断建议:
{
                self._generate_diagnosis_recommendations(
                    problematic_pods if "problematic_pods" in locals() else [],
                    problematic_deployments
                    if "problematic_deployments" in locals()
                    else [],
                )
            }
"""

            return diagnosis

        except Exception as e:
            logger.error(f"诊断集群健康失败: {str(e)}")
            return f"诊断集群健康失败: {str(e)}"

    def _generate_diagnosis_recommendations(
        self, problematic_pods, problematic_deployments
    ) -> str:
        """生成诊断建议"""
        if not problematic_pods and not problematic_deployments:
            return "集群状态良好，无需采取措施。"

        recommendations = []

        # 针对问题Pod的建议
        if problematic_pods:
            for pod in problematic_pods:
                if pod.get("restart_count", 0) > 5:
                    recommendations.append(
                        f"检查Pod {pod['name']} 频繁重启的原因，可能是资源不足或者健康检查配置不当。"
                    )
                elif pod.get("phase") == "Pending":
                    recommendations.append(
                        f"Pod {pod['name']} 处于Pending状态，可能是资源不足或者PVC问题。"
                    )
                elif pod.get("phase") == "Failed":
                    recommendations.append(
                        f"检查Pod {pod['name']} 失败的原因，查看日志获取更多信息。"
                    )

        # 针对问题Deployment的建议
        if problematic_deployments:
            for deployment in problematic_deployments:
                if deployment.get("available", 0) == 0:
                    recommendations.append(
                        f"部署 {deployment['name']} 没有可用副本，可能需要检查Pod日志和事件。"
                    )
                elif deployment.get("available", 0) < deployment.get("desired", 0):
                    recommendations.append(
                        f"部署 {deployment['name']} 的可用副本少于期望副本，建议使用'kubectl describe'和日志查看详情。"
                    )

        if not recommendations:
            recommendations.append(
                "尽管有一些问题，但没有明确的修复建议。请检查集群事件和日志获取更多信息。"
            )

        return "\n".join([f"{i + 1}. {rec}" for i, rec in enumerate(recommendations)])

    def get_available_tools(self) -> List[str]:
        """获取可用工具列表"""
        return [
            "analyze_deployment",
            "fix_deployment",
            "restart_deployment",
            "scale_deployment",
            "diagnose_cluster",
        ]

    async def process_agent_state(self, state) -> Any:
        """处理Agent状态，支持工作流处理

        Args:
            state: 工作流状态对象 (AgentState)

        Returns:
            更新后的AgentState对象
        """
        try:
            from dataclasses import replace

            # 获取状态上下文信息（确保是字典副本）
            context = dict(state.context) if state.context else {}

            # 获取部署信息
            deployment = context.get("deployment")
            namespace = context.get("namespace", "default")
            problem = context.get("problem", "")
            # 强制修复标志，默认为False
            context.get("force", False)

            if not deployment:
                logger.warning("没有指定部署名称，无法进行修复")
                context["error"] = "没有指定部署名称，无法进行修复"
                return replace(state, context=context)

            logger.info(
                f"K8sFixer开始修复: {deployment}/{namespace}, 问题描述: {problem[:50]}..."
            )

            # 记录提供方状态，便于诊断
            is_llm_healthy = await self.llm_service.is_healthy()
            logger.info(f"LLM服务状态: {'健康' if is_llm_healthy else '异常'}")
            logger.info(
                f"当前LLM提供商: {self.llm_service.provider}, 模型: {self.llm_service.model}"
            )

            # 执行修复
            try:
                fix_result = await self.analyze_and_fix_deployment(
                    deployment, namespace, problem
                )
            except Exception as fix_e:
                logger.error(f"修复过程发生错误: {str(fix_e)}")
                fix_result = f"修复过程发生错误: {str(fix_e)}"

            # 更新上下文
            context["result"] = fix_result
            context["success"] = (
                True
                if any(
                    term in str(fix_result).lower()
                    for term in ["自动修复完成", "成功", "已修复"]
                )
                else False
            )

            # 添加操作记录
            if "actions_taken" not in context or not isinstance(
                context["actions_taken"], list
            ):
                context["actions_taken"] = []
            context["actions_taken"].append(f"K8sFixer修复: {deployment}/{namespace}")

            # 如果修复失败
            if not context.get("success", False):
                context["error"] = "修复失败，请查看结果详情"
                logger.warning(f"修复结果显示失败: {fix_result[:100]}...")
            else:
                logger.info(f"修复成功: {fix_result[:100]}...")

            # 返回更新后的状态
            return replace(state, context=context)

        except Exception as e:
            logger.error(f"K8sFixer处理状态失败: {str(e)}")
            # 确保context是一个字典
            try:
                context = dict(state.context) if state.context else {}
            except (TypeError, ValueError, AttributeError) as e:
                logger.warning(f"转换状态上下文失败: {e}")
                context = {}

            context["error"] = f"K8sFixer处理失败: {str(e)}"
            from dataclasses import replace

            return replace(state, context=context)
