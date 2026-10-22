#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: MCP客户端
"""

import json
import logging
from typing import Any, Dict, List, Optional, Union

import aiohttp
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)

logger = logging.getLogger("aiops.mcp.client")


class MCPSessionClient:
    """MCP会话客户端"""

    def __init__(self, server_url: str = None):
        from app.config.settings import config

        mcp_config = config.mcp
        self.server_url = (server_url or mcp_config.server_url).rstrip("/")
        self.headers = {"X-API-Key": config.security.api_key}
        self.timeout = mcp_config.timeout
        self.max_retries = mcp_config.max_retries
        self.health_check_interval = mcp_config.health_check_interval
        logger.info(f"MCP会话客户端初始化，服务端地址: {self.server_url}")

    async def execute_tool(
        self, tool_name: str, parameters: Dict[str, Any] = None
    ) -> Any:
        """执行工具调用"""
        if parameters is None:
            parameters = {}

        try:
            async with aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                request_data = {"tool": tool_name, "parameters": parameters}

                async with session.post(
                    f"{self.server_url}/tools/execute",
                    json=request_data,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("error"):
                            raise RuntimeError(f"工具执行失败: {data['error']}")
                        return data.get("result")
                    else:
                        raise RuntimeError(f"工具调用失败，状态码: {response.status}")

        except aiohttp.ClientError as e:
            logger.error(f"MCP客户端网络错误: {str(e)}")
            raise RuntimeError(f"MCP服务连接失败: {str(e)}")
        except Exception as e:
            logger.error(f"MCP客户端执行异常: {str(e)}")
            raise

    async def get_available_tools(self) -> Dict[str, Any]:
        """获取可用工具列表"""
        return await self._make_get_request("/tools", "获取工具列表")

    async def health_check(self) -> bool:
        """健康检查"""
        try:
            async with aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                async with session.get(f"{self.server_url}/health") as response:
                    return response.status == 200
        except Exception:
            return False

    async def _make_get_request(
        self, endpoint: str, operation_name: str
    ) -> Dict[str, Any]:
        """通用GET请求处理"""
        try:
            async with aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                async with session.get(f"{self.server_url}{endpoint}") as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        raise RuntimeError(
                            f"{operation_name}失败，状态码: {response.status}"
                        )
        except aiohttp.ClientError as e:
            logger.error(f"MCP客户端网络错误: {str(e)}")
            raise RuntimeError(f"MCP服务连接失败: {str(e)}")
        except Exception as e:
            logger.error(f"MCP客户端{operation_name}异常: {str(e)}")
            raise


class MCPAssistant:
    """MCP助手类 - 集成到现有助手系统"""

    def __init__(self):
        from app.config.settings import config

        self.client = MCPSessionClient()
        self.llm_client = AsyncOpenAI(
            api_key=config.llm.effective_api_key,
            base_url=config.llm.effective_base_url,
            timeout=config.llm.request_timeout,
        )
        self.model = config.llm.effective_model
        # 简易会话上下文：按 session_id 记录最近若干轮 Q/A
        self._history: Dict[str, List[Dict[str, str]]] = {}
        self._max_history_turns: int = 6  # 最多保留 6 条（约 3 轮）

    def _create_messages(
        self, system_content: str, user_content: str
    ) -> List[Union[ChatCompletionSystemMessageParam, ChatCompletionUserMessageParam]]:
        """创建符合OpenAI类型要求的消息列表"""
        return [
            ChatCompletionSystemMessageParam(role="system", content=system_content),
            ChatCompletionUserMessageParam(role="user", content=user_content),
        ]

    def _build_history_block(self, session_id: Optional[str]) -> str:
        """将会话历史拼接为可读文本块，便于作为提示注入。"""
        if not session_id:
            return ""
        records = self._history.get(session_id)
        if not records:
            return ""
        # 仅取最近 _max_history_turns 条
        recent = records[-self._max_history_turns :]
        lines: List[str] = []
        for item in recent:
            q = item.get("q", "")
            a = item.get("a", "")
            if q:
                lines.append(f"- 用户: {q}")
            if a:
                lines.append(f"- 助手: {a}")
        return "\n".join(lines)

    def _append_history_pair(self, session_id: Optional[str], question: str, answer: str) -> None:
        """将一轮问答追加到会话历史并裁剪长度。"""
        if not session_id:
            return
        pair = {"q": question, "a": answer}
        bucket = self._history.get(session_id)
        if bucket is None:
            self._history[session_id] = [pair]
        else:
            bucket.append(pair)
            if len(bucket) > self._max_history_turns:
                self._history[session_id] = bucket[-self._max_history_turns :]

    async def _format_tool_result(
        self, question: str, tool_name: str, parameters: Dict[str, Any], result: Any
    ) -> str:
        """格式化工具调用结果"""
        format_system_content = f"根据工具调用结果回答用户问题。工具：{tool_name}，参数：{json.dumps(parameters, ensure_ascii=False)}"
        format_user_content = (
            f"用户问题：{question}\n工具结果：{json.dumps(result, ensure_ascii=False)}"
        )

        format_messages = self._create_messages(
            format_system_content, format_user_content
        )

        format_response = await self.llm_client.chat.completions.create(
            model=self.model, messages=format_messages, temperature=0.3, max_tokens=1000
        )

        return format_response.choices[0].message.content

    async def process_query(self, question: str, session_id: Optional[str] = None) -> str:
        """处理MCP模式下的查询（带轻量上下文）。"""
        try:
            # 获取可用工具列表
            tools_info = await self.client.get_available_tools()
            available_tools = tools_info.get("tools", [])

            if not available_tools:
                return "当前没有可用的MCP工具"

            # 构建工具描述供AI决策
            tools_description = []
            for tool in available_tools:
                tools_description.append(
                    {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    }
                )

            # 构建系统提示
            history_block = self._build_history_block(session_id)
            system_content = (
                """你是一个智能助手，能够根据用户的问题自主选择合适的工具来回答。请分析用户的问题，判断是否需要使用工具，如果需要，选择最合适的工具并生成相应的参数。

