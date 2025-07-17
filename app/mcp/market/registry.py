"""
MCP服务注册中心
提供服务注册、发现、健康检查和状态管理功能
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import aiohttp

from .models import MCPService, ServiceInstance, ServiceHealth, ServiceStatus, ServiceHealthReport

logger = logging.getLogger("aiops.mcp.registry")


class MCPRegistry:
    """MCP服务注册中心"""
    
    def __init__(self):
        self._services: Dict[str, MCPService] = {}
        self._instances: Dict[str, ServiceInstance] = {}
        self._health_check_task: Optional[asyncio.Task] = None
        self._running = False
        
    async def start(self):
        """启动注册中心"""
        if self._running:
            return
            
        self._running = True
        self._health_check_task = asyncio.create_task(self._health_check_loop())
        logger.info("MCP注册中心已启动")
        
    async def stop(self):
        """停止注册中心"""
        if not self._running:
            return
            
        self._running = False
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
        logger.info("MCP注册中心已停止")
        
    async def register_service(self, service: MCPService) -> bool:
        """注册MCP服务"""
        try:
            # 验证服务URL
            if not service.server_url.startswith(('http://', 'https://')):
                raise ValueError(f"无效的服务地址: {service.server_url}")
                
            self._services[service.name] = service
            
            # 创建服务实例
            instance = ServiceInstance(service=service)
            self._instances[service.name] = instance
            
            # 立即进行健康检查
            await self._check_service_health(service.name)
            
            logger.info(f"MCP服务已注册: {service.name} ({service.server_url})")
            return True
            
        except Exception as e:
            logger.error(f"注册MCP服务失败: {service.name} - {str(e)}")
            return False
            
    async def unregister_service(self, service_name: str) -> bool:
        """注销MCP服务"""
        if service_name in self._services:
            del self._services[service_name]
            if service_name in self._instances:
                del self._instances[service_name]
            logger.info(f"MCP服务已注销: {service_name}")
            return True
        return False
        
    def get_service(self, service_name: str) -> Optional[MCPService]:
        """获取MCP服务配置"""
        return self._services.get(service_name)
        
    def list_services(self) -> List[MCPService]:
        """获取所有MCP服务"""
        return list(self._services.values())
        
    def get_active_services(self) -> List[MCPService]:
        """获取活跃的MCP服务"""
        return [s for s in self._services.values() if s.status == ServiceStatus.ACTIVE]
        
    def get_healthy_services(self) -> List[MCPService]:
        """获取健康的MCP服务"""
        return [s for s in self._services.values() if s.health == ServiceHealth.HEALTHY]
        
    async def get_service_tools(self, service_name: str) -> List[Dict[str, Any]]:
        """获取服务的可用工具"""
        if service_name not in self._instances:
            return []
            
        instance = self._instances[service_name]
        return instance.available_tools
        
    async def get_all_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        """获取所有服务的工具"""
        tools = {}
        for service_name in self._services:
            service_tools = await self.get_service_tools(service_name)
            if service_tools:
                tools[service_name] = service_tools
        return tools
        
    async def _check_service_health(self, service_name: str) -> bool:
        """检查单个服务健康状态"""
        if service_name not in self._services:
            return False
            
        service = self._services[service_name]
        instance = self._instances[service_name]
        
        start_time = datetime.now()
        
        try:
            async with aiohttp.ClientSession() as session:
                # 健康检查
                health_url = f"{service.server_url.rstrip('/')}/health"
                async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status != 200:
                        raise Exception(f"健康检查失败: {response.status}")
                        
                # 获取工具列表
                tools_url = f"{service.server_url.rstrip('/')}/tools"
                async with session.get(tools_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        tools_data = await response.json()
                        instance.available_tools = tools_data.get('tools', [])
                    else:
                        instance.available_tools = []
                        
            # 更新状态
            service.status = ServiceStatus.ACTIVE
            service.health = ServiceHealth.HEALTHY
            service.last_health_check = datetime.now()
            
            instance.last_check = datetime.now()
            instance.consecutive_failures = 0
            
            response_time = (datetime.now() - start_time).total_seconds()
            logger.debug(f"服务健康检查成功: {service_name} ({response_time:.2f}s)")
            return True
            
        except Exception as e:
            service.health = ServiceHealth.UNHEALTHY
            service.last_health_check = datetime.now()
            
            instance.consecutive_failures += 1
            instance.last_check = datetime.now()
            
            logger.warning(f"服务健康检查失败: {service_name} - {str(e)}")
            
            # 如果连续失败次数过多，标记为错误状态
            if instance.consecutive_failures >= 3:
                service.status = ServiceStatus.ERROR
                
            return False
            
    async def _health_check_loop(self):
        """健康检查循环"""
        while self._running:
            try:
                tasks = []
                for service_name in list(self._services.keys()):
                    tasks.append(self._check_service_health(service_name))
                    
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    
                # 等待下一次检查
                await asyncio.sleep(30)  # 每30秒检查一次
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"健康检查循环异常: {str(e)}")
                await asyncio.sleep(5)
                
    def get_market_status(self) -> Dict[str, Any]:
        """获取市场状态总览"""
        total_services = len(self._services)
        active_services = len([s for s in self._services.values() if s.status == ServiceStatus.ACTIVE])
        healthy_services = len([s for s in self._services.values() if s.health == ServiceHealth.HEALTHY])
        
        services = []
        for service_name, service in self._services.items():
            instance = self._instances.get(service_name)
            report = ServiceHealthReport(
                service_name=service_name,
                status=service.status,
                health=service.health,
                last_check=service.last_health_check,
                available_tools=[tool.get('name', '') for tool in instance.available_tools] if instance else []
            )
            services.append(report)
            
        return {
            "total_services": total_services,
            "active_services": active_services,
            "healthy_services": healthy_services,
            "total_tools": sum(len(s.available_tools) for s in self._instances.values()),
            "services": services
        }
        
    async def wait_for_service(self, service_name: str, timeout: float = 30) -> bool:
        """等待服务变为可用状态"""
        start_time = datetime.now()
        
        while (datetime.now() - start_time).total_seconds() < timeout:
            service = self.get_service(service_name)
            if service and service.status == ServiceStatus.ACTIVE and service.health == ServiceHealth.HEALTHY:
                return True
                
            await asyncio.sleep(1)
            
        return False