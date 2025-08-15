#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: LLM 服务封装
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Union

import ollama
import requests
from openai import OpenAI

from app.config.settings import config
from app.constants import LLM_MAX_RETRIES, LLM_TEMPERATURE_MAX, LLM_TEMPERATURE_MIN
from app.utils.error_handlers import (
    ErrorHandler,
    ExternalServiceError,
    ServiceError,
    ValidationError,
    retry_on_exception,
    validate_field_range,
)

logger = logging.getLogger("aiops.llm")


class LLMService:
    """统一的LLM服务接口，支持OpenAI和Ollama提供商"""

    def __init__(self):
        self.error_handler = ErrorHandler(logger)

        self.provider = (
            config.llm.provider.split("#")[0].strip()
            if config.llm.provider
            else "openai"
        )
        self.model = config.llm.effective_model
        self.temperature = self._validate_temperature(config.llm.temperature)
        self.max_tokens = config.llm.max_tokens

        self.backup_provider = (
            "ollama" if self.provider.lower() == "openai" else "openai"
        )

        if self.provider.lower() == "openai":
            self.client = OpenAI(
                api_key=config.llm.effective_api_key,
                base_url=config.llm.effective_base_url,
            )
            logger.debug(f"LLM服务(OpenAI)初始化完成: {self.model}")

            try:
                os.environ["OLLAMA_HOST"] = config.llm.ollama_base_url.replace(
                    "/v1", ""
                )
                logger.debug("备用Ollama服务初始化完成")
            except Exception as e:
                logger.warning(f"备用Ollama初始化失败: {str(e)}")

        elif self.provider.lower() == "ollama":
            self.client = None
            os.environ["OLLAMA_HOST"] = config.llm.ollama_base_url.replace("/v1", "")
            logger.debug(f"LLM服务(Ollama)初始化完成: {self.model}")

            try:
                self.backup_client = OpenAI(
                    api_key=config.llm.api_key, base_url=config.llm.base_url
                )
                logger.debug("备用OpenAI服务初始化完成")
            except Exception as e:
                logger.warning(f"备用OpenAI初始化失败: {str(e)}")
        else:
            raise ValidationError(f"不支持的LLM提供商: {self.provider}")

    def _validate_temperature(self, temperature: float) -> float:
        """验证温度参数"""
        if not (LLM_TEMPERATURE_MIN <= temperature <= LLM_TEMPERATURE_MAX):
            logger.warning(
                f"温度参数 {temperature} 超出范围 [{LLM_TEMPERATURE_MIN}, {LLM_TEMPERATURE_MAX}]，使用默认值"
            )
            return 0.7
        return temperature

    def _validate_generate_params(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """验证生成参数"""
        if not messages:
            raise ValidationError("消息列表不能为空")

        for i, msg in enumerate(messages):
            if not isinstance(msg, dict) or "role" not in msg or "content" not in msg:
                raise ValidationError(f"消息 {i} 格式无效，需要包含 role 和 content")

        effective_temp = temperature or self.temperature
        effective_max_tokens = max_tokens or self.max_tokens

        if temperature is not None:
            validate_field_range(
                temperature,
                "temperature",
                LLM_TEMPERATURE_MIN,
                LLM_TEMPERATURE_MAX,
            )

        return {
            "messages": messages,
            "temperature": effective_temp,
            "max_tokens": effective_max_tokens,
        }

    # 兼容测试：提供同步便捷方法（使用requests.post，便于单元测试mock）
    def generate_response(
        self, text: str, model: Optional[str] = None
    ) -> Optional[str]:
        try:
            url = f"{config.llm.effective_base_url}/chat/completions"
            payload = {
                "model": model or self.model,
                "messages": [{"role": "user", "content": text}],
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if isinstance(data, dict) and data.get("choices"):
                choice = data["choices"][0]
                # 支持多种常见格式
                if isinstance(choice, dict):
                    msg = choice.get("message") or {}
                    content = msg.get("content") if isinstance(msg, dict) else None
                    if content:
                        return content
                    delta = choice.get("delta") or {}
                    if isinstance(delta, dict) and delta.get("content"):
                        return delta.get("content")
            return None
        except Exception:
            return None

    def stream_response(self, text: str):
        url = f"{config.llm.effective_base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": text}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=30)
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    decoded = line.decode("utf-8").strip()
                    if not decoded.startswith("data:"):
                        continue
                    data_str = decoded.split("data:", 1)[1].strip()
                    if data_str == "[DONE]":
                        break
                    obj = json.loads(data_str)
                    choices = obj.get("choices") or []
                    if choices and isinstance(choices[0], dict):
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            yield content
                except Exception:
                    continue
        except Exception:
            # 失败时退化为一次性响应
            single = self.generate_response(text)
            if single:
                yield single

    def format_prompt(self, template: str, context: Dict[str, Any]) -> str:
        try:
            return template.format(**context)
        except Exception:
            return template

    def extract_code_blocks(self, text: str) -> List[str]:
        blocks = re.findall(r"```([\s\S]*?)```", text)
        return [b.strip() for b in blocks]

    def _call_openai_sync(
        self, messages: List[Dict[str, str]], model: str
    ) -> Optional[str]:
        client = (
            getattr(self, "backup_client", None)
            if self.provider.lower() == "ollama"
            else self.client
        )
        if not client:
            client = OpenAI(api_key=config.llm.api_key, base_url=config.llm.base_url)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        return (
            response.choices[0].message.content
            if response and hasattr(response, "choices")
            else None
        )

    async def generate_response_async(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        response_format: Optional[Dict[str, str]] = None,
        temperature: Optional[float] = None,
        stream: bool = False,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> Union[str, Dict[str, Any]]:
        # 将可选模型覆盖到实例，以复用现有实现
        if model:
            self.model = model
        return await self._generate_response_async_impl(
            messages=messages,
            system_prompt=system_prompt,
            response_format=response_format,
            temperature=temperature,
            stream=stream,
            max_tokens=max_tokens,
        )

    # 原异步实现改名为内部实现
    async def _generate_response_async_impl(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        response_format: Optional[Dict[str, str]] = None,
        temperature: Optional[float] = None,
        stream: bool = False,
        max_tokens: Optional[int] = None,
    ) -> Union[str, Dict[str, Any]]:
        """生成LLM响应，支持自动故障转移"""
        try:
            params = self._validate_generate_params(messages, temperature, max_tokens)

            if system_prompt:
                params["messages"] = [
                    {"role": "system", "content": system_prompt}
                ] + params["messages"]

            logger.debug(
                f"LLM请求: {len(params['messages'])} 条消息, 模型: {self.model}"
            )

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
            raise ServiceError(error_msg, "LLMService", "generate_response") from e

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
                ) from backup_error

        raise ServiceError("未知的LLM提供商配置", "LLMService")

    async def _call_openai_api(
        self,
        messages: List[Dict[str, str]],
        response_format: Optional[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        stream: bool = False,
    ) -> Optional[str]:
        """调用OpenAI API"""
        try:
            kwargs = {
                "model": config.llm.model,
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
                collected_content = []
                for chunk in response:
                    if chunk.choices and chunk.choices[0].delta.content:
                        collected_content.append(chunk.choices[0].delta.content)
                return "".join(collected_content)
            else:
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
        """调用Ollama API"""
        try:
            os.environ["OLLAMA_HOST"] = config.llm.ollama_base_url.replace("/v1", "")
            logger.debug(f"使用Ollama host: {os.environ.get('OLLAMA_HOST')}")

            ollama_messages = [
                {"role": m["role"], "content": m["content"]} for m in messages
            ]
            options = {"temperature": temperature, "num_predict": max_tokens}

            if stream:
                response_text = ""
                for chunk in ollama.chat(
                    model=config.llm.ollama_model,
                    messages=ollama_messages,
                    stream=True,
                    options=options,
                ):
                    if "message" in chunk and "content" in chunk["message"]:
                        response_text += chunk["message"]["content"]
                return response_text
            else:
                response = ollama.chat(
                    model=config.llm.ollama_model,
                    messages=ollama_messages,
                    options=options,
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
        """分析K8s问题并返回JSON格式的修复建议"""
        system_prompt = """你是一个Kubernetes专家，请分析部署问题。返回JSON格式:
{
    "problem_summary": "问题概述",
    "root_causes": ["根本原因"],
    "severity": "低/中/高/紧急",
    "fixes": [{"description": "修复描述", "yaml_changes": "YAML变更", "confidence": 0.9}],
    "additional_notes": "额外说明"
}"""

        try:
            context = f"""部署YAML:\n```yaml\n{deployment_yaml}\n```\n\n错误事件:\n```\n{error_event}\n```"""
            if additional_context:
                context += f"\n\n额外上下文:\n```\n{additional_context}\n```"

            messages = [{"role": "user", "content": context}]
            response_format = {"type": "json_object"}

            response = await self.generate_response_async(
                messages=messages,
                system_prompt=system_prompt,
                response_format=response_format,
                temperature=0.1,
            )

            if response:
                try:
                    return await self._extract_json_from_k8s_analysis(
                        response, messages
                    )
                except Exception:
                    alternative_response = await self.generate_response_async(
                        messages=messages, system_prompt=system_prompt, temperature=0.1
                    )
                    if alternative_response:
                        return await self._extract_json_from_k8s_analysis(
                            alternative_response, messages
                        )
                    return self._create_default_analysis()
            else:
                return self._create_default_analysis()

        except Exception as e:
            logger.error(f"K8s问题分析失败: {str(e)}")
            return self._create_default_analysis()

    def _create_default_analysis(self) -> Dict[str, Any]:
        """创建默认分析结果"""
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
        """从LLM响应中提取JSON"""
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            logger.warning("直接解析JSON失败，尝试提取")

        try:
            json_match = re.search(r"(\{.*\})", response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
        except (json.JSONDecodeError, AttributeError):
            logger.warning("提取JSON失败，尝试修复")

        try:
            fix_prompt = """修复上一条消息的JSON格式。返回JSON对象：problem_summary、root_causes、severity、fixes、additional_notes。"""
            fix_messages = messages + [
                {"role": "assistant", "content": response},
                {"role": "user", "content": fix_prompt},
            ]

            fixed_response = await self.generate_response_async(
                messages=fix_messages,
                temperature=0.1,
                response_format={"type": "json_object"},
            )

            if fixed_response:
                return json.loads(fixed_response)
            return self._create_default_analysis()

        except Exception as e:
            logger.error(f"修复JSON失败: {str(e)}")
            analysis = self._create_default_analysis()

            if "问题" in response or "problem" in response:
                analysis["problem_summary"] = "可能存在部署配置问题"
            if "修复" in response or "fix" in response:
                analysis["fixes"].append(
                    {
                        "description": "请查看原始响应中的修复建议",
                        "yaml_changes": "无法自动解析",
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
        """生成RCA分析总结"""
        system_prompt = """你是云平台监控专家。根据异常指标、相关性和根因候选，生成专业的分析总结和解决建议。使用简明专业的语言。"""

        try:
            content = f"""
指标异常:\n{json.dumps(anomalies, ensure_ascii=False, indent=2)}\n\n相关性:\n{json.dumps(correlations, ensure_ascii=False, indent=2)}\n\n候选根因:\n{json.dumps(candidates, ensure_ascii=False, indent=2)}\n\n请生成专业的根因分析总结和解决方案。
"""
            messages = [{"role": "user", "content": content}]

            response = await self.generate_response_async(
                messages=messages, system_prompt=system_prompt, temperature=0.3
            )
            return response

        except Exception as e:
            logger.error(f"生成RCA总结失败: {str(e)}")
            return None

    async def generate_fix_explanation(
        self, deployment: str, actions_taken: List[str], success: bool
    ) -> Optional[str]:
        """生成K8s修复说明"""
        system_prompt = """你是K8s修复系统解释器。根据部署名、执行操作和结果，提供简明的修复说明。"""

        try:
            result = "成功" if success else "失败"
            content = f"部署: {deployment}\n操作: {json.dumps(actions_taken, ensure_ascii=False)}\n结果: {result}\n\n请生成简明的修复说明。"
            messages = [{"role": "user", "content": content}]

            response = await self.generate_response_async(
                messages=messages, system_prompt=system_prompt, temperature=0.3
            )
            return response

        except Exception as e:
            logger.error(f"生成修复说明失败: {str(e)}")
            return None

    def is_healthy(self) -> bool:
        """检查LLM服务健康状态"""
        try:
            logger.debug("检查LLM服务健康状态")

            provider_health = self._check_provider_health(self.provider)
            if provider_health:
                logger.debug(f"LLM服务({self.provider})健康状态: 正常")
                return True

            logger.warning(
                f"LLM服务({self.provider})不可用，检查备用提供商({self.backup_provider})"
            )
            backup_health = self._check_provider_health(self.backup_provider)
            if backup_health:
                logger.debug(f"备用LLM服务({self.backup_provider})健康状态: 正常")
                return True

            logger.warning("所有LLM服务均不可用")
            return False

        except Exception as e:
            logger.error(f"检查LLM服务健康状态失败: {str(e)}")
            return False

    def _check_provider_health(self, provider: str) -> bool:
        """检查指定提供商的健康状态"""
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
        """检查OpenAI服务健康状态"""
        try:
            client = (
                getattr(self, "backup_client", None)
                if self.provider.lower() == "ollama"
                else self.client
            )
            if not client:
                client = OpenAI(
                    api_key=config.llm.api_key, base_url=config.llm.base_url
                )

            response = client.chat.completions.create(
                model=config.llm.model,
                messages=[{"role": "user", "content": "测试"}],
                max_tokens=5,
            )

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
        """检查Ollama服务健康状态"""
        try:
            os.environ["OLLAMA_HOST"] = config.llm.ollama_base_url.replace("/v1", "")

            try:
                response = ollama.list()
                if response and "models" in response:
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

                response = ollama.chat(
                    model=config.llm.ollama_model,
                    messages=[{"role": "user", "content": "测试"}],
                )

                if response and "message" in response:
                    logger.debug("Ollama单次请求测试通过")
                    return True
                else:
                    logger.warning("Ollama服务响应无效")
                    return False

        except Exception as e:
            logger.warning(f"Ollama健康检查失败: {str(e)}")
            return False
