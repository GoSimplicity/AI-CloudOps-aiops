"""
MCP服务发现与负载均衡
提供智能服务选择、故障转移和工具路由功能
"""

import asyncio
import logging
import random
import time
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime

import aiohttp

from .registry import MCPRegistry
from .models import MCPService, ServiceHealth, ServiceStatus, ToolResponse

logger = logging.getLogger("aiops.mcp.discovery")


class ServiceDiscovery:
    """MCP服务发现与负载均衡"""
    
    def __init__(self, registry: MCPRegistry):
        self.registry = registry
        self._service_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._cache_expiry: Dict[str, float] = {}
        self._cache_ttl = 60  # 缓存60秒
        
    async def get_service_for_tool(self, tool_name: str, preferred_services: Optional[List[str]] = None) -> Optional[MCPService]:
        """根据工具名称获取可用的服务"""
        try:
            # 获取所有支持该工具的服务
            candidate_services = []
            
            # 检查缓存
            cache_key = f"tool_{tool_name}"
            if cache_key in self._cache_expiry and time.time() < self._cache_expiry[cache_key]:
                services_with_tool = self._service_cache[cache_key]
            else:
                # 查询所有服务
                services_with_tool = []
                for service in self.registry.get_active_services():
                    if service.health != ServiceHealth.HEALTHY:
                        continue
                        
                    tools = await self.registry.get_service_tools(service.name)
                    if any(tool.get('name') == tool_name for tool in tools):
                        services_with_tool.append({
                            'service': service,
                            'tools': tools
                        })
                
                # 缓存结果
                self._service_cache[cache_key] = services_with_tool
                self._cache_expiry[cache_key] = time.time() + self._cache_ttl
            
            if not services_with_tool:
                return None
            
            # 优先使用指定的服务
            if preferred_services:
                for service_info in services_with_tool:
                    if service_info['service'].name in preferred_services:
                        return service_info['service']
            
            # 按权重和健康状态选择最优服务
            return self._select_best_service([info['service'] for info in services_with_tool])
            
        except Exception as e:
            logger.error(f"查找工具服务失败: {tool_name} - {e}")
            return None
    
    def _select_best_service(self, services: List[MCPService]) -> MCPService:
        """基于权重和健康状态选择最优服务"""
        if not services:
            return None
        
        if len(services) == 1:
            return services[0]
        
        # 计算权重（考虑健康状态）
        weighted_services = []
        for service in services:
            weight = service.weight
            
            # 健康状态好的服务权重更高
            if service.health == ServiceHealth.HEALTHY:
                weight *= 2
            elif service.health == ServiceHealth.UNHEALTHY:
                weight *= 0.5
            
            weighted_services.extend([service] * max(1, int(weight)))
        
        # 随机选择
        return random.choice(weighted_services)
    
    async def execute_tool(
        self, 
        tool_name: str, 
        parameters: Dict[str, Any], 
        preferred_services: Optional[List[str]] = None,
        timeout: Optional[int] = None
    ) -> ToolResponse:
        """执行工具调用，自动选择最优服务"""
        start_time = time.time()
        
        try:
            # 查找服务
            service = await self.get_service_for_tool(tool_name, preferred_services)
            
            if not service:
                return ToolResponse(
                    success=False,
                    result=None,
                    service_name="",
                    response_time=time.time() - start_time,
                    error=f"未找到支持工具 {tool_name} 的服务"
                )
            
            # 执行工具调用
            result = await self._execute_tool_on_service(
                service, tool_name, parameters, timeout
            )
            
            return ToolResponse(
                success=result.get('success', False),
                result=result.get('result'),
                service_name=service.name,
                response_time=time.time() - start_time,
                error=result.get('error')
            )
            
        except Exception as e:
            logger.error(f"执行工具失败: {tool_name} - {e}")
            return ToolResponse(
                success=False,
                result=None,
                service_name="",
                response_time=time.time() - start_time,
                error=str(e)
            )
    
    async def _execute_tool_on_service(
        self, 
        service: MCPService, 
        tool_name: str, 
        parameters: Dict[str, Any],
        timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        """在指定服务上执行工具调用"""
        try:
            timeout = timeout or service.timeout
            
            async with aiohttp.ClientSession() as session:
                request_data = {
                    "tool": tool_name,
                    "parameters": parameters
                }
                
                async with session.post(
                    f"{service.server_url.rstrip('/')}/tools/execute",
                    json=request_data,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("error"):
                            return {
                                'success': False,
                                'error': data['error'],
                                'result': None
                            }
                        return {
                            'success': True,
                            'result': data.get("result"),
                            'error': None
                        }
                    else:
                        return {
                            'success': False,
                            'error': f"HTTP {response.status}",
                            'result': None
                        }
                        
        except asyncio.TimeoutError:
            return {
                'success': False,
                'error': f"请求超时 ({timeout}s)",
                'result': None
            }
        except aiohttp.ClientError as e:
            return {
                'success': False,
                'error': f"网络错误: {str(e)}",
                'result': None
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'result': None
            }
    
    async def find_services_by_tags(self, tags: List[str]) -> List[MCPService]:
        """根据标签查找服务"""
        try:
            services = self.registry.get_active_services()
            
            # 过滤健康的服务
            healthy_services = [s for s in services if s.health == ServiceHealth.HEALTHY]
            
            # 按标签过滤
            if tags:
                matching_services = [
                    s for s in healthy_services
                    if any(tag in s.tags for tag in tags)
                ]
            else:
                matching_services = healthy_services
            
            return matching_services
            
        except Exception as e:
            logger.error(f"按标签查找服务失败: {tags} - {e}")
            return []
    
    async def get_service_capabilities(self, service_name: str) -> Dict[str, Any]:
        """获取服务的能力信息"""
        try:
            service = self.registry.get_service(service_name)
            if not service:
                return {}
            
            tools = await self.registry.get_service_tools(service_name)
            
            return {
                "service_name": service_name,
                "display_name": service.display_name,
                "description": service.description,
                "health": service.health.value,
                "status": service.status.value,
                "tools": tools,
                "tool_count": len(tools),
                "last_health_check": service.last_health_check.isoformat() if service.last_health_check else None
            }
            
        except Exception as e:
            logger.error(f"获取服务能力失败: {service_name} - {e}")
            return {}
    
    async def get_all_capabilities(self) -> Dict[str, Dict[str, Any]]:
        """获取所有服务的能力信息"""
        try:
            services = self.registry.get_active_services()
            capabilities = {}
            
            for service in services:
                if service.health == ServiceHealth.HEALTHY:
                    capabilities[service.name] = await self.get_service_capabilities(service.name)
            
            return capabilities
            
        except Exception as e:
            logger.error(f"获取所有服务能力失败: {e}")
            return {}
    
    async def execute_tools_bulk(
        self, 
        requests: List[Dict[str, Any]], 
        parallel: bool = True,
        max_concurrent: int = 5
    ) -> List[ToolResponse]:
        """批量执行工具调用"""
        try:
            if parallel:
                # 并行执行
                semaphore = asyncio.Semaphore(max_concurrent)
                
                async def execute_with_limit(request):
                    async with semaphore:
                        return await self.execute_tool(
                            tool_name=request["tool_name"],
                            parameters=request.get("parameters", {}),
                            preferred_services=request.get("preferred_services"),
                            timeout=request.get("timeout")
                        )
                
                tasks = [execute_with_limit(req) for req in requests]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # 处理异常
                final_results = []
                for result in results:
                    if isinstance(result, Exception):
                        final_results.append(ToolResponse(
                            success=False,
                            result=None,
                            service_name="",
                            response_time=0,
                            error=str(result)
                        ))
                    else:
                        final_results.append(result)
                
                return final_results
            else:
                # 串行执行
                results = []
                for request in requests:
                    result = await self.execute_tool(**request)
                    results.append(result)
                return results
                
        except Exception as e:
            logger.error(f"批量执行工具失败: {e}")
            return []
    
    def clear_cache(self):
        """清除缓存"""
        self._service_cache.clear()
        self._cache_expiry.clear()
        logger.info("MCP服务缓存已清除")
    
    async def refresh_service_cache(self, service_name: str):
        """刷新服务缓存"""
        try:
            # 清除相关缓存
            keys_to_remove = []
            for key in self._service_cache:
                if key.startswith("tool_"):
                    services_with_tool = self._service_cache[key]
                    if any(s['service'].name == service_name for s in services_with_tool):
                        keys_to_remove.append(key)
            
            for key in keys_to_remove:
                del self._service_cache[key]
                if key in self._cache_expiry:
                    del self._cache_expiry[key]
            
            logger.debug(f"已刷新服务 {service_name} 的缓存")
            
        except Exception as e:
            logger.error(f"刷新服务缓存失败: {service_name} - {e}")


class LoadBalancer:
    """负载均衡器"""
    
    def __init__(self):
        self._round_robin_index: Dict[str, int] = {}
        self._weighted_services: Dict[str, List[MCPService]] = {}
    
    def select_round_robin(self, services: List[MCPService], key: str = "default") -> MCPService:
        """轮询选择服务"""
        if not services:
            return None
        
        if key not in self._round_robin_index:
            self._round_robin_index[key] = 0
        
        index = self._round_robin_index[key] % len(services)
        service = services[index]
        self._round_robin_index[key] = (index + 1) % len(services)
        
        return service
    
    def select_weighted_round_robin(self, services: List[MCPService], key: str = "default") -> MCPService:
        """加权轮询选择服务"""
        if not services:
            return None
        
        # 构建加权服务列表
        weighted_services = []
        for service in services:
            weight = max(1, service.weight)
            # 健康状态影响权重
            if service.health == ServiceHealth.HEALTHY:
                weight *= 2
            elif service.health == ServiceHealth.UNHEALTHY:
                weight = max(1, weight // 2)
            
            weighted_services.extend([service] * weight)
        
        if not weighted_services:
            return None
        
        # 轮询选择
        if key not in self._weighted_services:
            self._weighted_services[key] = weighted_services
        
        services_list = self._weighted_services[key]
        if key not in self._round_robin_index:
            self._round_robin_index[key] = 0
        
        index = self._round_robin_index[key] % len(services_list)
        service = services_list[index]
        self._round_robin_index[key] = (index + 1) % len(services_list)
        
        return service
    
    def select_random(self, services: List[MCPService]) -> MCPService:
        """随机选择服务"""
        if not services:
            return None
        return random.choice(services)
    
    def select_least_connections(self, services: List[MCPService], active_connections: Dict[str, int]) -> MCPService:
        """最少连接数选择"""
        if not services:
            return None
        
        # 按连接数排序
        sorted_services = sorted(services, key=lambda s: active_connections.get(s.name, 0))
        return sorted_services[0] if sorted_services else None