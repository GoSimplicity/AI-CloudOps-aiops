"""
增强版MCP客户端
支持多服务注册发现、智能路由和故障转移
"""

import json
import logging
from typing import Any, Dict, List, Optional, Union
import asyncio

from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)

from .registry import MCPRegistry
from .service_discovery import ServiceDiscovery
from .plugin_manager import MCPPluginManager
from .models import MCPService, ToolResponse

logger = logging.getLogger("aiops.mcp.client_v2")


class EnhancedMCPClient:
    """增强版MCP客户端 - 支持多服务"""
    
    def __init__(self):
        from app.config.settings import config
        
        self.registry = MCPRegistry()
        self.service_discovery = ServiceDiscovery(self.registry)
        self.plugin_manager = MCPPluginManager(self.registry)
        
        # LLM客户端
        self.llm_client = AsyncOpenAI(
            api_key=config.llm.effective_api_key,
            base_url=config.llm.effective_base_url,
            timeout=config.llm.request_timeout
        )
        self.model = config.llm.effective_model
    
    async def start(self):
        """启动客户端"""
        try:
            await self.registry.start()
            await self.plugin_manager.start()
            logger.info("增强版MCP客户端已启动")
        except Exception as e:
            logger.error(f"启动MCP客户端失败: {e}")
            raise
    
    async def stop(self):
        """停止客户端"""
        try:
            await self.registry.stop()
            await self.plugin_manager.stop()
            logger.info("增强版MCP客户端已停止")
        except Exception as e:
            logger.error(f"停止MCP客户端失败: {e}")
    
    async def add_service(self, service: MCPService) -> bool:
        """添加MCP服务"""
        return await self.plugin_manager.add_service(service)
    
    async def remove_service(self, service_name: str) -> bool:
        """移除MCP服务"""
        return await self.plugin_manager.remove_service(service_name)
    
    async def list_services(self) -> List[MCPService]:
        """获取所有服务"""
        return self.registry.list_services()
    
    async def get_service(self, service_name: str) -> Optional[MCPService]:
        """获取单个服务"""
        return self.registry.get_service(service_name)
    
    async def get_market_status(self) -> Dict[str, Any]:
        """获取市场状态"""
        return self.registry.get_market_status()
    
    async def get_available_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        """获取所有可用工具"""
        return await self.registry.get_all_tools()
    
    async def execute_tool(
        self, 
        tool_name: str, 
        parameters: Dict[str, Any] = None,
        preferred_services: Optional[List[str]] = None,
        timeout: Optional[int] = None
    ) -> ToolResponse:
        """执行工具调用"""
        if parameters is None:
            parameters = {}
        
        return await self.service_discovery.execute_tool(
            tool_name=tool_name,
            parameters=parameters,
            preferred_services=preferred_services,
            timeout=timeout
        )
    
    def _create_messages(self, system_content: str, user_content: str) -> List[
        Union[ChatCompletionSystemMessageParam, ChatCompletionUserMessageParam]]:
        """创建符合OpenAI类型要求的消息列表"""
        return [
            ChatCompletionSystemMessageParam(role="system", content=system_content),
            ChatCompletionUserMessageParam(role="user", content=user_content)
        ]
    
    async def _format_tool_result(self, question: str, tool_name: str, parameters: Dict[str, Any], result: Any) -> str:
        """格式化工具调用结果"""
        format_system_content = f"""你是一个智能助手，根据工具调用结果回答用户问题。
工具：{tool_name}
参数：{json.dumps(parameters, ensure_ascii=False)}

请用自然、友好的语言回答用户的问题，确保答案准确有用。"""
        
        format_user_content = f"""用户问题：{question}

工具调用结果：{json.dumps(result, ensure_ascii=False, indent=2)}"""
        
        format_messages = self._create_messages(format_system_content, format_user_content)
        
        try:
            format_response = await self.llm_client.chat.completions.create(
                model=self.model,
                messages=format_messages,
                temperature=0.3,
                max_tokens=1000
            )
            
            return format_response.choices[0].message.content
        except Exception as e:
            logger.error(f"格式化工具结果失败: {e}")
            return f"工具执行成功，结果：{result}"
    
    async def process_query(self, question: str, preferred_services: Optional[List[str]] = None) -> str:
        """处理自然语言查询，智能选择工具"""
        try:
            # 获取所有可用工具
            all_tools = await self.get_available_tools()
            if not all_tools:
                return "当前没有可用的MCP工具"
            
            # 构建工具信息
            tools_info = []
            for service_name, tools in all_tools.items():
                service = await self.get_service(service_name)
                if service and service.health == ServiceHealth.HEALTHY:
                    for tool in tools:
                        tools_info.append({
                            "service_name": service_name,
                            "service_display_name": service.display_name,
                            "name": tool.get('name', ''),
                            "description": tool.get('description', ''),
                            "parameters": tool.get('parameters', {})
                        })
            
            if not tools_info:
                return "当前没有可用的健康工具服务"
            
            # 构建系统提示
            system_content = f"""你是一个智能助手，能够根据用户的问题自主选择合适的工具来回答。

可用工具列表：
{json.dumps(tools_info, ensure_ascii=False, indent=2)}

请分析用户的问题，判断是否需要使用工具，如果需要，选择最合适的工具并生成相应的参数。

请始终以以下JSON格式回复：
{{
    "should_use_tool": true/false,
    "tool_name": "工具名称",
    "service_name": "服务名称",
    "parameters": {{
        // 工具需要的参数
    }},
    "reasoning": "选择这个工具的原因"
}}

如果不需要使用工具，should_use_tool设为false，并回复：
{{
    "should_use_tool": false,
    "direct_answer": "直接回答用户的内容"
}}"""
            
            messages = self._create_messages(system_content, question)
            
            # 调用AI模型进行决策
            response = await self.llm_client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.1,
                max_tokens=800
            )
            
            try:
                decision = json.loads(response.choices[0].message.content)
            except json.JSONDecodeError:
                # 如果AI回复不是JSON格式，尝试提取关键信息
                content = response.choices[0].message.content
                logger.warning(f"AI回复格式错误: {content}")
                decision = self._fallback_decision(question, tools_info)
            
            # 根据决策执行相应操作
            if decision.get("should_use_tool") and decision.get("tool_name"):
                tool_name = decision["tool_name"]
                parameters = decision.get("parameters", {})
                service_name = decision.get("service_name")
                
                # 执行工具调用
                tool_result = await self.execute_tool(
                    tool_name=tool_name,
                    parameters=parameters,
                    preferred_services=[service_name] if service_name else None
                )
                
                if tool_result.success:
                    # 使用AI格式化结果
                    return await self._format_tool_result(question, tool_name, parameters, tool_result.result)
                else:
                    return f"抱歉，工具 {tool_name} 执行失败: {tool_result.error}"
            else:
                # 直接回答
                return decision.get("direct_answer", "我无法回答这个问题")
        
        except Exception as e:
            logger.error(f"处理查询失败: {e}")
            return self._fallback_response(question)
    
    def _fallback_decision(self, question: str, tools_info: List[Dict[str, Any]]) -> Dict[str, Any]:
        """AI决策失败时的回退逻辑"""
        question_lower = question.lower()
        
        # 简单的关键词匹配
        tool_mapping = {
            "时间": "get_current_time",
            "几点": "get_current_time",
            "日期": "get_current_time",
            "文件": "read_file",
            "读取": "read_file",
            "计算": "calculate",
            "计算": "calculate",
        }
        
        for keyword, tool_name in tool_mapping.items():
            if keyword in question_lower:
                # 查找支持该工具的服务
                for tool_info in tools_info:
                    if tool_info["name"] == tool_name:
                        return {
                            "should_use_tool": True,
                            "tool_name": tool_name,
                            "service_name": tool_info["service_name"],
                            "parameters": {},
                            "reasoning": f"基于关键词 '{keyword}' 选择工具"
                        }
        
        return {
            "should_use_tool": False,
            "direct_answer": "我无法确定需要使用哪个工具来回答您的问题。"
        }
    
    def _fallback_response(self, question: str) -> str:
        """处理查询失败时的回退响应"""
        return "抱歉，MCP服务暂时不可用，请稍后重试。"
    
    async def batch_execute_tools(
        self, 
        requests: List[Dict[str, Any]], 
        parallel: bool = True,
        max_concurrent: int = 5
    ) -> List[ToolResponse]:
        """批量执行工具调用"""
        return await self.service_discovery.execute_tools_bulk(
            requests=requests,
            parallel=parallel,
            max_concurrent=max_concurrent
        )
    
    async def discover_services(self, service_urls: List[str]) -> Dict[str, bool]:
        """发现并注册新的MCP服务"""
        results = {}
        
        for url in service_urls:
            try:
                # 检查服务健康状态
                async with aiohttp.ClientSession() as session:
                    health_url = f"{url.rstrip('/')}/health"
                    async with session.get(health_url, timeout=5) as response:
                        if response.status != 200:
                            results[url] = False
                            continue
                    
                    # 获取服务信息
                    info_url = f"{url.rstrip('/')}/info"
                    async with session.get(info_url, timeout=10) as response:
                        if response.status == 200:
                            info = await response.json()
                            
                            # 创建服务配置
                            service = MCPService(
                                name=info.get("name", f"service_{hash(url) % 10000}"),
                                display_name=info.get("display_name", "未知服务"),
                                description=info.get("description", "自动发现的服务"),
                                server_url=url,
                                version=info.get("version", "1.0.0"),
                                tags=info.get("tags", ["discovered"])
                            )
                            
                            # 注册服务
                            success = await self.add_service(service)
                            results[url] = success
                        else:
                            results[url] = False
            
            except Exception as e:
                logger.error(f"发现服务失败 {url}: {e}")
                results[url] = False
        
        return results
    
    async def is_service_available(self, service_name: str) -> bool:
        """检查服务是否可用"""
        service = self.registry.get_service(service_name)
        return service is not None and service.health == ServiceHealth.HEALTHY
    
    async def wait_for_service(self, service_name: str, timeout: float = 30) -> bool:
        """等待服务变为可用"""
        return await self.registry.wait_for_service(service_name, timeout)


class MCPServiceManager:
    """MCP服务管理器 - 单例模式"""
    
    _instance: Optional[EnhancedMCPClient] = None
    
    @classmethod
    async def get_instance(cls) -> EnhancedMCPClient:
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = EnhancedMCPClient()
            await cls._instance.start()
        return cls._instance
    
    @classmethod
    async def shutdown(cls):
        """关闭单例实例"""
        if cls._instance is not None:
            await cls._instance.stop()
            cls._instance = None