"""
MCP插件管理器
提供配置文件驱动的MCP服务管理功能
支持动态加载、卸载和更新MCP服务
"""

import asyncio
import logging
import yaml
import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import os
import aiofiles
import hashlib

from .registry import MCPRegistry
from .models import MCPService, ServiceConfig, ServiceStatus

logger = logging.getLogger("aiops.mcp.plugin_manager")


class MCPPluginManager:
    """MCP插件管理器 - 配置文件驱动的服务管理"""
    
    def __init__(self, registry: MCPRegistry, config_path: Optional[str] = None):
        self.registry = registry
        self.config_path = config_path or self._get_default_config_path()
        self._config_hash = None
        self._config_watch_task: Optional[asyncio.Task] = None
        self._running = False
        
    def _get_default_config_path(self) -> str:
        """获取默认配置文件路径"""
        # 优先使用环境变量
        if config_path := os.getenv('MCP_CONFIG_PATH'):
            return config_path
            
        # 使用项目配置目录
        return os.path.join('config', 'mcp_services.yaml')
        
    async def start(self):
        """启动插件管理器"""
        if self._running:
            return
            
        self._running = True
        await self.load_services_from_config()
        
        # 启动配置文件监控
        self._config_watch_task = asyncio.create_task(self._watch_config_file())
        logger.info(f"MCP插件管理器已启动，配置文件: {self.config_path}")
        
    async def stop(self):
        """停止插件管理器"""
        if not self._running:
            return
            
        self._running = False
        if self._config_watch_task:
            self._config_watch_task.cancel()
            try:
                await self._config_watch_task
            except asyncio.CancelledError:
                pass
        logger.info("MCP插件管理器已停止")
        
    async def load_services_from_config(self) -> bool:
        """从配置文件加载服务"""
        try:
            if not os.path.exists(self.config_path):
                logger.warning(f"配置文件不存在: {self.config_path}")
                return False
                
            async with aiofiles.open(self.config_path, 'r', encoding='utf-8') as f:
                content = await f.read()
                
            # 检查配置是否有变化
            new_hash = hashlib.md5(content.encode()).hexdigest()
            if new_hash == self._config_hash:
                return True  # 配置未变化
                
            self._config_hash = new_hash
            
            # 解析配置
            if self.config_path.endswith('.json'):
                config_data = json.loads(content)
            else:
                config_data = yaml.safe_load(content)
                
            service_config = ServiceConfig(**config_data)
            
            # 同步服务
            await self._sync_services(service_config.services)
            
            logger.info(f"成功从配置文件加载 {len(service_config.services)} 个MCP服务")
            return True
            
        except Exception as e:
            logger.error(f"加载配置文件失败: {str(e)}")
            return False
            
    async def _sync_services(self, new_services: List[MCPService]):
        """同步服务列表"""
        current_services = {s.name: s for s in self.registry.list_services()}
        new_service_names = {s.name for s in new_services}
        
        # 添加新服务
        for service in new_services:
            if service.name not in current_services:
                await self.registry.register_service(service)
                logger.info(f"添加新服务: {service.name}")
            else:
                # 更新现有服务
                await self._update_service(service)
                
        # 移除已删除的服务
        for service_name in list(current_services.keys()):
            if service_name not in new_service_names:
                await self.registry.unregister_service(service_name)
                logger.info(f"移除服务: {service_name}")
                
    async def _update_service(self, service: MCPService):
        """更新服务配置"""
        existing = self.registry.get_service(service.name)
        if existing and existing != service:
            await self.registry.unregister_service(service.name)
            await self.registry.register_service(service)
            logger.info(f"更新服务配置: {service.name}")
            
    async def add_service(self, service: MCPService) -> bool:
        """添加单个服务"""
        try:
            # 添加到注册表
            success = await self.registry.register_service(service)
            if success:
                # 更新配置文件
                await self._update_config_file()
                logger.info(f"成功添加服务: {service.name}")
            return success
        except Exception as e:
            logger.error(f"添加服务失败: {service.name} - {str(e)}")
            return False
            
    async def remove_service(self, service_name: str) -> bool:
        """移除单个服务"""
        try:
            success = await self.registry.unregister_service(service_name)
            if success:
                # 更新配置文件
                await self._update_config_file()
                logger.info(f"成功移除服务: {service_name}")
            return success
        except Exception as e:
            logger.error(f"移除服务失败: {service_name} - {str(e)}")
            return False
            
    async def update_service_config(self, service_name: str, **kwargs) -> bool:
        """更新服务配置"""
        try:
            service = self.registry.get_service(service_name)
            if not service:
                return False
                
            # 更新属性
            for key, value in kwargs.items():
                if hasattr(service, key):
                    setattr(service, key, value)
                    
            service.updated_at = datetime.now()
            
            # 重新注册服务
            await self.registry.unregister_service(service_name)
            await self.registry.register_service(service)
            
            # 更新配置文件
            await self._update_config_file()
            
            logger.info(f"成功更新服务配置: {service_name}")
            return True
            
        except Exception as e:
            logger.error(f"更新服务配置失败: {service_name} - {str(e)}")
            return False
            
    async def _update_config_file(self) -> bool:
        """更新配置文件"""
        try:
            services = self.registry.list_services()
            config_data = {
                'services': [service.dict(exclude={'status', 'health', 'last_health_check', 'registered_at', 'updated_at'}) 
                           for service in services]
            }
            
            # 确保目录存在
            Path(self.config_path).parent.mkdir(parents=True, exist_ok=True)
            
            async with aiofiles.open(self.config_path, 'w', encoding='utf-8') as f:
                if self.config_path.endswith('.json'):
                    await f.write(json.dumps(config_data, indent=2, ensure_ascii=False))
                else:
                    await f.write(yaml.dump(config_data, allow_unicode=True, indent=2))
                    
            # 更新配置哈希
            content = json.dumps(config_data, sort_keys=True)
            self._config_hash = hashlib.md5(content.encode()).hexdigest()
            
            return True
            
        except Exception as e:
            logger.error(f"更新配置文件失败: {str(e)}")
            return False
            
    async def _watch_config_file(self):
        """监控配置文件变化"""
        last_mtime = 0
        
        while self._running:
            try:
                if os.path.exists(self.config_path):
                    current_mtime = os.path.getmtime(self.config_path)
                    if current_mtime > last_mtime:
                        last_mtime = current_mtime
                        await asyncio.sleep(1)  # 等待文件写入完成
                        await self.load_services_from_config()
                        
                await asyncio.sleep(5)  # 每5秒检查一次
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"配置文件监控异常: {str(e)}")
                await asyncio.sleep(10)
                
    def create_sample_config(self) -> str:
        """创建示例配置文件"""
        sample_config = {
            "services": [
                {
                    "name": "time_service",
                    "display_name": "时间服务",
                    "description": "提供时间相关的工具服务",
                    "version": "1.0.0",
                    "server_url": "http://localhost:9000",
                    "timeout": 30,
                    "max_retries": 3,
                    "health_check_interval": 60,
                    "weight": 1,
                    "tags": ["utility", "time"],
                    "metadata": {
                        "author": "AI-CloudOps",
                        "category": "utility"
                    }
                },
                {
                    "name": "file_service", 
                    "display_name": "文件服务",
                    "description": "提供文件操作相关的工具服务",
                    "version": "1.0.0",
                    "server_url": "http://localhost:9001",
                    "timeout": 30,
                    "max_retries": 3,
                    "health_check_interval": 60,
                    "weight": 2,
                    "tags": ["file", "system"],
                    "metadata": {
                        "author": "AI-CloudOps",
                        "category": "system"
                    }
                },
                {
                    "name": "calculator_service",
                    "display_name": "计算器服务",
                    "description": "提供数学计算相关的工具服务", 
                    "version": "1.0.0",
                    "server_url": "http://localhost:9002",
                    "timeout": 30,
                    "max_retries": 3,
                    "health_check_interval": 60,
                    "weight": 1,
                    "tags": ["math", "calculator"],
                    "metadata": {
                        "author": "AI-CloudOps", 
                        "category": "utility"
                    }
                }
            ],
            "global_timeout": 30,
            "max_concurrent_requests": 100,
            "health_check_enabled": true,
            "load_balancing": "round_robin",
            "cache_enabled": true,
            "cache_ttl": 300
        }
        
        return yaml.dump(sample_config, allow_unicode=True, indent=2)
        
    async def export_config(self, export_path: str) -> bool:
        """导出当前配置"""
        try:
            services = self.registry.list_services()
            config_data = {
                'services': [service.dict(exclude={'status', 'health', 'last_health_check', 'registered_at', 'updated_at'}) 
                           for service in services],
                'export_timestamp': datetime.now().isoformat(),
                'total_services': len(services),
                'active_services': len([s for s in services if s.status == ServiceStatus.ACTIVE])
            }
            
            async with aiofiles.open(export_path, 'w', encoding='utf-8') as f:
                if export_path.endswith('.json'):
                    await f.write(json.dumps(config_data, indent=2, ensure_ascii=False))
                else:
                    await f.write(yaml.dump(config_data, allow_unicode=True, indent=2))
                    
            logger.info(f"配置已导出到: {export_path}")
            return True
            
        except Exception as e:
            logger.error(f"导出配置失败: {str(e)}")
            return False