可用工具列表：
"""
                + json.dumps(tools_description, ensure_ascii=False, indent=2)
                + """

对话历史（如有）：
"""
                + (history_block if history_block else "无")
                + """

请始终以以下JSON格式回复：
{
    "should_use_tool": true/false,
    "tool_name": "工具名称",
    "parameters": {
        // 工具需要的参数
    },
    "reasoning": "选择这个工具的原因"
}

如果不需要使用工具，should_use_tool设为false，并回复：
{
    "should_use_tool": false,
    "direct_answer": "直接回答用户的内容"
}"""
            )

            messages = self._create_messages(system_content, question)

            # 调用AI模型进行决策
            response = await self.llm_client.chat.completions.create(
                model=self.model, messages=messages, temperature=0.1, max_tokens=500
            )

            try:
                decision = json.loads(response.choices[0].message.content)
            except json.JSONDecodeError:
                # 如果AI回复不是JSON格式，尝试提取关键信息
                content = response.choices[0].message.content
                if "get_current_time" in content.lower() or "时间" in question.lower():
                    decision = {
                        "should_use_tool": True,
                        "tool_name": "get_current_time",
                        "parameters": {},
                        "reasoning": "用户询问时间问题",
                    }
                else:
                    decision = {
                        "should_use_tool": False,
                        "direct_answer": "我无法确定需要使用哪个工具来回答您的问题。",
                    }

            # 根据AI决策执行相应操作
            if decision.get("should_use_tool") and decision.get("tool_name"):
                tool_name = decision["tool_name"]
                parameters = decision.get("parameters", {})

                # 执行工具调用
                result = await self.client.execute_tool(tool_name, parameters)

                if result:
                    # 使用AI格式化结果
                    final_answer = await self._format_tool_result(
                        question, tool_name, parameters, result
                    )
                    self._append_history_pair(session_id, question, final_answer)
                    return final_answer
                else:
                    final_answer = f"抱歉，工具 {tool_name} 执行失败"
                    self._append_history_pair(session_id, question, final_answer)
                    return final_answer
            else:
                # 直接回答
                final_answer = decision.get("direct_answer", "我无法回答这个问题")
                self._append_history_pair(session_id, question, final_answer)
                return final_answer

        except Exception as e:
            logger.error(f"MCP助手处理查询失败: {str(e)}")
            # 降级到简单关键词匹配
            question_lower = question.lower()
            if (
                "时间" in question_lower
                or "几点" in question_lower
                or "现在" in question_lower
            ):
                try:
                    result = await self.client.execute_tool("get_current_time")
                    if result:
                        final_answer = f"当前时间是: {result.get('time', '未知')}"
                        self._append_history_pair(session_id, question, final_answer)
                        return final_answer
                except Exception as tool_error:
                    logger.error(f"工具调用失败: {str(tool_error)}")
            final_answer = f"MCP服务暂时不可用: {str(e)}"
            self._append_history_pair(session_id, question, final_answer)
            return final_answer

    async def is_available(self) -> bool:
        """检查MCP服务是否可用"""
        try:
            from app.config.settings import config

            if config.llm.provider.lower() == "openai" and (not config.llm.api_key or config.llm.api_key == "sk-xxx"):
                return False
            if not await self.client.health_check():
                return False
            await self.llm_client.with_options(timeout=5, max_retries=0).models.list()
            return True
        except Exception:
            return False
