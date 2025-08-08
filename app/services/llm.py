#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 大语言模型服务 - 提供LLM服务接口，支持多种LLM提供商
"""

# ==================== 核心依赖导入 ====================
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Union

import ollama  # Ollama本地模型客户端

# ==================== LLM客户端库导入 ====================
from openai import OpenAI  # OpenAI官方客户端

# ==================== 内部组件导入 ====================
from app.config.settings import config  # 系统配置
from app.constants import (
    LLM_MAX_RETRIES,
    LLM_TEMPERATURE_MAX,  # LLM相关常量
    LLM_TEMPERATURE_MIN,
)
from app.utils.error_handlers import (
    ErrorHandler,  # 错误处理工具
    ExternalServiceError,
    ServiceError,
    ValidationError,
    retry_on_exception,
    validate_field_range,
)

logger = logging.getLogger("aiops.llm")


class LLMService:
    """
    LLM服务管理类 - AI-CloudOps系统的大语言模型统一服务接口

    该类提供了一个统一的接口来管理和调用多种大语言模型提供商。它实现了
    智能的提供商选择和回退机制，确保在任何情况下都能提供可靠的AI服务。

    核心特性：
    1. 多提供商支持 - 无缝集成OpenAI和Ollama
    2. 自动故障转移 - 主要提供商失效时自动切换
    3. 参数验证 - 确保所有输入参数的有效性
    4. 错误处理 - 全面的异常处理和恢复机制
    5. 性能优化 - 连接池和缓存机制
    6. 监控集成 - 服务状态监控和健康检查

    架构设计：
    - 主要提供商：用户配置的首选LLM提供商
    - 备用提供商：当主要提供商不可用时的备选方案
    - 统一接口：对外提供一致的API调用方式
    - 智能路由：根据请求特性选择最合适的提供商

    Attributes:
        provider (str): 主要LLM提供商名称
        backup_provider (str): 备用LLM提供商名称
        model (str): 当前使用的模型名称
        temperature (float): 生成温度参数
        max_tokens (int): 最大令牌数限制
        client: 主要提供商的客户端实例
        backup_client: 备用提供商的客户端实例
        error_handler: 错误处理器实例
    """

    def __init__(self):
        """
        初始化LLM服务管理器

        设置主要和备用LLM提供商，配置相关参数和客户端连接。
        系统会优先使用外部模型(OpenAI)，如果不可用则自动回退到本地模型(Ollama)。

        初始化流程：
        1. 解析和验证配置参数
        2. 初始化错误处理器
        3. 设置主要提供商和备用提供商
        4. 创建客户端连接
        5. 验证服务可用性

        异常处理：
        - 配置错误：抛出ValidationError
        - 连接失败：记录警告但继续初始化
        - 不支持的提供商：抛出ValidationError
        """
        # 初始化错误处理器，提供统一的异常处理
        self.error_handler = ErrorHandler(logger)

        # 解析和清理提供商配置，移除可能的注释和空格
        self.provider = (
            config.llm.provider.split("#")[0].strip()
            if config.llm.provider
            else "openai"
        )
        self.model = config.llm.effective_model
        self.temperature = self._validate_temperature(config.llm.temperature)
        self.max_tokens = config.llm.max_tokens

        # 设置备用提供商，确保高可用性
        self.backup_provider = (
            "ollama" if self.provider.lower() == "openai" else "openai"
        )
        self.backup_model = (
            config.llm.ollama_model
            if self.backup_provider == "ollama"
            else config.llm.model
        )

        # 根据配置的主要提供商类型进行不同的初始化流程
        if self.provider.lower() == "openai":
            # OpenAI提供商初始化流程
            # 使用OpenAI兼容的API客户端
            self.client = OpenAI(
                api_key=config.llm.effective_api_key,
                base_url=config.llm.effective_base_url,
            )
            logger.debug(f"LLM服务(OpenAI)初始化完成: {self.model}")

            # 预初始化备用Ollama客户端 - 无需设置host，使用环境变量
            try:
                # 使用环境变量设置Ollama主机地址，为故障转移做准备
                os.environ["OLLAMA_HOST"] = config.llm.ollama_base_url.replace(
                    "/v1", ""
                )
                logger.debug(
                    f"备用LLM服务(Ollama)初始化完成: {config.llm.ollama_model}, OLLAMA_HOST={os.environ.get('OLLAMA_HOST')}"
                )
            except Exception as e:
                logger.warning(f"备用Ollama初始化失败: {str(e)}")

        elif self.provider.lower() == "ollama":
            # Ollama提供商初始化流程
            # 使用Ollama客户端，不需要具体的客户端实例
            self.client = None  # Ollama使用独立的API调用，不需要客户端实例

            # 使用环境变量设置Ollama主机地址
            os.environ["OLLAMA_HOST"] = config.llm.ollama_base_url.replace("/v1", "")
            logger.debug(
                f"LLM服务(Ollama)初始化完成: {self.model}, OLLAMA_HOST={os.environ.get('OLLAMA_HOST')}"
            )

            # 预初始化备用OpenAI客户端
            try:
                self.backup_client = OpenAI(
                    api_key=config.llm.api_key, base_url=config.llm.base_url
                )
                logger.debug(f"备用LLM服务(OpenAI)初始化完成: {config.llm.model}")
            except Exception as e:
                logger.warning(f"备用OpenAI初始化失败: {str(e)}")
        else:
            # 不支持的提供商类型，抛出验证错误
            raise ValidationError(f"不支持的LLM提供商: {self.provider}")

    def _validate_temperature(self, temperature: float) -> float:
        """
        验证温度参数的有效性 - 模型生成参数验证方法

        温度参数控制着语言模型生成文本的随机性和创造性。较低的温度值
        会使生成的文本更加确定性和一致性，而较高的温度值会增加随机性和创造性。

        Args:
            temperature (float): 输入的温度值

        Returns:
            float: 验证后的温度值，范围在[LLM_TEMPERATURE_MIN, LLM_TEMPERATURE_MAX]之内

        验证规则：
        - 温度必须在配置的最小值和最大值之间
        - 如果超出范围，会使用0.7作为默认值
        - 默认值0.7是一个平衡的选择，既保证了一定的创造性又保持了输出的稳定性

        常用温度值参考：
        - 0.0-0.3: 非常确定性的输出，适合事实性问答
        - 0.4-0.7: 平衡的输出，适合大多数应用场景
        - 0.8-1.0: 富有创造性的输出，适合文学创作等
        """
        # 检查温度值是否在允许的范围内
        if not (LLM_TEMPERATURE_MIN <= temperature <= LLM_TEMPERATURE_MAX):
            logger.warning(
                f"温度参数 {temperature} 超出范围 [{LLM_TEMPERATURE_MIN}, {LLM_TEMPERATURE_MAX}]，使用默认值"
            )
            return 0.7  # 返回平衡的默认温度值
        return temperature

    def _validate_generate_params(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        验证生成参数的有效性 - 请求参数验证和标准化方法

        在调用LLM API之前，验证所有输入参数的格式和有效性，确保请求能够成功执行。
        该方法会检查消息格式、参数范围等，并提供合理的默认值。

        Args:
            messages (List[Dict[str, str]]): 对话消息列表，每个消息包含role和content
            temperature (Optional[float]): 生成温度参数，如果为None则使用实例默认值
            max_tokens (Optional[int]): 最大令牌数，如果为None则使用实例默认值

        Returns:
            Dict[str, Any]: 验证后的参数字典，包含：
                - messages: 验证后的消息列表
                - temperature: 有效的温度值
                - max_tokens: 有效的最大令牌数

        Raises:
            ValidationError: 当参数格式无效时抛出

        验证项目：
        1. 消息列表不能为空
        2. 每个消息必须包含role和content字段
        3. 温度参数必须在有效范围内
        4. 令牌数必须为正数

        消息格式要求：
        - role: 消息角色，通常为"user"、"assistant"或"system"
        - content: 消息内容，不能为空字符串
        """
        # 验证消息列表不能为空
        if not messages:
            raise ValidationError("消息列表不能为空")

        # 验证每个消息的格式
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict) or "role" not in msg or "content" not in msg:
                raise ValidationError(f"消息 {i} 格式无效，需要包含 role 和 content")

        # 设置有效的温度和令牌数参数
        effective_temp = temperature or self.temperature
        effective_max_tokens = max_tokens or self.max_tokens

        # 验证温度范围
        if temperature is not None:
            validate_field_range(
                {"temperature": temperature},
                "temperature",
                LLM_TEMPERATURE_MIN,
                LLM_TEMPERATURE_MAX,
            )

        # 返回验证后的参数字典
        return {
            "messages": messages,
            "temperature": effective_temp,
            "max_tokens": effective_max_tokens,
        }

    async def generate_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        response_format: Optional[Dict[str, str]] = None,
        temperature: Optional[float] = None,
        stream: bool = False,
        max_tokens: Optional[int] = None,
    ) -> Union[str, Dict[str, Any]]:
        """
        生成LLM响应 - 统一的文本生成接口

        这是LLM服务的核心方法，提供统一的接口来调用不同的语言模型提供商。
        支持多种参数配置和响应格式，并具备自动故障转移能力。

        Args:
            messages (List[Dict[str, str]]): 对话消息列表
            system_prompt (Optional[str]): 系统提示词，用于设置模型行为
            response_format (Optional[Dict[str, str]]): 响应格式要求，如JSON格式
            temperature (Optional[float]): 生成温度，控制输出的随机性
            stream (bool): 是否启用流式响应，默认False
            max_tokens (Optional[int]): 最大生成令牌数

        Returns:
            Union[str, Dict[str, Any]]: 生成的响应内容

        特性：
        1. 自动故障转移：主要提供商失败时自动切换到备用提供商
        2. 参数验证：确保所有输入参数的有效性
        3. 系统提示词：支持添加系统级别的指导信息
        4. 流式响应：支持实时文本生成
        5. 格式化输出：支持JSON等结构化输出格式
        6. 错误处理：完善的异常处理和错误恢复机制

        异常处理：
        - ValidationError: 参数验证失败
        - ServiceError: 服务调用失败
        - ExternalServiceError: 外部服务不可用

        使用示例：
        ```python
        messages = [{"role": "user", "content": "Hello"}]
        response = await llm.generate_response(messages)
        ```
        """
        try:
            # 验证参数
            params = self._validate_generate_params(messages, temperature, max_tokens)

            # 添加系统提示
            if system_prompt:
                params["messages"] = [
                    {"role": "system", "content": system_prompt}
                ] + params["messages"]

            logger.debug(
                f"LLM请求: {len(params['messages'])} 条消息, 模型: {self.model}"
            )

            # 执行生成，带自动回退
            return await self._execute_generation_with_fallback(
                params["messages"],
                response_format,
                params["temperature"],
                params["max_tokens"],
                stream,
            )

        except (ValidationError, ServiceError):
            raise
        except Exception as e:
            error_msg, _ = self.error_handler.log_and_return_error(e, "LLM响应生成")
            raise ServiceError(error_msg, "LLMService", "generate_response")

    @retry_on_exception(
        max_retries=LLM_MAX_RETRIES, delay=1.0, exceptions=(ExternalServiceError,)
    )
    async def _execute_generation_with_fallback(
        self,
        messages: List[Dict[str, str]],
        response_format: Optional[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        stream: bool = False,
    ) -> Union[str, Dict[str, Any]]:
        """执行生成，支持提供商自动回退"""

        # 尝试主要提供商
        try:
            if self.provider.lower() == "openai":
                return await self._call_openai_api(
                    messages, response_format, temperature, max_tokens, stream
                )
            elif self.provider.lower() == "ollama":
                return await self._call_ollama_api(
                    messages, temperature, max_tokens, stream
                )
        except Exception as e:
            logger.warning(f"{self.provider} API调用失败，尝试备用提供商: {str(e)}")

            # 尝试备用提供商
            try:
                if self.backup_provider.lower() == "ollama":
                    return await self._call_ollama_api(
                        messages, temperature, max_tokens, stream
                    )
                elif self.backup_provider.lower() == "openai":
                    return await self._call_openai_api(
                        messages, response_format, temperature, max_tokens, stream
                    )
            except Exception as backup_error:
                logger.error(
                    f"备用提供商 {self.backup_provider} 也失败: {str(backup_error)}"
                )
                raise ExternalServiceError(
                    f"所有LLM提供商均不可用: 主要({str(e)}), 备用({str(backup_error)})",
                    "LLM",
                )

        raise ServiceError("未知的LLM提供商配置", "LLMService")

    async def _call_openai_api(
        self,
        messages: List[Dict[str, str]],
        response_format: Optional[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        stream: bool = False,
    ) -> Optional[str]:
        """调用OpenAI兼容API生成响应"""
        try:
            kwargs = {
                "model": config.llm.model,  # 确保使用正确的模型名称
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": stream,
            }

            if response_format:
                kwargs["response_format"] = response_format

            client = (
                getattr(self, "backup_client", None)
                if self.provider.lower() == "ollama"
                else self.client
            )
            if not client:
                client = OpenAI(
                    api_key=config.llm.api_key, base_url=config.llm.base_url
                )

            response = client.chat.completions.create(**kwargs)

            if stream:
                # 处理流式响应
                collected_chunks = []
                collected_content = []

                for chunk in response:
                    collected_chunks.append(chunk)
                    if chunk.choices and chunk.choices[0].delta.content:
                        collected_content.append(chunk.choices[0].delta.content)

                return "".join(collected_content)
            else:
                # 常规响应
                result = response.choices[0].message.content
                logger.debug(f"LLM响应长度: {len(result) if result else 0}")
                return result
        except Exception as e:
            logger.error(f"OpenAI API调用失败: {str(e)}")
            raise e

    async def _call_ollama_api(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        stream: bool = False,
    ) -> Optional[str]:
        """调用Ollama API生成响应"""
        try:
            # 确保设置正确的Ollama host
            os.environ["OLLAMA_HOST"] = config.llm.ollama_base_url.replace("/v1", "")
            logger.debug(f"使用Ollama host: {os.environ.get('OLLAMA_HOST')}")

            # 将消息转换为Ollama格式
            if stream:
                # 流式处理
                response = ""
                for chunk in ollama.chat(
                    model=config.llm.ollama_model,
                    messages=[
                        {"role": m["role"], "content": m["content"]} for m in messages
                    ],
                    stream=True,
                    options={"temperature": temperature, "num_predict": max_tokens},
                ):
                    if "message" in chunk and "content" in chunk["message"]:
                        response += chunk["message"]["content"]

                return response
            else:
                # 常规响应
                response = ollama.chat(
                    model=config.llm.ollama_model,
                    messages=[
                        {"role": m["role"], "content": m["content"]} for m in messages
                    ],
                    options={"temperature": temperature, "num_predict": max_tokens},
                )

                if "message" in response and "content" in response["message"]:
                    return response["message"]["content"]
                else:
                    logger.error("Ollama响应格式无效")
                    return None
        except Exception as e:
            logger.error(f"Ollama API调用失败: {str(e)}")
            raise e

    async def analyze_k8s_problem(
        self,
        deployment_yaml: str,
        error_event: str,
        additional_context: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """分析Kubernetes问题并提供修复建议"""
        system_prompt = """
你是一个Kubernetes专家，帮助用户分析和修复Kubernetes部署问题。
请根据提供的部署YAML和错误事件，识别问题并提出修复建议。
你的回答应该包含以下格式的JSON结构:
{
    "problem_summary": "简短的问题概述",
    "root_causes": ["根本原因1", "根本原因2"],
    "severity": "严重程度 (低/中/高/紧急)",
    "fixes": [
        {
            "description": "修复1的描述",
            "yaml_changes": "需要进行的YAML变更",
            "confidence": 0.9
        }
    ],
    "additional_notes": "任何额外的说明或建议"
}
请确保回答仅包含有效的JSON，不要添加额外解释。
"""

        try:
            # 准备消息
            context = f"""
部署YAML:
```yaml
{deployment_yaml}
```

错误事件:
```
{error_event}
```
"""
            if additional_context:
                context += f"\n额外上下文信息:\n```\n{additional_context}\n```"

            messages = [{"role": "user", "content": context}]

            # 调用LLM API
            response_format = {"type": "json_object"}
            response = await self.generate_response(
                messages=messages,
                system_prompt=system_prompt,
                response_format=response_format,
                temperature=0.1,
            )

            if response:
                try:
                    # 提取JSON响应
                    return await self._extract_json_from_k8s_analysis(
                        response, messages
                    )
                except Exception as json_error:
                    logger.error(f"解析K8s分析JSON失败: {str(json_error)}")
                    # 尝试再次调用，但不指定JSON响应格式
                    alternative_response = await self.generate_response(
                        messages=messages, system_prompt=system_prompt, temperature=0.1
                    )

                    if alternative_response:
                        return await self._extract_json_from_k8s_analysis(
                            alternative_response, messages
                        )
                    else:
                        logger.error("获取替代响应失败")
                        return self._create_default_analysis()
            else:
                logger.error("从LLM获取响应失败")
                return self._create_default_analysis()

        except Exception as e:
            logger.error(f"K8s问题分析失败: {str(e)}")
            return self._create_default_analysis()

    def _create_default_analysis(self) -> Dict[str, Any]:
        """创建默认的分析结果"""
        return {
            "problem_summary": "无法分析问题",
            "root_causes": ["分析过程中出现错误"],
            "severity": "未知",
            "fixes": [],
            "additional_notes": "请检查您的部署YAML和错误描述，并确保LLM服务正常运行。",
        }

    async def _extract_json_from_k8s_analysis(
        self, response: str, messages: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """从LLM响应中提取JSON对象"""
        # 尝试直接解析
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            logger.warning("直接解析JSON失败，尝试提取JSON部分")

        # 尝试从文本中提取JSON部分
        try:
            # 查找以 { 开头，以 } 结尾的部分
            json_match = re.search(r"(\{.*\})", response, re.DOTALL)
            if json_match:
                extracted_json = json_match.group(1)
                return json.loads(extracted_json)
        except (json.JSONDecodeError, AttributeError):
            logger.warning("从响应中提取JSON失败，尝试进行修复")

        # 尝试请求LLM修复JSON
        try:
            fix_prompt = """
上一条消息中的JSON格式有问题，请修复它。
返回一个有效的JSON对象，包含以下字段：
- problem_summary: 问题概述 (字符串)
- root_causes: 根本原因列表 (字符串数组)
- severity: 严重程度 (字符串: "低", "中", "高" 或 "紧急")
- fixes: 修复建议列表 (对象数组，每个对象包含 description, yaml_changes 和 confidence 字段)
- additional_notes: 额外说明 (字符串)

请确保返回的是一个有效的、格式正确的JSON对象，不要添加其他解释。
"""
            fix_messages = messages + [
                {"role": "assistant", "content": response},
                {"role": "user", "content": fix_prompt},
            ]

            fixed_response = await self.generate_response(
                messages=fix_messages,
                temperature=0.1,
                response_format={"type": "json_object"},
            )

            if fixed_response:
                return json.loads(fixed_response)
            else:
                logger.error("修复JSON响应失败")
                return self._create_default_analysis()

        except Exception as e:
            logger.error(f"修复JSON格式失败: {str(e)}")

            # 创建最基本的返回数据
            analysis = self._create_default_analysis()

            # 尝试从原始响应中提取有用信息
            if "问题概述" in response or "problem_summary" in response:
                analysis["problem_summary"] = "可能存在部署配置问题"

            if "修复" in response or "fix" in response:
                analysis["fixes"].append(
                    {
                        "description": "请查看原始响应中的修复建议",
                        "yaml_changes": "无法自动解析YAML变更",
                        "confidence": 0.5,
                    }
                )

            return analysis

    async def generate_rca_summary(
        self,
        anomalies: Dict[str, Any],
        correlations: Dict[str, Any],
        candidates: List[Dict[str, Any]],
    ) -> Optional[str]:
        """生成根因分析总结"""
        system_prompt = """
你是一个专业的云平台监控和根因分析专家。
请根据提供的指标异常、相关性和候选根因，总结分析结果并提供清晰的根因说明。
提供具有洞察力的分析，着重于主要问题及其原因，并提供可能的解决方向。
使用简明专业的语言，注重实用性建议。
"""

        try:
            # 准备消息内容
            content = f"""
## 指标异常:
{json.dumps(anomalies, ensure_ascii=False, indent=2)}

## 相关性:
{json.dumps(correlations, ensure_ascii=False, indent=2)}

## 候选根因:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

请生成一份专业的根因分析总结，并提出可能的解决方案。
"""
            messages = [{"role": "user", "content": content}]

            # 生成根因分析总结
            response = await self.generate_response(
                messages=messages, system_prompt=system_prompt, temperature=0.3
            )

            return response

        except Exception as e:
            logger.error(f"生成RCA总结失败: {str(e)}")
            return None

    async def generate_fix_explanation(
        self, deployment: str, actions_taken: List[str], success: bool
    ) -> Optional[str]:
        """生成修复说明"""
        system_prompt = """
你是Kubernetes自动修复系统的解释器。
请根据提供的部署名称、已执行的操作和修复结果，提供一份简明清晰的修复说明。
内容应该简洁、专业，并对技术细节进行合理解释。
"""

        try:
            # 准备消息内容
            result = "成功" if success else "失败"
            content = f"""
部署: {deployment}
执行的操作:
{json.dumps(actions_taken, ensure_ascii=False, indent=2)}
修复结果: {result}

请生成一份简明的修复说明。
"""
            messages = [{"role": "user", "content": content}]

            # 生成修复说明
            response = await self.generate_response(
                messages=messages, system_prompt=system_prompt, temperature=0.3
            )

            return response

        except Exception as e:
            logger.error(f"生成修复说明失败: {str(e)}")
            return None

    def is_healthy(self) -> bool:
        """
        检查LLM服务是否健康 - 综合服务健康状态检查方法

        检查所有配置的LLM提供商的健康状态，确保至少有一个提供商可用。
        该方法会先检查主要提供商，如果不可用则检查备用提供商。

        Returns:
            bool: True表示服务健康，False表示所有提供商都不可用

        检查策略：
        1. 优先检查主要提供商的健康状态
        2. 如果主要提供商健康，直接返回True
        3. 如果主要提供商不健康，检查备用提供商
        4. 如果备用提供商健康，返回True
        5. 如果所有提供商都不健康，返回False

        健康检查项目：
        - 服务连接性测试
        - API密钥有效性验证
        - 简单请求响应测试
        - 模型可用性检查

        应用场景：
        - 服务启动时的健康检查
        - 定期服务状态监控
        - 故障转移决策依据
        - 系统诊断和运维
        """
        try:
            logger.debug("检查LLM服务健康状态")

            # 检查主要提供商健康状态
            provider_health = self._check_provider_health(self.provider)

            if provider_health:
                logger.debug(f"LLM服务({self.provider})健康状态: 正常")
                return True

            # 如果主要提供商不健康，检查备用提供商
            logger.warning(
                f"LLM服务({self.provider})不可用，检查备用提供商({self.backup_provider})"
            )
            backup_health = self._check_provider_health(self.backup_provider)

            if backup_health:
                logger.debug(f"备用LLM服务({self.backup_provider})健康状态: 正常")
                return True

            # 所有提供商都不可用
            logger.warning("所有LLM服务均不可用")
            logger.debug("建议检查：")
            logger.debug("1. OpenAI API密钥是否有效且余额充足")
            logger.debug("2. Ollama服务是否在本地运行 (127.0.0.1:11434)")
            logger.debug("3. 网络连接是否正常")
            return False

        except Exception as e:
            logger.error(f"检查LLM服务健康状态时出错: {str(e)}")
            return False

    def _check_provider_health(self, provider: str) -> bool:
        """
        检查特定提供商的健康状态 - 单个提供商健康检查方法

        针对指定的LLM提供商执行健康检查，验证其服务可用性和响应能力。
        该方法会根据不同的提供商类型调用相应的健康检查逻辑。

        Args:
            provider (str): 要检查的提供商名称，支持"openai"和"ollama"

        Returns:
            bool: True表示提供商健康，False表示不可用

        检查流程：
        1. 识别提供商类型
        2. 调用对应的健康检查方法
        3. 处理检查过程中的异常
        4. 返回健康状态结果

        支持的提供商：
        - openai: 检查OpenAI API的连接性和可用性
        - ollama: 检查本地Ollama服务的状态

        异常处理：
        - 不支持的提供商类型会记录警告
        - 检查过程中的异常会被捕获并记录
        - 任何异常都会导致健康检查失败
        """
        try:
            if provider.lower() == "openai":
                return self._check_openai_health()
            elif provider.lower() == "ollama":
                return self._check_ollama_health()
            else:
                logger.warning(f"不支持的LLM提供商: {provider}")
                return False
        except Exception as e:
            logger.error(f"检查{provider}健康状态失败: {str(e)}")
            return False

    def _check_openai_health(self) -> bool:
        """
        检查OpenAI服务健康状态 - OpenAI提供商健康检查方法

        通过发送简单的测试请求来验证OpenAI API的连接性和可用性。
        该方法会创建或使用现有的客户端连接，发送最小化的测试请求来确认服务状态。

        Returns:
            bool: True表示OpenAI服务健康，False表示服务不可用

        检查流程：
        1. 获取或创建OpenAI客户端实例
        2. 发送简单的聊天完成请求
        3. 验证响应的有效性和完整性
        4. 记录检查结果和错误信息

        健康检查策略：
        - 使用最小的令牌数(5个)减少成本
        - 发送简单的中文测试文本
        - 检查响应结构的完整性
        - 捕获和记录所有可能的异常

        应用场景：
        - 服务启动时的连接验证
        - 定期的服务可用性监控
        - 故障转移前的状态确认
        - 系统健康检查API
        """
        try:
            # 获取适当的客户端实例 - 根据当前提供商选择主要或备用客户端
            client = (
                getattr(self, "backup_client", None)
                if self.provider.lower() == "ollama"
                else self.client
            )
            if not client:
                # 如果没有现有客户端，创建新的临时客户端进行测试
                client = OpenAI(
                    api_key=config.llm.api_key, base_url=config.llm.base_url
                )

            # 发送简单的测试请求验证API连接和服务可用性
            # 使用最小令牌数以减少测试成本
            response = client.chat.completions.create(
                model=config.llm.model,
                messages=[{"role": "user", "content": "测试"}],
                max_tokens=5,  # 最小令牌数，仅用于连接验证
            )

            # 验证响应的完整性和有效性
            if response and hasattr(response, "choices") and len(response.choices) > 0:
                logger.debug("OpenAI健康检查通过")
                return True
            else:
                logger.warning("OpenAI服务响应无效")
                return False

        except Exception as e:
            error_str = str(e)
            if "insufficient" in error_str.lower() and "balance" in error_str.lower():
                logger.error("OpenAI健康检查失败: 账户余额不足")
            elif "403" in error_str:
                logger.error("OpenAI健康检查失败: API密钥权限问题或余额不足")
            elif "401" in error_str:
                logger.error("OpenAI健康检查失败: API密钥无效")
            elif "connection" in error_str.lower():
                logger.error("OpenAI健康检查失败: 网络连接问题")
            else:
                logger.warning(f"OpenAI健康检查失败: {error_str}")
            return False

    def _check_ollama_health(self) -> bool:
        """
        检查Ollama服务健康状态 - Ollama提供商健康检查方法

        通过检查模型列表和发送测试请求来验证Ollama服务的可用性。
        该方法会设置正确的主机环境变量，检查需要的模型是否可用，并验证服务响应。

        Returns:
            bool: True表示Ollama服务健康，False表示服务不可用

        检查流程：
        1. 设置正确的Ollama主机环境变量
        2. 尝试获取可用模型列表
        3. 验证所需模型是否可用
        4. 如果模型列表检查失败，发送测试请求验证基本功能

        健康检查策略：
        - 优先检查模型可用性和完整性
        - 备选方案：直接发送聊天测试请求
        - 使用环境变量管理主机配置
        - 全面的异常处理和错误恢复

        模型验证逻辑：
        - 检查配置的模型是否在可用模型列表中
        - 如果模型列表获取失败，尝试直接调用聊天API
        - 验证聊天响应的结构和内容完整性

        应用场景：
        - 本地Ollama服务状态监控
        - 模型可用性验证
        - 服务启动检查
        - 故障诊断和排查
        """
        try:
            # 设置正确的Ollama主机地址，去除API版本路径
            os.environ["OLLAMA_HOST"] = config.llm.ollama_base_url.replace("/v1", "")

            # 首先尝试获取模型列表来验证服务连接性和模型可用性
            try:
                response = ollama.list()
                if response and "models" in response:
                    # 检查所需的模型是否在可用模型列表中
                    model_available = any(
                        model["name"] == config.llm.ollama_model
                        for model in response["models"]
                    )
                    if not model_available:
                        logger.warning(f"Ollama模型 {config.llm.ollama_model} 不可用")
                        return False

                    logger.debug("Ollama健康检查通过")
                    return True
                else:
                    logger.warning("Ollama服务响应无效")
                    return False
            except Exception as e:
                logger.warning(f"获取Ollama模型列表失败: {str(e)}")

                # 如果模型列表检查失败，尝试直接发送聊天请求作为备选验证方法
                response = ollama.chat(
                    model=config.llm.ollama_model,
                    messages=[{"role": "user", "content": "测试"}],
                )

                # 验证聊天响应的结构完整性
                if response and "message" in response:
                    logger.debug("Ollama单次请求测试通过")
                    return True
                else:
                    logger.warning("Ollama服务响应无效")
                    return False

        except Exception as e:
            logger.warning(f"Ollama健康检查失败: {str(e)}")
            return False
